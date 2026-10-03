//! ┌─────────────────────────────────────────────────────────────────────┐
//! │  📄 lib.rs                                                          │
//! │  Package: cyrene-workspace-client-sdk                              │
//! │  Role: Client SDK for Workspace Authority and Bridge RPCs.          │
//! │                                                                     │
//! │  模块职责：用于连接 Workspace Authority 与 Frontend Bridge 的轻量 SDK。│
//! └─────────────────────────────────────────────────────────────────────┘

#![forbid(unsafe_code)]

use std::path::Path;
use std::time::Duration;

pub use cyrene_plugin_contracts::workspace_authority_v1 as authority;
pub use cyrene_plugin_contracts::workspace_authority_v2 as authority_v2;
pub use cyrene_plugin_contracts::workspace_bridge_v1 as bridge;
pub use cyrene_plugin_contracts::workspace_product_v2 as product;

use authority_v2::workspace_authority_service_client::WorkspaceAuthorityServiceClient;
use bridge::workspace_frontend_bridge_service_client::WorkspaceFrontendBridgeServiceClient;
use cyrene_plugin_contracts::workspace_tunnel_v1::{
    tunnel_frame::Payload as TunnelPayload,
    workspace_tunnel_service_client::WorkspaceTunnelServiceClient, HealthCheckRequest,
    HealthCheckResponse, OpenTunnel, TunnelData, TunnelFrame, TunnelHalfClose, TunnelTarget,
};
use thiserror::Error;
use tokio::io::{AsyncReadExt, AsyncWriteExt, DuplexStream};
use tokio::sync::mpsc;
use tokio_stream::wrappers::ReceiverStream;
use tonic::transport::{Certificate, Channel, ClientTlsConfig, Endpoint, Identity, Uri};
use tonic::Request;
use tower::service_fn;

const MAX_TUNNEL_MESSAGE_BYTES: usize = 64 * 1024 + 1024;
const TUNNEL_SEND_QUEUE_CAPACITY: usize = 8;

#[derive(Debug, Error)]
pub enum ClientSdkError {
    #[error("TRANSPORT_ERROR: {0}")]
    Transport(String),
    #[error("RPC_ERROR: {0}")]
    Rpc(Box<tonic::Status>),
    #[error("INVALID_ENDPOINT: {0}")]
    InvalidEndpoint(String),
    #[error("MUTUAL_TLS_REQUIRED: network RPC connections require configured CA, SNI, and client identity")]
    MutualTlsRequired,
    #[error("INVALID_CREDENTIAL: {0}")]
    InvalidCredential(String),
    #[error("PROTOCOL_VERSION_MISMATCH: Authority selected v{selected}, expected v2")]
    ProtocolVersionMismatch { selected: u32 },
}

impl From<tonic::Status> for ClientSdkError {
    fn from(status: tonic::Status) -> Self {
        Self::Rpc(Box::new(status))
    }
}

/// TLS material used for authenticated network connections to Workspace services.
/// The CA verifies the server chain and `server_name` verifies its certificate identity.
#[derive(Clone)]
pub struct MutualTlsClientConfig {
    ca_certificate_pem: Vec<u8>,
    server_name: String,
    client_certificate_pem: Vec<u8>,
    client_private_key_pem: Vec<u8>,
}

