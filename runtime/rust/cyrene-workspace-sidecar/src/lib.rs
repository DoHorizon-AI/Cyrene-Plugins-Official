//! ┌─────────────────────────────────────────────────────────────────────┐
//! │  📄 lib.rs                                                          │
//! │  Package: cyrene-workspace-sidecar                                  │
//! │  Role: Loopback-only Workspace API bridge for non-Rust clients.     │
//! │                                                                     │
//! │  模块职责：为非 Rust 客户端提供本地 loopback gRPC 代理。             │
//! └─────────────────────────────────────────────────────────────────────┘

#![forbid(unsafe_code)]

use std::path::PathBuf;
use tonic::{Request, Response, Status};

pub use cyrene_plugin_contracts::workspace_authority_v1 as authority;
pub use cyrene_plugin_contracts::workspace_bridge_v1 as bridge;
pub use cyrene_plugin_contracts::workspace_local_v1::{
    workspace_sidecar_service_server::{WorkspaceSidecarService, WorkspaceSidecarServiceServer},
    DiscoverWorkspacesRequest, DiscoverWorkspacesResponse, ExecuteRequest, ExecuteResponse,
    HealthRequest, HealthResponse, WorkspaceSummary,
};
pub use cyrene_plugin_contracts::workspace_product_v2 as product;
use cyrene_workspace_client_sdk::{AuthorityClient, FrontendBridgeClient};

/// Configuration for the loopback sidecar service.
#[derive(Clone, Debug)]
pub struct SidecarConfig {
    pub authority_endpoint: String,
    pub authority_uds_path: Option<PathBuf>,
    pub bridge_endpoint: String,
    pub bridge_uds_path: Option<PathBuf>,
}

impl Default for SidecarConfig {
    fn default() -> Self {
        Self {
            authority_endpoint: "http://127.0.0.1:50051".to_string(),
            authority_uds_path: None,
            bridge_endpoint: "http://127.0.0.1:50053".to_string(),
            bridge_uds_path: Some(PathBuf::from("/tmp/cyrene-frontend-bridge.sock")),
        }
    }
}

pub struct WorkspaceSidecarServiceImpl {
    config: SidecarConfig,
}

impl WorkspaceSidecarServiceImpl {
    pub fn new(config: SidecarConfig) -> Self {
        Self { config }
    }

    async fn connect_authority(&self) -> Result<AuthorityClient, Status> {
        #[cfg(unix)]
        if let Some(ref path) = self.config.authority_uds_path {
            if path.exists() {
                return AuthorityClient::connect_uds(path).await.map_err(|e| {
                    Status::unavailable(format!("Failed to connect to Authority UDS: {e}"))
                });
            }
        }

        AuthorityClient::connect(&self.config.authority_endpoint)
            .await
            .map_err(|e| Status::unavailable(format!("Failed to connect to Authority: {e}")))
    }

    async fn connect_bridge(&self) -> Result<FrontendBridgeClient, Status> {
        #[cfg(unix)]
        if let Some(ref path) = self.config.bridge_uds_path {
            if path.exists() {
                return FrontendBridgeClient::connect_uds(path).await.map_err(|e| {
                    Status::unavailable(format!("Failed to connect to Bridge UDS: {e}"))
                });
            }
        }

        FrontendBridgeClient::connect(&self.config.bridge_endpoint)
            .await
            .map_err(|e| Status::unavailable(format!("Failed to connect to Bridge: {e}")))
    }
}

#[tonic::async_trait]
impl WorkspaceSidecarService for WorkspaceSidecarServiceImpl {
    async fn health(
        &self,
        _request: Request<HealthRequest>,
    ) -> Result<Response<HealthResponse>, Status> {
        Ok(Response::new(HealthResponse { local_ready: true }))
    }

    async fn discover_workspaces(
        &self,
        request: Request<DiscoverWorkspacesRequest>,
    ) -> Result<Response<DiscoverWorkspacesResponse>, Status> {
        let req = request.into_inner();
        let mut authority = self.connect_authority().await?;

        let auth_resp = authority
            .discover_workspaces(authority::DiscoverWorkspacesRequest {
                principal_id: req.caller_token,
            })
            .await
            .map_err(|e| Status::internal(format!("Authority discover error: {e}")))?;

        let workspaces = auth_resp
            .workspaces
            .into_iter()
            .map(|ws| WorkspaceSummary {
                workspace_id: ws.workspace_id,
                display_name: ws.display_name,
                endpoints: ws.connection_endpoints,
            })
            .collect();

        Ok(Response::new(DiscoverWorkspacesResponse { workspaces }))
    }

    async fn execute(
        &self,
        request: Request<ExecuteRequest>,
    ) -> Result<Response<ExecuteResponse>, Status> {
        let req = request.into_inner();
        let invocation = req.invocation.ok_or_else(|| {
            Status::invalid_argument("MISSING_INVOCATION: invocation field is required")
        })?;

        let mut authority = self.connect_authority().await?;
        let approve_req = authority::ApproveAndEnqueueInvocationRequest {
            workspace_id: req.workspace_id.clone(),
            caller_token: req.caller_token.clone(),
            invocation: Some(invocation),
        };

        let approve_resp = authority
            .approve_and_enqueue_invocation(approve_req)
            .await
            .map_err(|e| Status::internal(format!("Authority approve error: {e}")))?;

        if let Some(err) = approve_resp.error {
            return Ok(Response::new(ExecuteResponse {
                success: false,
                product_response: None,
                error_message: format!(
                    "AUTHORITY_APPROVAL_FAILED: code={}, message={}",
                    err.code, err.message
                ),
            }));
        }

        let approved_inv = approve_resp
            .approved_invocation
            .ok_or_else(|| Status::internal("Authority response missing approved_invocation"))?;

        let mut bridge = self.connect_bridge().await?;
        let bridge_req = bridge::BridgeExecuteInvocationRequest {
            approved_invocation: Some(approved_inv),
            relay_endpoint: String::new(),
            session_token: req.caller_token,
        };

        let bridge_resp = bridge
            .execute_invocation(bridge_req)
            .await
            .map_err(|e| Status::internal(format!("Bridge execution error: {e}")))?;

        Ok(Response::new(ExecuteResponse {
            success: bridge_resp.success,
            product_response: bridge_resp.product_response,
            error_message: bridge_resp.error_message,
        }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn test_sidecar_health() {
        let service = WorkspaceSidecarServiceImpl::new(SidecarConfig::default());
        let resp = service
            .health(Request::new(HealthRequest {}))
            .await
            .unwrap();
        assert!(resp.into_inner().local_ready);
    }
}
