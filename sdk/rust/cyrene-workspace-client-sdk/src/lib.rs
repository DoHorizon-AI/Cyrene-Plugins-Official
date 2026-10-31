//! ┌─────────────────────────────────────────────────────────────────────┐
//! │  📄 lib.rs                                                          │
//! │  Package: cyrene-workspace-client-sdk                              │
//! │  Role: Client SDK for Workspace Authority and Bridge RPCs.          │
//! │                                                                     │
//! │  模块职责：用于连接 Workspace Authority 与 Frontend Bridge 的轻量 SDK。│
//! └─────────────────────────────────────────────────────────────────────┘

#![forbid(unsafe_code)]

use std::path::Path;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

pub use cyrene_plugin_contracts::workspace_authority_v1 as authority;
pub use cyrene_plugin_contracts::workspace_authority_v2 as authority_v2;
pub use cyrene_plugin_contracts::workspace_bridge_v1 as bridge;
pub use cyrene_plugin_contracts::workspace_product_v2 as product;

use authority_v2::workspace_authority_service_client::WorkspaceAuthorityServiceClient;
use authority_v2::ManagedWorkloadFeatureToken;
use bridge::workspace_frontend_bridge_service_client::WorkspaceFrontendBridgeServiceClient;
use cyrene_plugin_contracts::workspace_tunnel_v1::{
    tunnel_frame::Payload as TunnelPayload,
    workspace_tunnel_service_client::WorkspaceTunnelServiceClient, HealthCheckRequest,
    HealthCheckResponse, OpenTunnel, TunnelData, TunnelFrame, TunnelHalfClose, TunnelTarget,
};
use prost::Message;
use thiserror::Error;
use tokio::io::{AsyncReadExt, AsyncWriteExt, DuplexStream};
use tokio::sync::mpsc;
use tokio_stream::wrappers::ReceiverStream;
use tonic::transport::{Certificate, Channel, ClientTlsConfig, Endpoint, Identity, Uri};
use tonic::Request;
use tower::service_fn;

