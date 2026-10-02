//! ┌─────────────────────────────────────────────────────────────────────┐
//! │  📄 lib.rs                                                          │
//! │  Package: cyrene-workspace-relay                                    │
//! │  Role: Authenticated, bounded Authority byte-stream relay.          │
//! │                                                                     │
//! │  模块职责：按 mTLS peer 身份配对 Authority Bridge 与 Connector，     │
//! │  仅转发有界字节帧，不承载业务授权、队列或 HTTP 派发。                │
//! └─────────────────────────────────────────────────────────────────────┘

#![forbid(unsafe_code)]

use std::collections::{HashMap, HashSet};
use std::pin::Pin;
use std::sync::Arc;

pub use cyrene_plugin_contracts::workspace_tunnel_v1::{
    tunnel_frame::Payload,
    workspace_tunnel_service_server::{WorkspaceTunnelService, WorkspaceTunnelServiceServer},
    AuthorityBridgeRegistered, HealthCheckRequest, HealthCheckResponse, OpenTunnel,
    RegisterAuthorityBridge, TunnelAccepted, TunnelData, TunnelError, TunnelFrame, TunnelHalfClose,
    TunnelOpened, TunnelTarget,
};
use futures_util::StreamExt;
use tokio::sync::{mpsc, Mutex, RwLock};
use tokio_stream::wrappers::ReceiverStream;
use tonic::transport::server::TcpConnectInfo;
use tonic::transport::server::TlsConnectInfo;
use tonic::{Request, Response, Status, Streaming};
use tracing::{info, warn};
use x509_parser::prelude::{FromDer, GeneralName, X509Certificate};

const MAX_FRAME_BYTES: usize = 64 * 1024;
// With Connector SDK (8 x 16 KiB plus its 64 KiB duplex), Relay (3 x 64 KiB), and
// Bridge (two 3 x 64 KiB queues), queued application bytes peak at about 768 KiB per
// direction; three in-flight 64 KiB frames keep the path below 1 MiB per substream.
const FRAME_QUEUE_CAPACITY: usize = 3;
const MAX_ACTIVE_STREAMS: usize = 128;

pub type ResponseStream =
    Pin<Box<dyn tokio_stream::Stream<Item = Result<TunnelFrame, Status>> + Send + 'static>>;
type FrameSender = mpsc::Sender<Result<TunnelFrame, Status>>;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum PeerRole {
    AuthorityBridge,
    Connector,
}

#[derive(Default)]
struct RelayState {
    authority_bridge: Mutex<Option<FrameSender>>,
    connector_streams: RwLock<HashMap<String, FrameSender>>,
}

/// Protected exact certificate SAN mappings that grant tunnel roles.
#[derive(Clone, Debug)]
pub struct PeerIdentityAllowlist {
    authority_bridge_sans: HashSet<String>,
    connector_sans: HashSet<String>,
}

impl PeerIdentityAllowlist {
    /// Creates a role mapping from deployment-controlled SAN values.
    pub fn new(
        authority_bridge_sans: impl IntoIterator<Item = String>,
        connector_sans: impl IntoIterator<Item = String>,
    ) -> Result<Self, &'static str> {
        let authority_bridge_sans: HashSet<_> = authority_bridge_sans
            .into_iter()
            .filter(|identity| !identity.trim().is_empty())
            .collect();
        let connector_sans: HashSet<_> = connector_sans
            .into_iter()
            .filter(|identity| !identity.trim().is_empty())
            .collect();
        if authority_bridge_sans.is_empty() || connector_sans.is_empty() {
            return Err("both Authority Bridge and Connector SAN allowlists are required");
        }
        if !authority_bridge_sans.is_disjoint(&connector_sans) {
            return Err("a certificate SAN cannot grant both tunnel roles");
        }
        Ok(Self {
            authority_bridge_sans,
            connector_sans,
        })
    }
}

/// Authenticated, in-memory router for Authority Tonic TLS byte streams.
#[derive(Clone)]
pub struct WorkspaceRelay {
    state: Arc<RelayState>,
    peer_allowlist: Arc<PeerIdentityAllowlist>,
    max_active_streams: usize,
}

impl WorkspaceRelay {
    /// Creates an empty relay with no registered Authority Bridge.
    pub fn new(peer_allowlist: PeerIdentityAllowlist) -> Self {
        Self {
            state: Arc::default(),
            peer_allowlist: Arc::new(peer_allowlist),
            max_active_streams: MAX_ACTIVE_STREAMS,
        }
    }

    async fn bridge_sender(&self) -> Option<FrameSender> {
        self.state.authority_bridge.lock().await.clone()
    }

    /// Reports whether the fixed Authority Bridge currently has a live Relay stream.
    pub async fn authority_bridge_registered(&self) -> bool {
        self.bridge_sender()
            .await
            .is_some_and(|sender| !sender.is_closed())
    }

