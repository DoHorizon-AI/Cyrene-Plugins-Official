//! ┌─────────────────────────────────────────────────────────────────────┐
//! │  📄 lib.rs                                                          │
//! │  Package: cyrene-workspace-sidecar                                  │
//! │  Role: Authenticated local API for Authority-owned Product calls.   │
//! │                                                                     │
//! │  模块职责：本地认证后通过Relay调用Authority身份、入队和持久结果等待。 │
//! └─────────────────────────────────────────────────────────────────────┘

#![forbid(unsafe_code)]

use std::fs;
use std::path::Path;
use std::sync::Arc;
use tonic::service::Interceptor;
use tonic::{Request, Response, Status};

pub use cyrene_plugin_contracts::workspace_authority_v2 as authority;
pub use cyrene_plugin_contracts::workspace_local_v1 as local_v1;
pub use cyrene_plugin_contracts::workspace_local_v2 as local_v2;
pub use cyrene_plugin_contracts::workspace_product_v2 as product;
pub use cyrene_plugin_contracts::workspace_v1 as legacy_workspace;
use cyrene_workspace_client_sdk::{AuthorityClient, MutualTlsClientConfig};

/// Relay and end-to-end Authority TLS configuration used by the sidecar.
#[derive(Clone)]
pub struct SidecarConfig {
    pub relay_endpoint: String,
    pub relay_tls: MutualTlsClientConfig,
    pub authority_tls: MutualTlsClientConfig,
    pub result_wait_timeout_ms: u32,
}

#[derive(Clone)]
pub struct WorkspaceSidecarServiceImpl {
    config: SidecarConfig,
}

/// Intercepts all loopback RPCs with a separately provisioned local bearer secret.
#[derive(Clone)]
pub struct LocalBearerInterceptor {
    token: Arc<Vec<u8>>,
}

impl LocalBearerInterceptor {
    /// Loads a private local API token separate from the user's Authority session token.
    pub fn from_file(path: impl AsRef<Path>) -> Result<Self, std::io::Error> {
        let path = path.as_ref();
        if !path.is_absolute() {
            return Err(local_token_config_error(
                "SIDECAR_LOCAL_TOKEN_PATH_MUST_BE_ABSOLUTE",
            ));
        }
        let metadata = fs::symlink_metadata(path)?;
        if !metadata.is_file() {
            return Err(local_token_config_error(
                "SIDECAR_LOCAL_TOKEN_MUST_BE_REGULAR_FILE",
            ));
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            if metadata.permissions().mode() & 0o077 != 0 {
                return Err(local_token_config_error(
                    "SIDECAR_LOCAL_TOKEN_PERMISSIONS_INVALID",
                ));
            }
        }
        let mut bytes = fs::read(path)?;
        while matches!(bytes.last(), Some(b'\n' | b'\r')) {
            bytes.pop();
        }
        if !(32..=256).contains(&bytes.len())
            || !bytes
                .iter()
                .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.'))
        {
            return Err(local_token_config_error("SIDECAR_LOCAL_TOKEN_INVALID"));
        }
        Ok(Self {
            token: Arc::new(bytes),
        })
    }
}

impl Interceptor for LocalBearerInterceptor {
    fn call(&mut self, request: Request<()>) -> Result<Request<()>, Status> {
        let provided = request
            .metadata()
            .get("authorization")
            .and_then(|value| value.as_bytes().strip_prefix(b"Bearer "));
        if provided.is_some_and(|provided| constant_time_eq(provided, self.token.as_slice())) {
            Ok(request)
        } else {
            Err(Status::unauthenticated("SIDECAR_LOCAL_AUTH_REQUIRED"))
        }
    }
}

impl WorkspaceSidecarServiceImpl {
    pub fn new(config: SidecarConfig) -> Self {
        Self { config }
    }

    async fn connect_authority(&self) -> Result<AuthorityClient, Status> {
        AuthorityClient::connect_via_relay(
            self.config.relay_endpoint.clone(),
            &self.config.relay_tls,
            &self.config.authority_tls,
        )
        .await
        .map_err(|error| Status::unavailable(format!("AUTHORITY_RELAY_UNAVAILABLE: {error}")))
    }