const MAX_TUNNEL_MESSAGE_BYTES: usize = 64 * 1024 + 1024;
const TUNNEL_SEND_QUEUE_CAPACITY: usize = 8;
const DELEGATED_INVOCATIONS_FEATURE: &str = "delegated_invocations_v1";

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
    selected_features: Vec<String>,
    managed_workload_feature_token: Vec<u8>,
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
                requested_features: vec![DELEGATED_INVOCATIONS_FEATURE.to_string()],
            })
            .await?
            .into_inner();
        if negotiated.selected_version != 2 {
            return Err(ClientSdkError::ProtocolVersionMismatch {
                selected: negotiated.selected_version,
            });
        }
        Ok(Self {
            inner,
            selected_features: negotiated.selected_features,
            managed_workload_feature_token: negotiated.managed_workload_feature_token,
        })
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
                requested_features: vec![DELEGATED_INVOCATIONS_FEATURE.to_string()],
            })
            .await?;
        let selected_version = negotiated.get_ref().selected_version;
        if selected_version != 2 {
            return Err(ClientSdkError::ProtocolVersionMismatch {
                selected: selected_version,
            });
        }
        let negotiated = negotiated.into_inner();
        Ok(Self {
            inner,
            selected_features: negotiated.selected_features,
            managed_workload_feature_token: negotiated.managed_workload_feature_token,
        })
    }

    /// Returns whether Authority negotiated bounded delegated invocations.
    pub fn supports_delegated_invocations(&self) -> bool {
        self.selected_features
            .iter()
            .any(|feature| feature == DELEGATED_INVOCATIONS_FEATURE)
    }

    /// Returns the short-lived token bound to the authenticated workload profile.
    pub fn managed_workload_feature_token(&self) -> &[u8] {
        &self.managed_workload_feature_token
    }

    pub async fn negotiate_version(
        &mut self,
        req: authority_v2::NegotiateVersionRequest,
    ) -> Result<authority_v2::NegotiateVersionResponse, ClientSdkError> {
        let resp = self.inner.negotiate_version(req).await?;
        let response = resp.into_inner();
        self.selected_features = response.selected_features.clone();
        self.managed_workload_feature_token = response.managed_workload_feature_token.clone();
        Ok(response)
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

    /// Creates one bounded Product delegation using the verified human bearer.
    pub async fn create_delegation_with_bearer(
        &mut self,
        req: authority_v2::CreateDelegationRequest,
        bearer_token: &str,
    ) -> Result<authority_v2::CreateDelegationResponse, ClientSdkError> {
        let resp = self
            .inner
            .create_delegation(bearer_request(req, bearer_token)?)
            .await?;
        Ok(resp.into_inner())
    }

    /// Revokes one delegation using the verified human bearer.
    pub async fn revoke_delegation_with_bearer(
        &mut self,
        req: authority_v2::RevokeDelegationRequest,
        bearer_token: &str,
    ) -> Result<authority_v2::RevokeDelegationResponse, ClientSdkError> {
        let resp = self
            .inner
            .revoke_delegation(bearer_request(req, bearer_token)?)
            .await?;
        Ok(resp.into_inner())
    }

    /// Reads the caller's own delegation using the legacy interactive bearer path.
    /// The request deliberately carries no Product execution credential.
    pub async fn get_delegation_with_bearer(
        &mut self,
        req: authority_v2::GetDelegationRequest,
        bearer_token: &str,
    ) -> Result<authority_v2::GetDelegationResponse, ClientSdkError> {
        if req.execution_credential.is_some() {
            return Err(ClientSdkError::InvalidCredential(
                "bearer delegation reads cannot include a Product execution credential".into(),
            ));
        }
        let resp = self
            .inner
            .get_delegation(bearer_request(req, bearer_token)?)
            .await?;
        Ok(resp.into_inner())
    }

    /// Reads a delegation under an already-signed Product execution credential.
    /// The credential is passed unchanged for Authority validation; this method
    /// only rejects ambiguous request credentials, mismatched workspaces, and
    /// incomplete delegated-profile fields. Authority remains responsible for
    /// validating the signature, principal, active grant, and target grant.
    pub async fn get_delegation_with_execution_credential(
        &mut self,
        mut req: authority_v2::GetDelegationRequest,
        credential: authority_v2::ExecutionCredential,
    ) -> Result<authority_v2::GetDelegationResponse, ClientSdkError> {
        let is_delegated = credential.authorization_source
            == authority_v2::InvocationAuthorizationSource::Delegation as i32;
        let is_interactive = matches!(
            credential.authorization_source,
            value
                if value
                    == authority_v2::InvocationAuthorizationSource::InteractiveSession as i32
                    || value
                        == authority_v2::InvocationAuthorizationSource::Unspecified as i32
        );
        if req.execution_credential.is_some()
            || credential.workspace_id != req.workspace_id
            || (!is_delegated && !is_interactive)
            || (is_delegated
                && (credential.delegation_id.is_empty()
                    || credential.delegation_generation == 0
                    || credential.delegation_audience.is_empty()))
        {
            return Err(ClientSdkError::InvalidCredential(
                "Product execution credential is ambiguous or incomplete for this delegation read"
                    .into(),
            ));
        }
        req.execution_credential = Some(credential);
        let resp = self.inner.get_delegation(req).await?;
        Ok(resp.into_inner())
    }

    /// Enqueues an invocation under the managed workload token and delegation scope.
    pub async fn approve_and_enqueue_delegated_invocation(
        &mut self,
        mut req: authority_v2::ApproveAndEnqueueDelegatedInvocationRequest,
        expected_audience: &str,
    ) -> Result<authority_v2::ApproveAndEnqueueDelegatedInvocationResponse, ClientSdkError> {
        if expected_audience.trim().is_empty() {
            return Err(ClientSdkError::InvalidCredential(
                "delegated invocation audience is required".to_string(),
            ));
        }
        // A cached negotiation token can expire while this client stays open.
        // Refresh it immediately before each delegated enqueue.
        let negotiated = self
            .inner
            .negotiate_version(authority_v2::NegotiateVersionRequest {
                minimum_version: 2,
                maximum_version: 2,
                requested_features: vec![DELEGATED_INVOCATIONS_FEATURE.to_string()],
            })
            .await?
            .into_inner();
        if negotiated.selected_version != 2 || !negotiated.supported_versions.contains(&2) {
            self.selected_features.clear();
            self.managed_workload_feature_token.clear();
            return Err(ClientSdkError::ProtocolVersionMismatch {
                selected: negotiated.selected_version,
            });
        }
        self.selected_features = negotiated.selected_features;
        self.managed_workload_feature_token = negotiated.managed_workload_feature_token;
        if !self.supports_delegated_invocations() || self.managed_workload_feature_token.is_empty()
        {
            return Err(ClientSdkError::InvalidCredential(
                "Authority did not negotiate delegated invocation support".to_string(),
            ));
        }
        validate_managed_workload_feature_token(
            &self.managed_workload_feature_token,
            expected_audience,
        )?;
        req.managed_workload_feature_token = self.managed_workload_feature_token.clone();
        let resp = self
            .inner
            .approve_and_enqueue_delegated_invocation(req)
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

    /// Revalidates a Product workload execution credential over authenticated mTLS.
    pub async fn validate_product_execution_authorization(
        &mut self,
        req: authority_v2::ValidateProductExecutionAuthorizationRequest,
    ) -> Result<authority_v2::ValidateProductExecutionAuthorizationResponse, ClientSdkError> {
        let resp = self
            .inner
            .validate_product_execution_authorization(req)
            .await?;
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

fn validate_managed_workload_feature_token(
    encoded: &[u8],
    expected_audience: &str,
) -> Result<(), ClientSdkError> {
    let token = ManagedWorkloadFeatureToken::decode(encoded)
        .map_err(|error| ClientSdkError::InvalidCredential(error.to_string()))?;
    let issued = token.issued_at.as_ref().ok_or_else(|| {
        ClientSdkError::InvalidCredential("feature token issue time is missing".into())
    })?;
    let expires = token.expires_at.as_ref().ok_or_else(|| {
        ClientSdkError::InvalidCredential("feature token expiry is missing".into())
    })?;
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|error| ClientSdkError::InvalidCredential(error.to_string()))?
        .as_secs() as i64;
    if token.audience != expected_audience
        || token.client_certificate_sha256.len() != 32
        || token.signing_key_id.is_empty()
        || token.authority_signature.len() != 64
        || issued.seconds > now.saturating_add(30)
        || expires.seconds <= now
        || expires.seconds <= issued.seconds
        || expires.seconds.saturating_sub(issued.seconds) > 300
    {
        return Err(ClientSdkError::InvalidCredential(
            "Authority workload feature token is expired or mismatched".to_string(),
        ));
    }
    Ok(())
}

#[cfg(all(test, unix))]
mod delegated_workload_token_tests {
    use super::{authority_v2, AuthorityClient, ClientSdkError};
    use cyrene_plugin_contracts::workspace_authority_v2::workspace_authority_service_server::{
        WorkspaceAuthorityService, WorkspaceAuthorityServiceServer,
    };
    use prost::Message;
    use std::collections::VecDeque;
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::sync::{Arc, Mutex};
    use std::time::{SystemTime, UNIX_EPOCH};
    use tokio::net::UnixListener;
    use tokio_stream::wrappers::UnixListenerStream;
    use tonic::transport::Server;
    use tonic::{Request, Response, Status};

    static NEXT_SOCKET: AtomicU64 = AtomicU64::new(1);

    #[derive(Default)]
    struct State {
        feature_tokens: VecDeque<Vec<u8>>,
        selected: bool,
        negotiations: usize,
        enqueued_tokens: Vec<Vec<u8>>,
        delegation_reads: Vec<(
            String,
            Option<authority_v2::ExecutionCredential>,
            Option<String>,
        )>,
    }

    #[derive(Clone)]
    struct MockAuthority {
        state: Arc<Mutex<State>>,
    }

    macro_rules! unimplemented_rpc {
        ($name:ident, $request:ty, $response:ty) => {
            fn $name<'life0, 'async_trait>(
                &'life0 self,
                _request: Request<$request>,
            ) -> ::core::pin::Pin<
                Box<
                    dyn ::core::future::Future<Output = Result<Response<$response>, Status>>
                        + Send
                        + 'async_trait,
                >,
            >
            where
                'life0: 'async_trait,
                Self: 'async_trait,
            {
                Box::pin(async { Err(Status::unimplemented("RPC is outside the SDK token test")) })
            }
        };
    }

    #[tonic::async_trait]
    impl WorkspaceAuthorityService for MockAuthority {
        async fn negotiate_version(
            &self,
            _request: Request<authority_v2::NegotiateVersionRequest>,
        ) -> Result<Response<authority_v2::NegotiateVersionResponse>, Status> {
            let mut state = self.state.lock().expect("mock Authority state");
            state.negotiations += 1;
            let token = state.feature_tokens.pop_front().unwrap_or_default();
            let selected_features = if state.selected {
                vec!["delegated_invocations_v1".to_string()]
            } else {
                Vec::new()
            };
            Ok(Response::new(authority_v2::NegotiateVersionResponse {
                selected_version: 2,
                supported_versions: vec![2],
                selected_features,
                supported_features: if state.selected {
                    vec!["delegated_invocations_v1".to_string()]
                } else {
                    Vec::new()
                },
                managed_workload_feature_token: token,
            }))
        }

        async fn approve_and_enqueue_delegated_invocation(
            &self,
            request: Request<authority_v2::ApproveAndEnqueueDelegatedInvocationRequest>,
        ) -> Result<Response<authority_v2::ApproveAndEnqueueDelegatedInvocationResponse>, Status>
        {
            self.state
                .lock()
                .expect("mock Authority state")
                .enqueued_tokens
                .push(request.into_inner().managed_workload_feature_token);
            Ok(Response::new(
                authority_v2::ApproveAndEnqueueDelegatedInvocationResponse {
                    invocation_id: "invocation-test".to_string(),
                    state: authority_v2::InvocationState::Pending as i32,
                },
            ))
        }

        async fn get_delegation(
            &self,
            request: Request<authority_v2::GetDelegationRequest>,
        ) -> Result<Response<authority_v2::GetDelegationResponse>, Status> {
            // This local recorder only checks the Authority-owned delegated target
            // fence; it does not simulate signature verification or authorization.
            let bearer = request
                .metadata()
                .get("authorization")
                .and_then(|value| value.to_str().ok())
                .map(str::to_owned);
            let request = request.into_inner();
            let target_delegation_id = request.delegation_id.clone();
            let target_mismatch = request
                .execution_credential
                .as_ref()
                .is_some_and(|credential| {
                    credential.authorization_source
                        == authority_v2::InvocationAuthorizationSource::Delegation as i32
                        && credential.delegation_id != request.delegation_id
                });
            self.state
                .lock()
                .expect("mock Authority state")
                .delegation_reads
                .push((
                    target_delegation_id.clone(),
                    request.execution_credential.clone(),
                    bearer,
                ));
            if target_mismatch {
                return Err(Status::permission_denied(
                    "requested delegation does not match the signed proof",
                ));
            }
            Ok(Response::new(authority_v2::GetDelegationResponse {
                delegation_id: target_delegation_id,
                workspace_id: request.workspace_id,
                ..authority_v2::GetDelegationResponse::default()
            }))
        }

        unimplemented_rpc!(
            verify_identity,
            authority_v2::VerifyIdentityRequest,
            authority_v2::VerifyIdentityResponse
        );
        unimplemented_rpc!(
            get_catalog_snapshot,
            authority_v2::CatalogSnapshotRequest,
            authority_v2::CatalogSnapshotResponse
        );
        unimplemented_rpc!(
            approve_and_enqueue_invocation,
            authority_v2::ApproveAndEnqueueInvocationRequest,
            authority_v2::ApproveAndEnqueueInvocationResponse
        );
        unimplemented_rpc!(
            create_delegation,
            authority_v2::CreateDelegationRequest,
            authority_v2::CreateDelegationResponse
        );
        unimplemented_rpc!(
            revoke_delegation,
            authority_v2::RevokeDelegationRequest,
            authority_v2::RevokeDelegationResponse
        );
        unimplemented_rpc!(
            claim_invocations,
            authority_v2::ClaimInvocationsRequest,
            authority_v2::ClaimInvocationsResponse
        );
        unimplemented_rpc!(
            validate_execution_authorization,
            authority_v2::ValidateExecutionAuthorizationRequest,
            authority_v2::ValidateExecutionAuthorizationResponse
        );
        unimplemented_rpc!(
            validate_product_execution_authorization,
            authority_v2::ValidateProductExecutionAuthorizationRequest,
            authority_v2::ValidateProductExecutionAuthorizationResponse
        );
        unimplemented_rpc!(
            get_product_invocation_status,
            authority_v2::GetProductInvocationStatusRequest,
            authority_v2::GetProductInvocationStatusResponse
        );
        unimplemented_rpc!(
            acknowledge_delivery,
            authority_v2::AcknowledgeDeliveryRequest,
            authority_v2::AcknowledgeDeliveryResponse
        );
        unimplemented_rpc!(
            submit_invocation_result,
            authority_v2::SubmitInvocationResultRequest,
            authority_v2::SubmitInvocationResultResponse
        );
        unimplemented_rpc!(
            wait_invocation_result,
            authority_v2::WaitInvocationResultRequest,
            authority_v2::WaitInvocationResultResponse
        );
    }

    fn feature_token(serial: u8, expires_at: i64) -> Vec<u8> {
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("system clock")
            .as_secs() as i64;
        authority_v2::ManagedWorkloadFeatureToken {
            audience: "exchange-product".to_string(),
            client_certificate_sha256: vec![serial; 32],
            signing_key_id: format!("key-{serial}"),
            issued_at: Some(prost_types::Timestamp {
                seconds: now,
                nanos: 0,
            }),
            expires_at: Some(prost_types::Timestamp {
                seconds: expires_at,
                nanos: 0,
            }),
            authority_signature: vec![serial; 64],
        }
        .encode_to_vec()
    }

    async fn start_authority(
        selected: bool,
        feature_tokens: Vec<Vec<u8>>,
    ) -> (PathBuf, Arc<Mutex<State>>, tokio::task::JoinHandle<()>) {
        let socket = std::env::temp_dir().join(format!(
            "cyrene-authority-sdk-{}-{}.sock",
            std::process::id(),
            NEXT_SOCKET.fetch_add(1, Ordering::Relaxed)
        ));
        let listener = UnixListener::bind(&socket).expect("bind test Authority socket");
        let state = Arc::new(Mutex::new(State {
            feature_tokens: feature_tokens.into(),
            selected,
            ..State::default()
        }));
        let service = MockAuthority {
            state: Arc::clone(&state),
        };
        let task = tokio::spawn(async move {
            Server::builder()
                .add_service(WorkspaceAuthorityServiceServer::new(service))
                .serve_with_incoming(UnixListenerStream::new(listener))
                .await
                .expect("serve test Authority");
        });
        (socket, state, task)
    }

    fn enqueue_request() -> authority_v2::ApproveAndEnqueueDelegatedInvocationRequest {
        authority_v2::ApproveAndEnqueueDelegatedInvocationRequest {
            workspace_id: "workspace-test".to_string(),
            delegation_id: "delegation-test".to_string(),
            invocation: None,
            managed_workload_feature_token: b"caller supplied stale token".to_vec(),
        }
    }

    #[tokio::test]
    async fn delegated_enqueue_refreshes_and_transmits_the_current_workload_token() {
        let expiry = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("system clock")
            .as_secs() as i64
            + 180;
        let first = feature_token(1, expiry);
        let fresh = feature_token(2, expiry);
        let (socket, state, server) =
            start_authority(true, vec![first.clone(), fresh.clone()]).await;
        let mut client = AuthorityClient::connect_uds(&socket)
            .await
            .expect("connect SDK");
        assert_eq!(client.managed_workload_feature_token(), first);

        client
            .approve_and_enqueue_delegated_invocation(enqueue_request(), "exchange-product")
            .await
            .expect("enqueue with freshly negotiated token");

        let state = state.lock().expect("mock Authority state");
        assert_eq!(state.negotiations, 2);
        assert_eq!(state.enqueued_tokens, vec![fresh]);
        assert_ne!(state.enqueued_tokens[0], first);
        drop(state);
        drop(client);
        server.abort();
        let _ = std::fs::remove_file(socket);
    }

    #[tokio::test]
    async fn delegated_enqueue_rejects_expired_or_unsupported_refreshed_profiles() {
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("system clock")
            .as_secs() as i64;
        let (socket, state, server) = start_authority(
            true,
            vec![feature_token(3, now + 180), feature_token(4, now - 1)],
        )
        .await;
        let mut client = AuthorityClient::connect_uds(&socket)
            .await
            .expect("connect SDK");
        assert!(client
            .approve_and_enqueue_delegated_invocation(enqueue_request(), "exchange-product")
            .await
            .is_err());
        assert!(state
            .lock()
            .expect("mock Authority state")
            .enqueued_tokens
            .is_empty());
        drop(client);
        server.abort();
        let _ = std::fs::remove_file(socket);

        let (socket, state, server) = start_authority(false, Vec::new()).await;
        let mut client = AuthorityClient::connect_uds(&socket)
            .await
            .expect("connect old profile");
        assert!(matches!(
            client
                .approve_and_enqueue_delegated_invocation(enqueue_request(), "exchange-product")
                .await,
            Err(ClientSdkError::InvalidCredential(_))
        ));
        let state = state.lock().expect("mock Authority state");
        assert_eq!(state.negotiations, 2);
        assert!(state.enqueued_tokens.is_empty());
        drop(state);
        drop(client);
        server.abort();
        let _ = std::fs::remove_file(socket);
    }

    #[tokio::test]
    async fn signed_product_proofs_are_forwarded_without_bearer_metadata() {
        let (socket, state, server) = start_authority(false, Vec::new()).await;
        let mut client = AuthorityClient::connect_uds(&socket)
            .await
            .expect("connect mock Authority");
        let bearer = authority_v2::GetDelegationRequest {
            workspace_id: "workspace-test".into(),
            delegation_id: "grant-test".into(),
            execution_credential: None,
        };
        client
            .get_delegation_with_bearer(bearer, "human-token")
            .await
            .expect("legacy bearer read");

        let interactive = authority_v2::ExecutionCredential {
            authorization_source: authority_v2::InvocationAuthorizationSource::InteractiveSession
                as i32,
            workspace_id: "workspace-test".into(),
            session_id: "session-test".into(),
            session_generation: 2,
            ..authority_v2::ExecutionCredential::default()
        };
        let interactive_request = authority_v2::GetDelegationRequest {
            workspace_id: "workspace-test".into(),
            delegation_id: "grant-test".into(),
            execution_credential: None,
        };
        client
            .get_delegation_with_execution_credential(interactive_request, interactive.clone())
            .await
            .expect("signed interactive Product read");

        let legacy_interactive = authority_v2::ExecutionCredential {
            workspace_id: "workspace-test".into(),
            session_id: "legacy-session-test".into(),
            session_generation: 1,
            ..authority_v2::ExecutionCredential::default()
        };
        client
            .get_delegation_with_execution_credential(
                authority_v2::GetDelegationRequest {
                    workspace_id: "workspace-test".into(),
                    delegation_id: "grant-test".into(),
                    execution_credential: None,
                },
                legacy_interactive.clone(),
            )
            .await
            .expect("legacy signed interactive Product read");

        let credential = authority_v2::ExecutionCredential {
            authorization_source: authority_v2::InvocationAuthorizationSource::Delegation as i32,
            workspace_id: "workspace-test".into(),
            delegation_id: "grant-test".into(),
            delegation_generation: 4,
            delegation_audience: "exchange-product".into(),
            ..authority_v2::ExecutionCredential::default()
        };
        let signed = authority_v2::GetDelegationRequest {
            workspace_id: "workspace-test".into(),
            delegation_id: "grant-test".into(),
            execution_credential: None,
        };
        client
            .get_delegation_with_execution_credential(signed, credential.clone())
            .await
            .expect("signed Product read");

        let mismatched_target = authority_v2::GetDelegationRequest {
            workspace_id: "workspace-test".into(),
            delegation_id: "some-other-grant".into(),
            execution_credential: None,
        };
        assert!(matches!(
            client
                .get_delegation_with_execution_credential(mismatched_target, credential.clone())
                .await,
            Err(ClientSdkError::Rpc(_))
        ));

        let state = state.lock().expect("mock Authority state");
        assert_eq!(state.delegation_reads.len(), 5);
        assert_eq!(
            state.delegation_reads[0],
            ("grant-test".into(), None, Some("Bearer human-token".into()))
        );
        assert_eq!(
            state.delegation_reads[1],
            ("grant-test".into(), Some(interactive), None)
        );
        assert_eq!(
            state.delegation_reads[2],
            ("grant-test".into(), Some(legacy_interactive), None)
        );
        assert_eq!(
            state.delegation_reads[3],
            ("grant-test".into(), Some(credential.clone()), None)
        );
        assert_eq!(
            state.delegation_reads[4],
            ("some-other-grant".into(), Some(credential), None)
        );
        drop(state);
        drop(client);
        server.abort();
        let _ = std::fs::remove_file(socket);
    }

    #[tokio::test]
    async fn signed_delegation_read_rejects_mismatched_scope_without_sending_identity() {
        let (socket, state, server) = start_authority(false, Vec::new()).await;
        let mut client = AuthorityClient::connect_uds(&socket)
            .await
            .expect("connect mock Authority");
        let credential = authority_v2::ExecutionCredential {
            authorization_source: authority_v2::InvocationAuthorizationSource::Delegation as i32,
            workspace_id: "workspace-other".into(),
            delegation_id: "grant-test".into(),
            delegation_generation: 4,
            delegation_audience: "exchange-product".into(),
            ..authority_v2::ExecutionCredential::default()
        };
        let request = authority_v2::GetDelegationRequest {
            workspace_id: "workspace-test".into(),
            delegation_id: "grant-test".into(),
            execution_credential: None,
        };
        assert!(matches!(
            client
                .get_delegation_with_execution_credential(request, credential)
                .await,
            Err(ClientSdkError::InvalidCredential(_))
        ));

        let incomplete_delegation = authority_v2::ExecutionCredential {
            authorization_source: authority_v2::InvocationAuthorizationSource::Delegation as i32,
            workspace_id: "workspace-test".into(),
            ..authority_v2::ExecutionCredential::default()
        };
        assert!(matches!(
            client
                .get_delegation_with_execution_credential(
                    authority_v2::GetDelegationRequest {
                        workspace_id: "workspace-test".into(),
                        delegation_id: "grant-test".into(),
                        execution_credential: None,
                    },
                    incomplete_delegation,
                )
                .await,
            Err(ClientSdkError::InvalidCredential(_))
        ));

        let valid_delegation = authority_v2::ExecutionCredential {
            authorization_source: authority_v2::InvocationAuthorizationSource::Delegation as i32,
            workspace_id: "workspace-test".into(),
            delegation_id: "grant-test".into(),
            delegation_generation: 4,
            delegation_audience: "exchange-product".into(),
            ..authority_v2::ExecutionCredential::default()
        };
        assert!(matches!(
            client
                .get_delegation_with_execution_credential(
                    authority_v2::GetDelegationRequest {
                        workspace_id: "workspace-test".into(),
                        delegation_id: "grant-test".into(),
                        execution_credential: Some(valid_delegation.clone()),
                    },
                    valid_delegation,
                )
                .await,
            Err(ClientSdkError::InvalidCredential(_))
        ));
        assert!(state
            .lock()
            .expect("mock Authority state")
            .delegation_reads
            .is_empty());
        drop(client);
        server.abort();
        let _ = std::fs::remove_file(socket);
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