    async fn send_to_bridge(&self, frame: TunnelFrame) -> Result<(), Status> {
        let sender = self
            .bridge_sender()
            .await
            .ok_or_else(|| Status::unavailable("Authority Bridge is not registered"))?;
        sender
            .send(Ok(frame))
            .await
            .map_err(|_| Status::unavailable("Authority Bridge stream is disconnected"))
    }

    async fn send_to_connector(&self, stream_id: &str, frame: TunnelFrame) -> Result<(), Status> {
        let sender = self
            .state
            .connector_streams
            .read()
            .await
            .get(stream_id)
            .cloned()
            .ok_or_else(|| Status::not_found("tunnel stream is not active"))?;
        sender
            .send(Ok(frame))
            .await
            .map_err(|_| Status::unavailable("Connector tunnel stream is disconnected"))
    }

    async fn close_connector_stream(&self, stream_id: &str, reason: &str) {
        self.state.connector_streams.write().await.remove(stream_id);
        let frame = frame(Payload::Closed(
            cyrene_plugin_contracts::workspace_tunnel_v1::TunnelClosed {
                stream_id: stream_id.to_string(),
            },
        ));
        if let Err(error) = self.send_to_bridge(frame).await {
            warn!(stream_id, %error, reason, "Could not notify Authority Bridge about closed tunnel");
        }
    }
}

#[tonic::async_trait]
impl WorkspaceTunnelService for WorkspaceRelay {
    type TunnelStream = ResponseStream;

    async fn tunnel(
        &self,
        request: Request<Streaming<TunnelFrame>>,
    ) -> Result<Response<Self::TunnelStream>, Status> {
        let role = authenticated_peer_role(&request, &self.peer_allowlist)?;
        let mut input = request.into_inner();
        let first = input
            .message()
            .await?
            .ok_or_else(|| Status::invalid_argument("first tunnel frame is required"))?;
        let (out_tx, out_rx) = mpsc::channel(FRAME_QUEUE_CAPACITY);

        match (role, first.payload) {
            (PeerRole::AuthorityBridge, Some(Payload::RegisterAuthorityBridge(register))) => {
                self.register_authority_bridge(register, out_tx.clone())
                    .await?;
                let relay = self.clone();
                tokio::spawn(async move {
                    if let Err(error) = relay.serve_authority_bridge(input, out_tx).await {
                        warn!(%error, "Authority Bridge tunnel ended");
                    }
                });
            }
            (PeerRole::Connector, Some(Payload::OpenTunnel(open))) => {
                let stream_id = self.open_connector_tunnel(open, out_tx.clone()).await?;
                let relay = self.clone();
                tokio::spawn(async move {
                    if let Err(error) = relay.serve_connector_stream(stream_id, input, out_tx).await
                    {
                        warn!(%error, "Connector tunnel ended");
                    }
                });
            }
            (PeerRole::AuthorityBridge, _) => {
                return Err(Status::permission_denied(
                    "Authority Bridge must register as the first tunnel frame",
                ));
            }
            (PeerRole::Connector, _) => {
                return Err(Status::permission_denied(
                    "Connector must open an Authority tunnel as the first frame",
                ));
            }
        }

        Ok(Response::new(Box::pin(ReceiverStream::new(out_rx))))
    }

    async fn health_check(
        &self,
        request: Request<HealthCheckRequest>,
    ) -> Result<Response<HealthCheckResponse>, Status> {
        authenticated_peer_role(&request, &self.peer_allowlist)?;
        let bridge_registered = self.authority_bridge_registered().await;
        Ok(Response::new(HealthCheckResponse {
            ready: bridge_registered,
            authority_bridge_registered: bridge_registered,
        }))
    }
}

impl WorkspaceRelay {
    async fn register_authority_bridge(
        &self,
        register: RegisterAuthorityBridge,
        out_tx: FrameSender,
    ) -> Result<(), Status> {
        if TunnelTarget::try_from(register.target).ok() != Some(TunnelTarget::Authority) {
            return Err(Status::permission_denied(
                "only the Authority target is supported",
            ));
        }

        {
            let mut bridge = self.state.authority_bridge.lock().await;
            if bridge.as_ref().is_some_and(|sender| !sender.is_closed()) {
                return Err(Status::already_exists(
                    "an Authority Bridge is already registered",
                ));
            }
            *bridge = Some(out_tx.clone());
        }
        out_tx
            .send(Ok(frame(Payload::AuthorityBridgeRegistered(
                AuthorityBridgeRegistered {
                    accepted: true,
                    error_code: String::new(),
                },
            ))))
            .await
            .map_err(|_| Status::unavailable("tunnel response was closed"))?;

        info!("Authenticated Authority Bridge registered");
        Ok(())
    }