    async fn negotiate_authority(&self, authority: &mut AuthorityClient) -> Result<(), Status> {
        let response = authority
            .negotiate_version(authority::NegotiateVersionRequest {
                minimum_version: 2,
                maximum_version: 2,
            })
            .await
            .map_err(|error| {
                Status::unavailable(format!("AUTHORITY_NEGOTIATION_FAILED: {error}"))
            })?;
        if response.selected_version != 2 || !response.supported_versions.contains(&2) {
            return Err(Status::failed_precondition(
                "AUTHORITY_LOCAL_API_VERSION_UNSUPPORTED",
            ));
        }
        Ok(())
    }
}

#[tonic::async_trait]
impl local_v2::workspace_sidecar_service_server::WorkspaceSidecarService
    for WorkspaceSidecarServiceImpl
{
    async fn negotiate_version(
        &self,
        request: Request<local_v2::NegotiateVersionRequest>,
    ) -> Result<Response<local_v2::NegotiateVersionResponse>, Status> {
        let request = request.into_inner();
        if request.minimum_version > 2 || request.maximum_version < 2 {
            return Err(Status::failed_precondition(
                "SIDECAR_LOCAL_API_VERSION_UNSUPPORTED",
            ));
        }
        Ok(Response::new(local_v2::NegotiateVersionResponse {
            selected_version: 2,
            supported_versions: vec![2],
        }))
    }

    async fn health(
        &self,
        _request: Request<local_v2::HealthRequest>,
    ) -> Result<Response<local_v2::HealthResponse>, Status> {
        let mut authority = self.connect_authority().await?;
        self.negotiate_authority(&mut authority).await?;
        Ok(Response::new(local_v2::HealthResponse {
            local_ready: true,
        }))
    }

    async fn discover_workspaces(
        &self,
        request: Request<local_v2::DiscoverWorkspacesRequest>,
    ) -> Result<Response<local_v2::DiscoverWorkspacesResponse>, Status> {
        let request = request.into_inner();
        require_user_token(&request.caller_token).map_err(Status::unauthenticated)?;
        let mut authority = self.connect_authority().await?;
        self.negotiate_authority(&mut authority).await?;
        let identity = authority
            .verify_identity_with_bearer(
                authority::VerifyIdentityRequest {
                    workspace_id: String::new(),
                },
                &request.caller_token,
            )
            .await
            .map_err(|error| {
                Status::unauthenticated(format!("IDENTITY_VERIFICATION_FAILED: {error}"))
            })?;
        if identity.principal_id.is_empty() {
            return Err(Status::unauthenticated("AUTHORITY_RETURNED_NO_PRINCIPAL"));
        }
        let workspaces = identity
            .permitted_workspaces
            .into_iter()
            .filter(|workspace_id| !workspace_id.is_empty())
            .map(|workspace_id| local_v2::WorkspaceSummary {
                display_name: workspace_id.clone(),
                workspace_id,
            })
            .collect();
        Ok(Response::new(local_v2::DiscoverWorkspacesResponse {
            workspaces,
        }))
    }

    async fn execute(
        &self,
        request: Request<local_v2::ExecuteRequest>,
    ) -> Result<Response<local_v2::ExecuteResponse>, Status> {
        let request = request.into_inner();
        require_user_token(&request.caller_token).map_err(Status::unauthenticated)?;
        let invocation = request.invocation.ok_or_else(|| {
            Status::invalid_argument("MISSING_INVOCATION: Product API invocation is required")
        })?;
        if request.workspace_id.trim().is_empty() {
            return Err(Status::invalid_argument("MISSING_WORKSPACE_ID"));
        }

        let mut authority = self.connect_authority().await?;
        self.negotiate_authority(&mut authority).await?;
        let identity = authority
            .verify_identity_with_bearer(
                authority::VerifyIdentityRequest {
                    workspace_id: request.workspace_id.clone(),
                },
                &request.caller_token,
            )
            .await
            .map_err(|error| {
                Status::unauthenticated(format!("IDENTITY_VERIFICATION_FAILED: {error}"))
            })?;
        if identity.principal_id.is_empty()
            || identity.workspace_id != request.workspace_id
            || !identity
                .permitted_workspaces
                .iter()
                .any(|workspace| workspace == &request.workspace_id)
        {
            return Err(Status::permission_denied(
                "CALLER_NOT_AUTHORIZED_FOR_WORKSPACE",
            ));
        }

        let approved = authority
            .approve_and_enqueue_invocation_with_bearer(
                authority::ApproveAndEnqueueInvocationRequest {
                    workspace_id: request.workspace_id.clone(),
                    invocation: Some(invocation),
                },
                &request.caller_token,
            )
            .await
            .map_err(|error| {
                Status::permission_denied(format!("INVOCATION_APPROVAL_FAILED: {error}"))
            })?;
        if approved.invocation_id.is_empty() {
            return Err(Status::internal("AUTHORITY_RETURNED_NO_INVOCATION_ID"));
        }

        let result = authority
            .wait_invocation_result_with_bearer(
                authority::WaitInvocationResultRequest {
                    invocation_id: approved.invocation_id,
                    timeout_ms: self.config.result_wait_timeout_ms,
                    workspace_id: request.workspace_id,
                },
                &request.caller_token,
            )
            .await
            .map_err(|error| {
                Status::unavailable(format!("INVOCATION_RESULT_WAIT_FAILED: {error}"))
            })?;
        let state = authority::InvocationState::try_from(result.state)
            .map_err(|_| Status::internal("AUTHORITY_RETURNED_UNKNOWN_INVOCATION_STATE"))?;
        if state == authority::InvocationState::Succeeded && result.product_response.is_none() {
            return Err(Status::internal(
                "AUTHORITY_REPORTED_SUCCESS_WITHOUT_VALIDATED_PRODUCT_RESPONSE",
            ));
        }
        Ok(Response::new(local_v2::ExecuteResponse {
            state: result.state,
            product_response: result.product_response,
            error_code: result.error_code,
            error_message: result.error_message,
        }))
    }
}