impl MutualTlsClientConfig {
    /// Builds a client identity from PEM-encoded trust and client credentials.
    pub fn from_pem(
        ca_certificate_pem: impl Into<Vec<u8>>,
        server_name: impl Into<String>,
        client_certificate_pem: impl Into<Vec<u8>>,
        client_private_key_pem: impl Into<Vec<u8>>,
    ) -> Result<Self, ClientSdkError> {
        let config = Self {
            ca_certificate_pem: ca_certificate_pem.into(),
            server_name: server_name.into(),
            client_certificate_pem: client_certificate_pem.into(),
            client_private_key_pem: client_private_key_pem.into(),
        };
        if config.ca_certificate_pem.is_empty()
            || config.client_certificate_pem.is_empty()
            || config.client_private_key_pem.is_empty()
            || config.server_name.trim().is_empty()
            || !config
                .server_name
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'-'))
        {
            return Err(ClientSdkError::InvalidEndpoint(
                "mTLS requires non-empty PEM credentials and a DNS/IP server name".to_string(),
            ));
        }
        Ok(config)
    }

    /// Loads CA, client certificate, and client key PEM files from disk.
    pub fn from_pem_files(
        ca_certificate_path: impl AsRef<Path>,
        server_name: impl Into<String>,
        client_certificate_path: impl AsRef<Path>,
        client_private_key_path: impl AsRef<Path>,
    ) -> Result<Self, ClientSdkError> {
        let read = |path: &Path| {
            std::fs::read(path).map_err(|error| ClientSdkError::Transport(error.to_string()))
        };
        Self::from_pem(
            read(ca_certificate_path.as_ref())?,
            server_name,
            read(client_certificate_path.as_ref())?,
            read(client_private_key_path.as_ref())?,
        )
    }

    fn tonic_config(&self) -> ClientTlsConfig {
        ClientTlsConfig::new()
            .ca_certificate(Certificate::from_pem(self.ca_certificate_pem.clone()))
            .domain_name(self.server_name.clone())
            .identity(Identity::from_pem(
                self.client_certificate_pem.clone(),
                self.client_private_key_pem.clone(),
            ))
    }
}

fn secure_endpoint(
    endpoint: impl Into<String>,
    tls: &MutualTlsClientConfig,
) -> Result<Endpoint, ClientSdkError> {
    let endpoint = endpoint.into();
    if !endpoint.starts_with("https://") {
        return Err(ClientSdkError::InvalidEndpoint(
            "mTLS endpoint must use https://".to_string(),
        ));
    }
    Endpoint::from_shared(endpoint)
        .map(|endpoint| {
            endpoint
                .connect_timeout(Duration::from_secs(5))
                .http2_keep_alive_interval(Duration::from_secs(15))
                .keep_alive_timeout(Duration::from_secs(5))
                .keep_alive_while_idle(true)
        })
        .map_err(|error| ClientSdkError::InvalidEndpoint(error.to_string()))?
        .tls_config(tls.tonic_config())
        .map_err(|error| ClientSdkError::InvalidEndpoint(error.to_string()))
}

async fn connect_mtls(
    endpoint: impl Into<String>,
    tls: &MutualTlsClientConfig,
) -> Result<Channel, ClientSdkError> {
    secure_endpoint(endpoint, tls)?
        .connect()
        .await
        .map_err(|error| ClientSdkError::Transport(error.to_string()))
}

fn bearer_request<T>(message: T, bearer_token: &str) -> Result<Request<T>, ClientSdkError> {
    let value = tonic::metadata::MetadataValue::try_from(format!("Bearer {bearer_token}"))
        .map_err(|error| ClientSdkError::InvalidCredential(error.to_string()))?;
    let mut request = Request::new(message);
    request.metadata_mut().insert("authorization", value);
    Ok(request)
}