    async fn serve_authority_bridge(
        &self,
        mut input: Streaming<TunnelFrame>,
        out_tx: FrameSender,
    ) -> Result<(), Status> {
        while let Some(incoming) = input.next().await {
            let incoming = match incoming {
                Ok(incoming) => incoming,
                Err(error) => {
                    warn!(%error, "Authority Bridge tunnel input failed");
                    break;
                }
            };
            if let Err(error) = validate_data_frame(&incoming) {
                warn!(%error, "Authority Bridge sent a malformed tunnel frame");
                break;
            }
            let stream_id = match incoming.payload.as_ref() {
                Some(Payload::Data(data)) => data.stream_id.clone(),
                Some(Payload::HalfClose(close)) => close.stream_id.clone(),
                Some(Payload::Closed(close)) => close.stream_id.clone(),
                Some(Payload::Error(error)) => error.stream_id.clone(),
                _ => {
                    warn!("Authority Bridge sent a frame without a valid stream identifier");
                    break;
                }
            };
            let terminal = matches!(
                incoming.payload.as_ref(),
                Some(Payload::Closed(_)) | Some(Payload::Error(_))
            );
            if let Err(error) = self.send_to_connector(&stream_id, incoming).await {
                warn!(%stream_id, %error, "Dropping Bridge frame for an inactive tunnel");
                continue;
            }
            if terminal {
                self.state
                    .connector_streams
                    .write()
                    .await
                    .remove(&stream_id);
            }
        }

        let mut bridge = self.state.authority_bridge.lock().await;
        if bridge
            .as_ref()
            .is_some_and(|sender| sender.same_channel(&out_tx))
        {
            *bridge = None;
        }
        let streams = std::mem::take(&mut *self.state.connector_streams.write().await);
        for (stream_id, sender) in streams {
            let _ = sender
                .send(Ok(frame(Payload::Error(TunnelError {
                    stream_id,
                    code: "AUTHORITY_BRIDGE_DISCONNECTED".to_string(),
                    message: "Authority Bridge stream disconnected".to_string(),
                    retryable: true,
                }))))
                .await;
        }
        warn!("Authority Bridge disconnected; active tunnels were closed without replay");
        Ok(())
    }

    async fn open_connector_tunnel(
        &self,
        open: OpenTunnel,
        out_tx: FrameSender,
    ) -> Result<String, Status> {
        if TunnelTarget::try_from(open.target).ok() != Some(TunnelTarget::Authority) {
            return Err(Status::permission_denied(
                "only the Authority target is supported",
            ));
        }
        let bridge = self
            .bridge_sender()
            .await
            .filter(|sender| !sender.is_closed())
            .ok_or_else(|| Status::unavailable("Authority Bridge is not registered"))?;
        let stream_id = uuid::Uuid::new_v4().to_string();
        {
            let mut streams = self.state.connector_streams.write().await;
            if streams.len() >= self.max_active_streams {
                return Err(Status::resource_exhausted("active tunnel limit reached"));
            }
            streams.insert(stream_id.clone(), out_tx.clone());
        }

        if bridge
            .send(Ok(frame(Payload::Accepted(TunnelAccepted {
                stream_id: stream_id.clone(),
            }))))
            .await
            .is_err()
        {
            self.state
                .connector_streams
                .write()
                .await
                .remove(&stream_id);
            return Err(Status::unavailable(
                "Authority Bridge stream is disconnected",
            ));
        }
        if out_tx
            .send(Ok(frame(Payload::Opened(TunnelOpened {
                stream_id: stream_id.clone(),
            }))))
            .await
            .is_err()
        {
            self.state
                .connector_streams
                .write()
                .await
                .remove(&stream_id);
            let _ = bridge
                .send(Ok(frame(Payload::Closed(
                    cyrene_plugin_contracts::workspace_tunnel_v1::TunnelClosed {
                        stream_id: stream_id.clone(),
                    },
                ))))
                .await;
            return Err(Status::unavailable("Connector response stream was closed"));
        }
        Ok(stream_id)
    }

