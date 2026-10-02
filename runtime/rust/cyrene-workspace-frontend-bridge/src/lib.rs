//! ┌─────────────────────────────────────────────────────────────────────┐
//! │  📄 lib.rs                                                          │
//! │  Package: cyrene-workspace-frontend-bridge                          │
//! │  Role: Decoupled Frontend Bridge gRPC Service for Platform BFF.     │
//! │                                                                     │
//! │  模块职责：独立 Frontend Bridge 服务端，通过 UDS/本地 RPC 承接 BFF   │
//! │  派发请求，将已批准调用通过 Relay / 连接链路转发至目标连接器。        │
//! └─────────────────────────────────────────────────────────────────────┘

#![forbid(unsafe_code)]

use std::sync::Arc;

pub use cyrene_plugin_contracts::workspace_authority_v1::WorkspaceDescriptor;
pub use cyrene_plugin_contracts::workspace_bridge_v1::workspace_frontend_bridge_service_server::{
    WorkspaceFrontendBridgeService, WorkspaceFrontendBridgeServiceServer,
};
pub use cyrene_plugin_contracts::workspace_bridge_v1::{
    BridgeDiscoverWorkspacesRequest, BridgeDiscoverWorkspacesResponse,
    BridgeExecuteInvocationRequest, BridgeExecuteInvocationResponse,
};
use cyrene_workspace_product_adapters::GenericProductHttpAdapter;
use tonic::{Request, Response, Status};
use tracing::error;

pub struct WorkspaceFrontendBridgeServiceImpl {
    http_adapter: Arc<GenericProductHttpAdapter>,
}

impl Default for WorkspaceFrontendBridgeServiceImpl {
    fn default() -> Self {
        Self::new(Arc::new(GenericProductHttpAdapter::default()))
    }
}

impl WorkspaceFrontendBridgeServiceImpl {
    pub fn new(http_adapter: Arc<GenericProductHttpAdapter>) -> Self {
        Self { http_adapter }
    }
}

#[tonic::async_trait]
impl WorkspaceFrontendBridgeService for WorkspaceFrontendBridgeServiceImpl {
    async fn discover_workspaces(
        &self,
        request: Request<BridgeDiscoverWorkspacesRequest>,
    ) -> Result<Response<BridgeDiscoverWorkspacesResponse>, Status> {
        let _req = request.into_inner();
        // Return default discoverable workspace descriptors
        let workspaces = vec![WorkspaceDescriptor {
            workspace_id: "workspace-default".to_string(),
            display_name: "Default Local Workspace".to_string(),
            connection_endpoints: vec!["http://127.0.0.1:8080".to_string()],
        }];

        Ok(Response::new(BridgeDiscoverWorkspacesResponse { workspaces }))
    }

    async fn execute_invocation(
        &self,
        request: Request<BridgeExecuteInvocationRequest>,
    ) -> Result<Response<BridgeExecuteInvocationResponse>, Status> {
        let req = request.into_inner();
        let approved = req.approved_invocation.ok_or_else(|| {
            Status::invalid_argument("approved_invocation is required")
        })?;

        match self.http_adapter.dispatch(&approved).await {
            Ok(product_response) => Ok(Response::new(BridgeExecuteInvocationResponse {
                success: true,
                product_response: Some(product_response),
                error_message: String::new(),
            })),
            Err(e) => {
                error!("Frontend bridge invocation dispatch error: {e}");
                Ok(Response::new(BridgeExecuteInvocationResponse {
                    success: false,
                    product_response: None,
                    error_message: e.to_string(),
                }))
            }
        }
    }
}
