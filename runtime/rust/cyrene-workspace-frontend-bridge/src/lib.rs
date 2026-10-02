//! ┌─────────────────────────────────────────────────────────────────────┐
//! │  📄 lib.rs                                                          │
//! │  Package: cyrene-workspace-frontend-bridge                          │
//! │  Role: Authority-side, fixed-target Relay byte bridge.              │
//! │                                                                     │
//! │  模块职责：将 Relay 有界字节流转发到固定 Authority upstream；不授   │
//! │  权、不解析业务消息，也不向 Product HTTP 派发。                     │
//! └─────────────────────────────────────────────────────────────────────┘

#![forbid(unsafe_code)]

use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

pub use cyrene_plugin_contracts::workspace_authority_v1::WorkspaceDescriptor;
pub use cyrene_plugin_contracts::workspace_bridge_v1::workspace_frontend_bridge_service_server::{
    WorkspaceFrontendBridgeService, WorkspaceFrontendBridgeServiceServer,
};
pub use cyrene_plugin_contracts::workspace_bridge_v1::{
    BridgeDiscoverWorkspacesRequest, BridgeDiscoverWorkspacesResponse,
    BridgeExecuteInvocationRequest, BridgeExecuteInvocationResponse,
};
use cyrene_plugin_contracts::workspace_tunnel_v1::{
    tunnel_frame::Payload, AuthorityBridgeRegistered, RegisterAuthorityBridge, TunnelData,
    TunnelError, TunnelFrame, TunnelHalfClose, TunnelTarget,
};
use cyrene_workspace_client_sdk::{MutualTlsClientConfig, WorkspaceRelayClient};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;
use tokio::sync::{mpsc, oneshot, Mutex};
use tokio_stream::wrappers::ReceiverStream;
use tonic::transport::server::UdsConnectInfo;
use tonic::{Request, Response, Status};
use tracing::{info, warn};

const MAX_FRAME_BYTES: usize = 64 * 1024;
// The substream and multiplexed Relay queues each hold at most three 64 KiB frames;
// this keeps the Bridge's copied-frame budget under the shared 1 MiB stream cap.
const FRAME_QUEUE_CAPACITY: usize = 3;
const MAX_ACTIVE_STREAMS: usize = 128;

/// Local legacy bridge RPCs are restricted to the BFF Unix peer and never dispatch work.
pub struct WorkspaceFrontendBridgeServiceImpl {
    authorized_uid: u32,
}

impl WorkspaceFrontendBridgeServiceImpl {
    /// Creates a fail-closed local bridge facade restricted to one OS user identity.
    pub fn new(authorized_uid: u32) -> Self {
        Self { authorized_uid }
    }

    // Tonic's service boundary requires its concrete wire-level `Status` type.
    #[allow(clippy::result_large_err)]
    fn verify_local_peer<T>(&self, request: &Request<T>) -> Result<(), Status> {
        let peer = request
            .extensions()
            .get::<UdsConnectInfo>()
            .and_then(|info| info.peer_cred.as_ref())
            .ok_or_else(|| Status::unauthenticated("Bridge requires an authenticated UDS peer"))?;
        if peer.uid() != self.authorized_uid {
            return Err(Status::permission_denied("UDS peer UID is not authorized"));
        }
        Ok(())
    }
}

#[tonic::async_trait]
impl WorkspaceFrontendBridgeService for WorkspaceFrontendBridgeServiceImpl {
    async fn discover_workspaces(
        &self,
        request: Request<BridgeDiscoverWorkspacesRequest>,
    ) -> Result<Response<BridgeDiscoverWorkspacesResponse>, Status> {
        self.verify_local_peer(&request)?;
        Err(Status::failed_precondition(
            "workspace discovery must use authenticated Workspace Authority v2 RPC",
        ))
    }

    async fn execute_invocation(
        &self,
        request: Request<BridgeExecuteInvocationRequest>,
    ) -> Result<Response<BridgeExecuteInvocationResponse>, Status> {
        self.verify_local_peer(&request)?;
        Err(Status::failed_precondition(
            "direct Bridge dispatch is disabled; enqueue once through Workspace Authority",
        ))
    }
}