    async fn serve_connector_stream(
        &self,
        stream_id: String,
        mut input: Streaming<TunnelFrame>,
        out_tx: FrameSender,
    ) -> Result<(), Status> {
        let mut terminal_frame_received = false;
        while let Some(incoming) = input.next().await {
            let incoming = match incoming {
                Ok(incoming) => incoming,
                Err(error) => {
                    warn!(stream_id, %error, "Connector tunnel input failed");
                    break;
                }
            };
            if let Err(error) = validate_stream_frame(&incoming, &stream_id) {
                let error_frame = frame(Payload::Error(TunnelError {
                    stream_id: stream_id.clone(),
                    code: "INVALID_TUNNEL_FRAME".to_string(),
                    message: error.message().to_string(),
                    retryable: false,
                }));
                let _ = self.send_to_bridge(error_frame.clone()).await;
                let _ = out_tx.send(Ok(error_frame)).await;
                terminal_frame_received = true;
                break;
            }
            let terminal = matches!(
                incoming.payload.as_ref(),
                Some(Payload::Closed(_)) | Some(Payload::Error(_))
            );
            if let Err(error) = self.send_to_bridge(incoming).await {
                let error_frame = frame(Payload::Error(TunnelError {
                    stream_id: stream_id.clone(),
                    code: "AUTHORITY_BRIDGE_DISCONNECTED".to_string(),
                    message: error.message().to_string(),
                    retryable: true,
                }));
                let _ = out_tx.send(Ok(error_frame)).await;
                terminal_frame_received = true;
                break;
            }
            if terminal {
                terminal_frame_received = true;
                break;
            }
        }
        if terminal_frame_received {
            self.state
                .connector_streams
                .write()
                .await
                .remove(&stream_id);
        } else {
            self.close_connector_stream(&stream_id, "connector disconnected")
                .await;
        }
        Ok(())
    }
}

fn frame(payload: Payload) -> TunnelFrame {
    TunnelFrame {
        frame_id: String::new(),
        payload: Some(payload),
    }
}

// Tonic service handlers use `Status` as their required protocol error type.
#[allow(clippy::result_large_err)]
fn validate_data_frame(frame: &TunnelFrame) -> Result<(), Status> {
    match &frame.payload {
        Some(Payload::Data(data)) if data.payload.len() > MAX_FRAME_BYTES => {
            Err(Status::resource_exhausted("tunnel frame exceeds 64 KiB"))
        }
        Some(Payload::Data(_) | Payload::HalfClose(_) | Payload::Closed(_) | Payload::Error(_)) => {
            Ok(())
        }
        _ => Err(Status::invalid_argument(
            "unexpected frame after tunnel registration",
        )),
    }
}

// Tonic service handlers use `Status` as their required protocol error type.
#[allow(clippy::result_large_err)]
fn validate_stream_frame(frame: &TunnelFrame, expected_stream_id: &str) -> Result<(), Status> {
    validate_data_frame(frame)?;
    let actual_stream_id = match &frame.payload {
        Some(Payload::Data(data)) => &data.stream_id,
        Some(Payload::HalfClose(close)) => &close.stream_id,
        Some(Payload::Closed(close)) => &close.stream_id,
        Some(Payload::Error(error)) => &error.stream_id,
        _ => return Err(Status::invalid_argument("invalid tunnel data frame")),
    };
    if actual_stream_id != expected_stream_id {
        return Err(Status::failed_precondition(
            "frame stream_id does not match opened tunnel",
        ));
    }
    Ok(())
}

// This helper returns tonic's wire-level authentication failures unchanged.
#[allow(clippy::result_large_err)]
fn authenticated_peer_role<T>(
    request: &Request<T>,
    allowlist: &PeerIdentityAllowlist,
) -> Result<PeerRole, Status> {
    let tls = request
        .extensions()
        .get::<TlsConnectInfo<TcpConnectInfo>>()
        .ok_or_else(|| Status::unauthenticated("mTLS peer identity is required"))?;
    let certs = tls
        .peer_certs()
        .ok_or_else(|| Status::unauthenticated("mTLS client certificate is required"))?;
    let leaf = certs
        .first()
        .ok_or_else(|| Status::unauthenticated("mTLS client certificate chain is empty"))?;
    let (_, certificate) = X509Certificate::from_der(leaf.as_ref())
        .map_err(|_| Status::unauthenticated("mTLS client certificate is malformed"))?;
    let sans = certificate
        .subject_alternative_name()
        .map_err(|_| Status::unauthenticated("mTLS certificate SAN is malformed"))?
        .ok_or_else(|| Status::unauthenticated("mTLS certificate SAN is required"))?;
    let identities: Vec<&str> = sans
        .value
        .general_names
        .iter()
        .filter_map(|name| match name {
            GeneralName::URI(uri) => Some(*uri),
            GeneralName::DNSName(name) => Some(*name),
            _ => None,
        })
        .collect();
    let bridge_match = identities
        .iter()
        .any(|identity| allowlist.authority_bridge_sans.contains(*identity));
    let connector_match = identities
        .iter()
        .any(|identity| allowlist.connector_sans.contains(*identity));
    match (bridge_match, connector_match) {
        (true, false) => Ok(PeerRole::AuthorityBridge),
        (false, true) => Ok(PeerRole::Connector),
        (true, true) => Err(Status::permission_denied(
            "mTLS peer SAN maps to conflicting tunnel roles",
        )),
        (false, false) => Err(Status::permission_denied(
            "mTLS peer SAN is not an allowed tunnel identity",
        )),
    }
}
