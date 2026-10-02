//! ┌─────────────────────────────────────────────────────────────────────┐
//! │  📄 lib.rs                                                          │
//! │  Package: cyrene-workspace-connector                                │
//! │  Role: Decoupled Connector worker executing approved invocations.  │
//! │                                                                     │
//! │  模块职责：独立连接进程 Connector 运行时，通过 Authority RPC 领取已    │
//! │  批准调用，委托通用 HTTP 适配器转发，并严格执行 UNKNOWN_RESULT 上报。 │
//! └─────────────────────────────────────────────────────────────────────┘

#![forbid(unsafe_code)]

use std::sync::Arc;

pub use cyrene_plugin_contracts::workspace_authority_v1::{
    AcknowledgeDeliveryRequest, ApprovedInvocation, ClaimInvocationsRequest,
    ExecutionOutcomeStatus, SubmitInvocationResultRequest,
};
pub use cyrene_plugin_contracts::workspace_product_v2::{
    ProductApiInvocationV2, ProductApiResponseV2,
};
use cyrene_workspace_client_sdk::AuthorityClient;
use cyrene_workspace_product_adapters::{GenericProductHttpAdapter, ProductAdapterError};
use thiserror::Error;
use tracing::{error, warn};

#[derive(Debug, Error)]
pub enum ConnectorError {
    #[error("CLIENT_SDK_ERROR: {0}")]
    ClientSdk(String),
    #[error("INVOCATION_ERROR: {0}")]
    Invocation(String),
}

/// Standalone Connector daemon for claiming and executing approved Product invocations.
pub struct WorkspaceConnectorWorker {
    connector_id: String,
    workspace_id: String,
    supported_components: Vec<String>,
    authority_client: AuthorityClient,
    http_adapter: Arc<GenericProductHttpAdapter>,
}

impl WorkspaceConnectorWorker {
    pub fn new(
        connector_id: impl Into<String>,
        workspace_id: impl Into<String>,
        supported_components: Vec<String>,
        authority_client: AuthorityClient,
        http_adapter: Arc<GenericProductHttpAdapter>,
    ) -> Self {
        Self {
            connector_id: connector_id.into(),
            workspace_id: workspace_id.into(),
            supported_components,
            authority_client,
            http_adapter,
        }
    }

    /// Process a single batch of approved invocations from the Authority Outbox.
    pub async fn process_batch(&mut self, max_batch: usize) -> Result<usize, ConnectorError> {
        let claim_req = ClaimInvocationsRequest {
            connector_id: self.connector_id.clone(),
            workspace_id: self.workspace_id.clone(),
            supported_components: self.supported_components.clone(),
            max_batch_size: max_batch as u32,
        };

        let claim_resp = self
            .authority_client
            .claim_invocations(claim_req)
            .await
            .map_err(|e| ConnectorError::ClientSdk(e.to_string()))?;

        let count = claim_resp.invocations.len();
        for invocation in claim_resp.invocations {
            if let Err(e) = self.execute_one(invocation).await {
                error!("Error executing invocation: {e}");
            }
        }

        Ok(count)
    }

    /// Execute a single claimed invocation and report result back to Authority.
    pub async fn execute_one(
        &mut self,
        invocation: ApprovedInvocation,
    ) -> Result<bool, ConnectorError> {
        let invocation_id = invocation.invocation_id.clone();
        let credential = invocation.credential.clone().ok_or_else(|| {
            ConnectorError::Invocation(
                "Missing DeliveryCredential on claimed invocation".to_string(),
            )
        })?;

        // 1. Generate delivery receipt and acknowledge delivery
        let receipt = format!("rcpt-{}-{}", self.connector_id, uuid::Uuid::new_v4());
        let ack_req = AcknowledgeDeliveryRequest {
            connector_id: self.connector_id.clone(),
            invocation_id: invocation_id.clone(),
            delivery_receipt: receipt.into_bytes(),
        };

        let ack_resp = self
            .authority_client
            .acknowledge_delivery(ack_req)
            .await
            .map_err(|e| ConnectorError::ClientSdk(e.to_string()))?;

        if !ack_resp.acknowledged {
            warn!("Delivery acknowledgement rejected for invocation {invocation_id}");
            return Ok(false);
        }

        // 2. Dispatch invocation via HTTP adapter
        let dispatch_result = self.http_adapter.dispatch(&invocation).await;

        // 3. Inspect outcome and submit result to Authority
        let is_idempotent = invocation
            .raw_invocation
            .as_ref()
            .map(|raw| !raw.idempotency_key.is_empty())
            .unwrap_or(false);

        let (outcome_status, product_response, error_message) = match dispatch_result {
            Ok(resp) => (ExecutionOutcomeStatus::Success, Some(resp), String::new()),
            Err(ProductAdapterError::Timeout(msg)) => {
                // If timeout occurs on non-idempotent operation: mark UNKNOWN_RESULT to forbid auto-retry
                let status = if is_idempotent {
                    ExecutionOutcomeStatus::Failed
                } else {
                    ExecutionOutcomeStatus::UnknownResult
                };
                (status, None, format!("TIMEOUT: {msg}"))
            }
            Err(ProductAdapterError::Network(msg)) => {
                // If network failure on non-idempotent operation: mark UNKNOWN_RESULT
                let status = if is_idempotent {
                    ExecutionOutcomeStatus::Failed
                } else {
                    ExecutionOutcomeStatus::UnknownResult
                };
                (status, None, format!("NETWORK_FAILURE: {msg}"))
            }
            Err(ProductAdapterError::InvalidUrl(msg)) => (
                ExecutionOutcomeStatus::Failed,
                None,
                format!("INVALID_URL: {msg}"),
            ),
        };

        let submit_req = SubmitInvocationResultRequest {
            invocation_id,
            credential: Some(credential),
            outcome_status: outcome_status as i32,
            product_response,
            error_message,
        };

        let submit_resp = self
            .authority_client
            .submit_invocation_result(submit_req)
            .await
            .map_err(|e| ConnectorError::ClientSdk(e.to_string()))?;

        Ok(submit_resp.accepted)
    }
}