async fn open_relay_tunnel(
    relay_endpoint: String,
    relay_tls: MutualTlsClientConfig,
) -> Result<DuplexStream, std::io::Error> {
    let relay_channel = connect_mtls(relay_endpoint, &relay_tls)
        .await
        .map_err(std::io::Error::other)?;
    // TunnelData is emitted in 16 KiB chunks, so this queue buffers at most 128 KiB.
    let (out_tx, out_rx) = mpsc::channel(TUNNEL_SEND_QUEUE_CAPACITY);
    out_tx
        .send(TunnelFrame {
            frame_id: String::new(),
            payload: Some(TunnelPayload::OpenTunnel(OpenTunnel {
                target: TunnelTarget::Authority as i32,
            })),
        })
        .await
        .map_err(std::io::Error::other)?;
    let mut client = WorkspaceTunnelServiceClient::new(relay_channel)
        .max_decoding_message_size(MAX_TUNNEL_MESSAGE_BYTES)
        .max_encoding_message_size(MAX_TUNNEL_MESSAGE_BYTES);
    let mut response = client
        .tunnel(Request::new(ReceiverStream::new(out_rx)))
        .await
        .map_err(std::io::Error::other)?
        .into_inner();
    let opened = response
        .message()
        .await
        .map_err(std::io::Error::other)?
        .ok_or_else(|| std::io::Error::other("relay closed before opening authority tunnel"))?;
    let stream_id = match opened.payload {
        Some(TunnelPayload::Opened(opened)) if !opened.stream_id.is_empty() => opened.stream_id,
        Some(TunnelPayload::Error(error)) => {
            return Err(std::io::Error::other(format!(
                "relay rejected tunnel ({}): {}",
                error.code, error.message
            )));
        }
        _ => {
            return Err(std::io::Error::other(
                "relay returned an invalid tunnel response",
            ))
        }
    };
    let (application_io, relay_io) = tokio::io::duplex(64 * 1024);
    let (mut relay_reader, mut relay_writer) = tokio::io::split(relay_io);
    let send_tx = out_tx.clone();
    let send_stream_id = stream_id.clone();
    tokio::spawn(async move {
        let mut buffer = vec![0; 16 * 1024];
        loop {
            match relay_reader.read(&mut buffer).await {
                Ok(0) => {
                    let _ = send_tx
                        .send(TunnelFrame {
                            frame_id: String::new(),
                            payload: Some(TunnelPayload::HalfClose(TunnelHalfClose {
                                stream_id: send_stream_id,
                            })),
                        })
                        .await;
                    break;
                }
                Ok(read) => {
                    if send_tx
                        .send(TunnelFrame {
                            frame_id: String::new(),
                            payload: Some(TunnelPayload::Data(TunnelData {
                                stream_id: send_stream_id.clone(),
                                payload: buffer[..read].to_vec(),
                            })),
                        })
                        .await
                        .is_err()
                    {
                        break;
                    }
                }
                Err(_) => break,
            }
        }
    });

    tokio::spawn(async move {
        while let Ok(Some(frame)) = response.message().await {
            match frame.payload {
                Some(TunnelPayload::Data(data))
                    if data.stream_id == stream_id && data.payload.len() <= 64 * 1024 =>
                {
                    if relay_writer.write_all(&data.payload).await.is_err() {
                        break;
                    }
                }
                Some(TunnelPayload::HalfClose(close)) if close.stream_id == stream_id => {
                    let _ = relay_writer.shutdown().await;
                }
                Some(TunnelPayload::Closed(closed)) if closed.stream_id == stream_id => {
                    let _ = relay_writer.shutdown().await;
                    break;
                }
                Some(TunnelPayload::Error(error)) if error.stream_id == stream_id => {
                    let _ = relay_writer.shutdown().await;
                    break;
                }
                _ => {
                    let _ = relay_writer.shutdown().await;
                    break;
                }
            }
        }
        let _ = relay_writer.shutdown().await;
    });

    Ok(application_io)
}

/// Client for connecting to Platform Workspace Authority Service.
#[derive(Clone)]
pub struct AuthorityClient {
    inner: WorkspaceAuthorityServiceClient<Channel>,
}

impl AuthorityClient {
    /// Legacy plaintext network connection. Network RPC requires [`Self::connect_via_relay`].
    pub async fn connect(endpoint: impl Into<String>) -> Result<Self, ClientSdkError> {
        let _ = endpoint.into();
        Err(ClientSdkError::MutualTlsRequired)
    }

