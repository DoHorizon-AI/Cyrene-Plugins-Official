//! ┌─────────────────────────────────────────────────────────────────────┐
//! │  📄 lib.rs                                                          │
//! │  Package: cyrene-workspace-product-adapters                         │
//! │  Role: Generic Product HTTP invocation forwarding adapter.          │
//! │                                                                     │
//! │  模块职责：执行已批准调用的通用 Product HTTP 转发与安全响应解析。      │
//! └─────────────────────────────────────────────────────────────────────┘

#![forbid(unsafe_code)]

use std::time::Duration;

pub use cyrene_plugin_contracts::workspace_authority_v1::ApprovedInvocation;
pub use cyrene_plugin_contracts::workspace_product_v2::ProductApiResponseV2;
use thiserror::Error;

#[derive(Debug, Error)]
pub enum ProductAdapterError {
    #[error("CONNECTION_TIMEOUT: {0}")]
    Timeout(String),
    #[error("NETWORK_FAILURE: {0}")]
    Network(String),
    #[error("INVALID_URL: {0}")]
    InvalidUrl(String),
}

/// Generic HTTP client adapter for dispatching approved Product operations.
pub struct GenericProductHttpAdapter {
    client: reqwest::Client,
    default_timeout: Duration,
}

impl Default for GenericProductHttpAdapter {
    fn default() -> Self {
        Self::new(Duration::from_secs(30))
    }
}

impl GenericProductHttpAdapter {
    pub fn new(default_timeout: Duration) -> Self {
        let client = reqwest::Client::builder()
            .timeout(default_timeout)
            .build()
            .unwrap_or_else(|_| reqwest::Client::new());
        Self {
            client,
            default_timeout,
        }
    }

    /// Dispatches an approved invocation over HTTP to the resolved product URL.
    pub async fn dispatch(
        &self,
        approved: &ApprovedInvocation,
    ) -> Result<ProductApiResponseV2, ProductAdapterError> {
        let raw = approved.raw_invocation.as_ref().ok_or_else(|| {
            ProductAdapterError::InvalidUrl(
                "Missing raw_invocation in ApprovedInvocation".to_string(),
            )
        })?;

        let url = if approved.resolved_target_url.is_empty() {
            return Err(ProductAdapterError::InvalidUrl(
                "Empty resolved_target_url".to_string(),
            ));
        } else {
            &approved.resolved_target_url
        };

        let timeout = if approved.timeout_seconds > 0 {
            Duration::from_secs(approved.timeout_seconds as u64)
        } else {
            self.default_timeout
        };

        let mut req_builder = self
            .client
            .post(url)
            .timeout(timeout)
            .header(reqwest::header::CONTENT_TYPE, "application/json");

        if !raw.idempotency_key.is_empty() {
            req_builder = req_builder.header("Idempotency-Key", &raw.idempotency_key);
        }

        if !raw.resource_id.is_empty() {
            req_builder = req_builder.header("X-Resource-ID", &raw.resource_id);
        }

        if !raw.json_body.is_empty() {
            req_builder = req_builder.body(raw.json_body.clone());
        }

        let resp = req_builder.send().await.map_err(|e| {
            if e.is_timeout() {
                ProductAdapterError::Timeout(e.to_string())
            } else {
                ProductAdapterError::Network(e.to_string())
            }
        })?;

        let status_code = resp.status().as_u16() as u32;
        let content_type = resp
            .headers()
            .get(reqwest::header::CONTENT_TYPE)
            .and_then(|v| v.to_str().ok())
            .unwrap_or("application/json")
            .to_string();

        let body_bytes = resp.bytes().await.map_err(|e| {
            ProductAdapterError::Network(format!("Failed to read response body: {e}"))
        })?;

        Ok(ProductApiResponseV2 {
            status_code,
            json_body: body_bytes.to_vec(),
            content_type,
        })
    }
}