/// Registers this process as the only fixed Authority target on Relay and bridges accepted
/// substreams to the configured Authority TCP socket. Returning means the Relay session ended;
/// callers may reconnect with a fresh session, while all prior streams remain terminated.
pub async fn serve_authority_tunnel_once(
    relay_endpoint: impl Into<String>,
    relay_tls: &MutualTlsClientConfig,
    authority_upstream: String,
    readiness: Arc<AtomicBool>,
) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    validate_fixed_authority_upstream(&authority_upstream)?;
    let authority_probe = tokio::time::timeout(
        Duration::from_secs(5),
        TcpStream::connect(authority_upstream.as_str()),
    )
    .await??;
    drop(authority_probe);
    let mut relay = WorkspaceRelayClient::connect_mtls(relay_endpoint, relay_tls).await?;
    let (out_tx, out_rx) = mpsc::channel(FRAME_QUEUE_CAPACITY);
    out_tx
        .send(TunnelFrame {
            frame_id: String::new(),
            payload: Some(Payload::RegisterAuthorityBridge(RegisterAuthorityBridge {
                target: TunnelTarget::Authority as i32,
            })),
        })
        .await?;
    let mut stream = relay.tunnel(ReceiverStream::new(out_rx)).await?;
    let registered = stream
        .message()
        .await?
        .ok_or("Relay closed before Authority Bridge registration completed")?;
    match registered.payload {
        Some(Payload::AuthorityBridgeRegistered(AuthorityBridgeRegistered {
            accepted: true,
            ..
        })) => info!("Authority Tunnel Bridge registered with Relay"),
        Some(Payload::AuthorityBridgeRegistered(result)) => {
            return Err(format!("Relay rejected Authority Bridge: {}", result.error_code).into());
        }
        _ => return Err("Relay returned an invalid bridge registration response".into()),
    }
    readiness.store(true, Ordering::Release);
    let _readiness_reset = ReadinessReset(Arc::clone(&readiness));

    let streams: Arc<Mutex<HashMap<String, mpsc::Sender<TunnelFrame>>>> =
        Arc::new(Mutex::new(HashMap::new()));
    let session_result: Result<(), Box<dyn std::error::Error + Send + Sync>> = async {
        while let Some(frame) = stream.message().await? {
            match frame.payload.as_ref() {
                Some(Payload::Accepted(accepted)) => {
                    if accepted.stream_id.is_empty() {
                        return Err("Relay accepted a tunnel without a stream ID".into());
                    }
                    let mut active = streams.lock().await;
                    if active.len() >= MAX_ACTIVE_STREAMS {
                        drop(active);
                        send_tunnel_error(
                            &out_tx,
                            accepted.stream_id.clone(),
                            "BRIDGE_STREAM_LIMIT",
                            "Authority Bridge active stream limit reached",
                            true,
                        )
                        .await;
                        continue;
                    }
                    if active.contains_key(&accepted.stream_id) {
                        return Err("Relay reused an active tunnel stream ID".into());
                    }
                    let stream_id = accepted.stream_id.clone();
                    let (stream_tx, stream_rx) = mpsc::channel(FRAME_QUEUE_CAPACITY);
                    active.insert(stream_id.clone(), stream_tx);
                    drop(active);
                    tokio::spawn(bridge_tcp_stream(
                        stream_id,
                        authority_upstream.clone(),
                        stream_rx,
                        out_tx.clone(),
                        Arc::clone(&streams),
                    ));
                }
                Some(Payload::Data(data)) => {
                    ensure_frame_size(&data.payload)?;
                    let stream_id = data.stream_id.clone();
                    send_to_stream(&streams, &stream_id, frame).await?;
                }
                Some(Payload::HalfClose(close)) => {
                    let stream_id = close.stream_id.clone();
                    send_to_stream(&streams, &stream_id, frame).await?;
                }
                Some(Payload::Closed(close)) => {
                    let stream_id = close.stream_id.clone();
                    send_to_stream(&streams, &stream_id, frame).await.ok();
                    streams.lock().await.remove(&stream_id);
                }
                Some(Payload::Error(error)) => {
                    let stream_id = error.stream_id.clone();
                    send_to_stream(&streams, &stream_id, frame).await.ok();
                    streams.lock().await.remove(&stream_id);
                }
                _ => return Err("Relay sent an invalid Authority Bridge frame".into()),
            }
        }
        Ok(())
    }
    .await;

    streams.lock().await.clear();
    warn!("Relay session disconnected; Authority tunnel streams were dropped without replay");
    session_result
}

struct ReadinessReset(Arc<AtomicBool>);

impl Drop for ReadinessReset {
    fn drop(&mut self) {
        self.0.store(false, Ordering::Release);
    }
}