    /// Connects to Authority through the authenticated Relay byte tunnel.
    /// Authority TLS remains end-to-end inside the tunnel; this method never falls back to a
    /// direct Authority socket when Relay is unavailable.
    pub async fn connect_via_relay(
        relay_endpoint: impl Into<String>,
        relay_tls: &MutualTlsClientConfig,
        authority_tls: &MutualTlsClientConfig,
    ) -> Result<Self, ClientSdkError> {
        let relay_endpoint = relay_endpoint.into();
        let endpoint = Endpoint::from_shared("https://authority.cyrene.invalid")
            .map_err(|error| ClientSdkError::InvalidEndpoint(error.to_string()))?
            .tls_config(authority_tls.tonic_config())
            .map_err(|error| ClientSdkError::InvalidEndpoint(error.to_string()))?
            .connect_timeout(Duration::from_secs(5))
            .http2_keep_alive_interval(Duration::from_secs(15))
            .keep_alive_timeout(Duration::from_secs(5))
            .keep_alive_while_idle(true);
        let relay_tls = relay_tls.clone();
        let channel = endpoint
            .connect_with_connector(service_fn(move |_: Uri| {
                let relay_endpoint = relay_endpoint.clone();
                let relay_tls = relay_tls.clone();
                async move {
                    open_relay_tunnel(relay_endpoint, relay_tls)
                        .await
                        .map(hyper_util::rt::tokio::TokioIo::new)
                }
            }))
            .await
            .map_err(|error| ClientSdkError::Transport(error.to_string()))?;
        let mut inner = WorkspaceAuthorityServiceClient::new(channel);
        let negotiated = inner
            .negotiate_version(authority_v2::NegotiateVersionRequest {
                minimum_version: 2,
                maximum_version: 2,
            })
            .await?
            .into_inner();
        if negotiated.selected_version != 2 {
            return Err(ClientSdkError::ProtocolVersionMismatch {
                selected: negotiated.selected_version,
            });
        }
        Ok(Self { inner })
    }

    /// Connect to Authority over a local Unix Domain Socket (UDS).
    #[cfg(unix)]
    pub async fn connect_uds(socket_path: impl AsRef<Path>) -> Result<Self, ClientSdkError> {
        let path = socket_path.as_ref().to_path_buf();
        let channel = Endpoint::try_from("http://[::]:50051")
            .map_err(|e| ClientSdkError::InvalidEndpoint(e.to_string()))?
            .connect_with_connector(service_fn(move |_: Uri| {
                let p = path.clone();
                async move {
                    let stream = tokio::net::UnixStream::connect(p).await?;
                    Ok::<_, std::io::Error>(hyper_util::rt::tokio::TokioIo::new(stream))
                }
            }))
            .await
            .map_err(|e| ClientSdkError::Transport(e.to_string()))?;

        let mut inner = WorkspaceAuthorityServiceClient::new(channel);
        let negotiated = inner
            .negotiate_version(authority_v2::NegotiateVersionRequest {
                minimum_version: 2,
                maximum_version: 2,
            })
            .await?;
        let selected_version = negotiated.get_ref().selected_version;
        if selected_version != 2 {
            return Err(ClientSdkError::ProtocolVersionMismatch {
                selected: selected_version,
            });
        }
        Ok(Self { inner })
    }

    pub async fn negotiate_version(
        &mut self,
        req: authority_v2::NegotiateVersionRequest,
    ) -> Result<authority_v2::NegotiateVersionResponse, ClientSdkError> {
        let resp = self.inner.negotiate_version(req).await?;
        Ok(resp.into_inner())
    }

    pub async fn verify_identity(
        &mut self,
        req: authority_v2::VerifyIdentityRequest,
    ) -> Result<authority_v2::VerifyIdentityResponse, ClientSdkError> {
        let resp = self.inner.verify_identity(req).await?;
        Ok(resp.into_inner())
    }

    /// Verifies a web principal using the bearer token in gRPC authorization metadata.
    pub async fn verify_identity_with_bearer(
        &mut self,
        req: authority_v2::VerifyIdentityRequest,
        bearer_token: &str,
    ) -> Result<authority_v2::VerifyIdentityResponse, ClientSdkError> {
        let resp = self
            .inner
            .verify_identity(bearer_request(req, bearer_token)?)
            .await?;
        Ok(resp.into_inner())
    }