#[tonic::async_trait]
impl local_v1::workspace_sidecar_service_server::WorkspaceSidecarService
    for WorkspaceSidecarServiceImpl
{
    async fn health(
        &self,
        _request: Request<local_v1::HealthRequest>,
    ) -> Result<Response<local_v1::HealthResponse>, Status> {
        let mut authority = self.connect_authority().await?;
        self.negotiate_authority(&mut authority).await?;
        Ok(Response::new(local_v1::HealthResponse {
            local_ready: true,
        }))
    }

    async fn discover_workspaces(
        &self,
        _request: Request<local_v1::DiscoverWorkspacesRequest>,
    ) -> Result<Response<local_v1::DiscoverWorkspacesResponse>, Status> {
        Err(Status::failed_precondition(
            "SIDECAR_LOCAL_V1_DISABLED_USE_V2_AUTHENTICATED_DISCOVERY",
        ))
    }

    async fn execute(
        &self,
        _request: Request<local_v1::ExecuteRequest>,
    ) -> Result<Response<local_v1::ExecuteResponse>, Status> {
        Err(Status::failed_precondition(
            "SIDECAR_LOCAL_V1_EXECUTION_DISABLED_USE_V2_AUTHORITY_OUTBOX",
        ))
    }
}

fn require_user_token(token: &str) -> Result<(), &'static str> {
    if token.trim().len() < 16 || token.bytes().any(|byte| byte.is_ascii_whitespace()) {
        return Err("CALLER_BEARER_TOKEN_REQUIRED");
    }
    Ok(())
}

fn local_token_config_error(code: &'static str) -> std::io::Error {
    std::io::Error::new(std::io::ErrorKind::InvalidInput, code)
}

fn constant_time_eq(left: &[u8], right: &[u8]) -> bool {
    let mut difference = left.len() ^ right.len();
    let max_len = left.len().max(right.len());
    for index in 0..max_len {
        difference |= usize::from(
            left.get(index).copied().unwrap_or_default()
                ^ right.get(index).copied().unwrap_or_default(),
        );
    }
    difference == 0
}