async fn bridge_tcp_stream(
    stream_id: String,
    authority_upstream: String,
    mut tunnel_rx: mpsc::Receiver<TunnelFrame>,
    relay_tx: mpsc::Sender<TunnelFrame>,
    streams: Arc<Mutex<HashMap<String, mpsc::Sender<TunnelFrame>>>>,
) {
    let tcp = match tokio::time::timeout(
        Duration::from_secs(5),
        TcpStream::connect(authority_upstream.as_str()),
    )
    .await
    {
        Ok(Ok(tcp)) => tcp,
        Ok(Err(error)) => {
            send_tunnel_error(
                &relay_tx,
                stream_id.clone(),
                "AUTHORITY_UPSTREAM_UNAVAILABLE",
                &error.to_string(),
                true,
            )
            .await;
            streams.lock().await.remove(&stream_id);
            return;
        }
        Err(_) => {
            send_tunnel_error(
                &relay_tx,
                stream_id.clone(),
                "AUTHORITY_UPSTREAM_TIMEOUT",
                "Authority upstream connection timed out",
                true,
            )
            .await;
            streams.lock().await.remove(&stream_id);
            return;
        }
    };
    let (mut authority_reader, mut authority_writer) = tokio::io::split(tcp);
    let writer_stream_id = stream_id.clone();
    let writer_streams = Arc::clone(&streams);
    let (stop_reader_tx, mut stop_reader_rx) = oneshot::channel();
    let writer = tokio::spawn(async move {
        while let Some(frame) = tunnel_rx.recv().await {
            match frame.payload {
                Some(Payload::Data(data)) if data.stream_id == writer_stream_id => {
                    if ensure_frame_size(&data.payload).is_err()
                        || authority_writer.write_all(&data.payload).await.is_err()
                    {
                        break;
                    }
                }
                Some(Payload::HalfClose(close)) if close.stream_id == writer_stream_id => {
                    let _ = authority_writer.shutdown().await;
                }
                Some(Payload::Closed(close)) if close.stream_id == writer_stream_id => break,
                Some(Payload::Error(error)) if error.stream_id == writer_stream_id => break,
                _ => break,
            }
        }
        writer_streams.lock().await.remove(&writer_stream_id);
        let _ = stop_reader_tx.send(());
    });

    let mut buffer = vec![0; 16 * 1024];
    loop {
        let read_result = tokio::select! {
            _ = &mut stop_reader_rx => break,
            result = authority_reader.read(&mut buffer) => result,
        };
        match read_result {
            Ok(0) => {
                let _ = relay_tx
                    .send(TunnelFrame {
                        frame_id: String::new(),
                        payload: Some(Payload::HalfClose(TunnelHalfClose {
                            stream_id: stream_id.clone(),
                        })),
                    })
                    .await;
                break;
            }
            Ok(read) => {
                if relay_tx
                    .send(TunnelFrame {
                        frame_id: String::new(),
                        payload: Some(Payload::Data(TunnelData {
                            stream_id: stream_id.clone(),
                            payload: buffer[..read].to_vec(),
                        })),
                    })
                    .await
                    .is_err()
                {
                    break;
                }
            }
            Err(error) => {
                send_tunnel_error(
                    &relay_tx,
                    stream_id.clone(),
                    "AUTHORITY_UPSTREAM_READ_FAILED",
                    &error.to_string(),
                    true,
                )
                .await;
                break;
            }
        }
    }
    let _ = writer.await;
    streams.lock().await.remove(&stream_id);
}

async fn send_to_stream(
    streams: &Arc<Mutex<HashMap<String, mpsc::Sender<TunnelFrame>>>>,
    stream_id: &str,
    frame: TunnelFrame,
) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let sender = streams
        .lock()
        .await
        .get(stream_id)
        .cloned()
        .ok_or("Relay referenced an unknown tunnel stream")?;
    sender.send(frame).await?;
    Ok(())
}

async fn send_tunnel_error(
    relay_tx: &mpsc::Sender<TunnelFrame>,
    stream_id: String,
    code: &str,
    message: &str,
    retryable: bool,
) {
    let _ = relay_tx
        .send(TunnelFrame {
            frame_id: String::new(),
            payload: Some(Payload::Error(TunnelError {
                stream_id,
                code: code.to_string(),
                message: message.to_string(),
                retryable,
            })),
        })
        .await;
}

// Keep size failures in the same Tonic status form used by the tunnel handler.
#[allow(clippy::result_large_err)]
fn ensure_frame_size(payload: &[u8]) -> Result<(), Status> {
    if payload.len() > MAX_FRAME_BYTES {
        return Err(Status::resource_exhausted("tunnel frame exceeds 64 KiB"));
    }
    Ok(())
}

fn validate_fixed_authority_upstream(value: &str) -> Result<(), std::io::Error> {
    let Some((host, port)) = value.rsplit_once(':') else {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "Authority upstream must be a fixed host:port value",
        ));
    };
    if host.is_empty() || host.contains(['/', '@', '?', '#', ' ']) || port.parse::<u16>().is_err() {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "Authority upstream must be a fixed host:port value without URL syntax",
        ));
    }
    Ok(())
}