    pub async fn get_catalog_snapshot(
        &mut self,
        req: authority_v2::CatalogSnapshotRequest,
    ) -> Result<authority_v2::CatalogSnapshotResponse, ClientSdkError> {
        let resp = self.inner.get_catalog_snapshot(req).await?;
        Ok(resp.into_inner())
    }

    /// Reads the caller-authorized catalog snapshot using bearer metadata.
    pub async fn get_catalog_snapshot_with_bearer(
        &mut self,
        req: authority_v2::CatalogSnapshotRequest,
        bearer_token: &str,
    ) -> Result<authority_v2::CatalogSnapshotResponse, ClientSdkError> {
        let resp = self
            .inner
            .get_catalog_snapshot(bearer_request(req, bearer_token)?)
            .await?;
        Ok(resp.into_inner())
    }

    pub async fn approve_and_enqueue_invocation(
        &mut self,
        req: authority_v2::ApproveAndEnqueueInvocationRequest,
    ) -> Result<authority_v2::ApproveAndEnqueueInvocationResponse, ClientSdkError> {
        let resp = self.inner.approve_and_enqueue_invocation(req).await?;
        Ok(resp.into_inner())
    }

    /// Approves one Product invocation with the caller token in gRPC metadata.
    pub async fn approve_and_enqueue_invocation_with_bearer(
        &mut self,
        req: authority_v2::ApproveAndEnqueueInvocationRequest,
        bearer_token: &str,
    ) -> Result<authority_v2::ApproveAndEnqueueInvocationResponse, ClientSdkError> {
        let resp = self
            .inner
            .approve_and_enqueue_invocation(bearer_request(req, bearer_token)?)
            .await?;
        Ok(resp.into_inner())
    }

    pub async fn claim_invocations(
        &mut self,
        req: authority_v2::ClaimInvocationsRequest,
    ) -> Result<authority_v2::ClaimInvocationsResponse, ClientSdkError> {
        let resp = self.inner.claim_invocations(req).await?;
        Ok(resp.into_inner())
    }

    pub async fn validate_execution_authorization(
        &mut self,
        req: authority_v2::ValidateExecutionAuthorizationRequest,
    ) -> Result<authority_v2::ValidateExecutionAuthorizationResponse, ClientSdkError> {
        let resp = self.inner.validate_execution_authorization(req).await?;
        Ok(resp.into_inner())
    }

    pub async fn acknowledge_delivery(
        &mut self,
        req: authority_v2::AcknowledgeDeliveryRequest,
    ) -> Result<authority_v2::AcknowledgeDeliveryResponse, ClientSdkError> {
        let resp = self.inner.acknowledge_delivery(req).await?;
        Ok(resp.into_inner())
    }

    pub async fn submit_invocation_result(
        &mut self,
        req: authority_v2::SubmitInvocationResultRequest,
    ) -> Result<authority_v2::SubmitInvocationResultResponse, ClientSdkError> {
        let resp = self.inner.submit_invocation_result(req).await?;
        Ok(resp.into_inner())
    }

    pub async fn wait_invocation_result(
        &mut self,
        req: authority_v2::WaitInvocationResultRequest,
    ) -> Result<authority_v2::WaitInvocationResultResponse, ClientSdkError> {
        let resp = self.inner.wait_invocation_result(req).await?;
        Ok(resp.into_inner())
    }

    /// Waits for a durable outbox result using the original caller token in metadata.
    pub async fn wait_invocation_result_with_bearer(
        &mut self,
        req: authority_v2::WaitInvocationResultRequest,
        bearer_token: &str,
    ) -> Result<authority_v2::WaitInvocationResultResponse, ClientSdkError> {
        let resp = self
            .inner
            .wait_invocation_result(bearer_request(req, bearer_token)?)
            .await?;
        Ok(resp.into_inner())
    }
}

