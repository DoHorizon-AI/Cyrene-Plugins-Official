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
pub use cyrene_plugin_contracts::workspace_bridge_v1 as bridge;
pub use cyrene_plugin_contracts::workspace_product_v2 as product;

use authority::workspace_authority_service_client::WorkspaceAuthorityServiceClient;
use bridge::workspace_frontend_bridge_service_client::WorkspaceFrontendBridgeServiceClient;
use thiserror::Error;
use tonic::transport::{Channel, Endpoint, Uri};
use tower::service_fn;

#[derive(Debug, Error)]
pub enum ClientSdkError {
    #[error("TRANSPORT_ERROR: {0}")]
    Transport(String),
    #[error("RPC_ERROR: {0}")]
    Rpc(#[from] tonic::Status),
    #[error("INVALID_ENDPOINT: {0}")]
    InvalidEndpoint(String),
}

/// Client for connecting to Platform Workspace Authority Service.
#[derive(Clone)]
pub struct AuthorityClient {
    inner: WorkspaceAuthorityServiceClient<Channel>,
}

impl AuthorityClient {
    /// Connect to Authority over standard network transport (TCP / mTLS).
    pub async fn connect(endpoint: impl Into<String>) -> Result<Self, ClientSdkError> {
        let ep_str = endpoint.into();
        let ep = Endpoint::from_shared(ep_str)
            .map_err(|e| ClientSdkError::InvalidEndpoint(e.to_string()))?
            .connect_timeout(Duration::from_secs(5));
        let channel = ep.connect().await.map_err(|e| ClientSdkError::Transport(e.to_string()))?;
        Ok(Self {
            inner: WorkspaceAuthorityServiceClient::new(channel),
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

        Ok(Self {
            inner: WorkspaceAuthorityServiceClient::new(channel),
        })
    }

    pub async fn verify_identity(
        &mut self,
        req: authority::VerifyIdentityRequest,
    ) -> Result<authority::VerifyIdentityResponse, ClientSdkError> {
        let resp = self.inner.verify_identity(req).await?;
        Ok(resp.into_inner())
    }

    pub async fn discover_workspaces(
        &mut self,
        req: authority::DiscoverWorkspacesRequest,
    ) -> Result<authority::DiscoverWorkspacesResponse, ClientSdkError> {
        let resp = self.inner.discover_workspaces(req).await?;
        Ok(resp.into_inner())
    }

    pub async fn approve_and_enqueue_invocation(
        &mut self,
        req: authority::ApproveAndEnqueueInvocationRequest,
    ) -> Result<authority::ApproveAndEnqueueInvocationResponse, ClientSdkError> {
        let resp = self.inner.approve_and_enqueue_invocation(req).await?;
        Ok(resp.into_inner())
    }

    pub async fn claim_invocations(
        &mut self,
        req: authority::ClaimInvocationsRequest,
    ) -> Result<authority::ClaimInvocationsResponse, ClientSdkError> {
        let resp = self.inner.claim_invocations(req).await?;
        Ok(resp.into_inner())
    }

    pub async fn acknowledge_delivery(
        &mut self,
        req: authority::AcknowledgeDeliveryRequest,
    ) -> Result<authority::AcknowledgeDeliveryResponse, ClientSdkError> {
        let resp = self.inner.acknowledge_delivery(req).await?;
        Ok(resp.into_inner())
    }

    pub async fn submit_invocation_result(
        &mut self,
        req: authority::SubmitInvocationResultRequest,
    ) -> Result<authority::SubmitInvocationResultResponse, ClientSdkError> {
        let resp = self.inner.submit_invocation_result(req).await?;
        Ok(resp.into_inner())
    }

    pub async fn get_catalog_view(
        &mut self,
        req: authority::GetCatalogViewRequest,
    ) -> Result<authority::GetCatalogViewResponse, ClientSdkError> {
        let resp = self.inner.get_catalog_view(req).await?;
        Ok(resp.into_inner())
    }
}

/// Client for connecting to the Plugins Frontend Bridge Service.
#[derive(Clone)]
pub struct FrontendBridgeClient {
    inner: WorkspaceFrontendBridgeServiceClient<Channel>,
}

impl FrontendBridgeClient {
    pub async fn connect(endpoint: impl Into<String>) -> Result<Self, ClientSdkError> {
        let ep_str = endpoint.into();
        let ep = Endpoint::from_shared(ep_str)
            .map_err(|e| ClientSdkError::InvalidEndpoint(e.to_string()))?
            .connect_timeout(Duration::from_secs(5));
        let channel = ep.connect().await.map_err(|e| ClientSdkError::Transport(e.to_string()))?;
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