/// Client for connecting to the Plugins Frontend Bridge Service.
#[derive(Clone)]
pub struct FrontendBridgeClient {
    inner: WorkspaceFrontendBridgeServiceClient<Channel>,
}

/// mTLS-authenticated access to Relay readiness and tunnel control RPCs.
#[derive(Clone)]
pub struct WorkspaceRelayClient {
    inner: WorkspaceTunnelServiceClient<Channel>,
}

impl WorkspaceRelayClient {
    /// Connects with a verified Relay server identity and client certificate.
    pub async fn connect_mtls(
        endpoint: impl Into<String>,
        tls: &MutualTlsClientConfig,
    ) -> Result<Self, ClientSdkError> {
        let channel = connect_mtls(endpoint, tls).await?;
        Ok(Self {
            inner: WorkspaceTunnelServiceClient::new(channel)
                .max_decoding_message_size(MAX_TUNNEL_MESSAGE_BYTES)
                .max_encoding_message_size(MAX_TUNNEL_MESSAGE_BYTES),
        })
    }

    /// Returns true only when the Authority Bridge has a live registration.
    pub async fn health_check(&mut self) -> Result<HealthCheckResponse, ClientSdkError> {
        let response = self.inner.health_check(HealthCheckRequest {}).await?;
        Ok(response.into_inner())
    }

    /// Opens the bounded multiplexed tunnel stream used by Authority Bridge.
    pub async fn tunnel(
        &mut self,
        request: impl tonic::IntoStreamingRequest<Message = TunnelFrame>,
    ) -> Result<tonic::Streaming<TunnelFrame>, ClientSdkError> {
        let response = self.inner.tunnel(request).await?;
        Ok(response.into_inner())
    }
}

impl FrontendBridgeClient {
    /// Legacy plaintext network connection. Network RPC requires [`Self::connect_mtls`].
    pub async fn connect(endpoint: impl Into<String>) -> Result<Self, ClientSdkError> {
        let _ = endpoint.into();
        Err(ClientSdkError::MutualTlsRequired)
    }

    /// Connects to Frontend Bridge with server certificate validation and client authentication.
    pub async fn connect_mtls(
        endpoint: impl Into<String>,
        tls: &MutualTlsClientConfig,
    ) -> Result<Self, ClientSdkError> {
        let channel = connect_mtls(endpoint, tls).await?;
        Ok(Self {
            inner: WorkspaceFrontendBridgeServiceClient::new(channel),
        })
    }

    #[cfg(unix)]
    pub async fn connect_uds(socket_path: impl AsRef<Path>) -> Result<Self, ClientSdkError> {
        let path = socket_path.as_ref().to_path_buf();
        let channel = Endpoint::try_from("http://[::]:50051")
            .map_err(|e| ClientSdkError::InvalidEndpoint(e.to_string()))?
            .connect_with_connector(service_fn(move |_: Uri| {
                let p = path.clone();
                async move {
                    let stream = tokio::net::UnixStream::connect(p).await?;
                    Ok::<_, std::io::Error>(hyper_util::rt::tokio::TokioIo::new(stream))
                }
            }))
            .await
            .map_err(|e| ClientSdkError::Transport(e.to_string()))?;

        Ok(Self {
            inner: WorkspaceFrontendBridgeServiceClient::new(channel),
        })
    }

    pub async fn discover_workspaces(
        &mut self,
        req: bridge::BridgeDiscoverWorkspacesRequest,
    ) -> Result<bridge::BridgeDiscoverWorkspacesResponse, ClientSdkError> {
        let resp = self.inner.discover_workspaces(req).await?;
        Ok(resp.into_inner())
    }

    pub async fn execute_invocation(
        &mut self,
        req: bridge::BridgeExecuteInvocationRequest,
    ) -> Result<bridge::BridgeExecuteInvocationResponse, ClientSdkError> {
        let resp = self.inner.execute_invocation(req).await?;
        Ok(resp.into_inner())
    }
}
