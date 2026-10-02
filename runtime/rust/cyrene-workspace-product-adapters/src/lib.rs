//! ┌─────────────────────────────────────────────────────────────────────┐
//! │  📄 lib.rs                                                          │
//! │  Package: cyrene-workspace-product-adapters                         │
//! │  Role: Dispatch Authority-resolved Product operations.              │
//! │                                                                     │
//! │  模块职责：只执行Authority核验后给出的规范HTTP目标和操作语义。       │
//! └─────────────────────────────────────────────────────────────────────┘

#![forbid(unsafe_code)]

use std::collections::HashSet;
use std::net::{IpAddr, SocketAddr};
use std::time::Duration;

pub use cyrene_plugin_contracts::workspace_authority_v2::{ExecutionTarget, IdempotencySemantics};
pub use cyrene_plugin_contracts::workspace_product_v2::{
    ProductApiInvocationV2, ProductApiResponseV2,
};
use reqwest::{Method, Url};
use thiserror::Error;

#[derive(Debug, Error)]
pub enum ProductAdapterError {
    #[error("CONNECTION_TIMEOUT: {0}")]
    Timeout(String),
    #[error("NETWORK_FAILURE: {0}")]
    Network(String),
    #[error("INVALID_AUTHORIZED_TARGET: {0}")]
    InvalidTarget(String),
    #[error("INVALID_AUTHORIZED_INVOCATION: {0}")]
    InvalidInvocation(String),
}

/// HTTP adapter for targets returned by Authority after credential redemption.
pub struct GenericProductHttpAdapter {
    default_timeout: Duration,
    allowed_private_origins: HashSet<String>,
}

impl Default for GenericProductHttpAdapter {
    fn default() -> Self {
        Self::new(Duration::from_secs(30), std::iter::empty::<String>())
    }
}

impl GenericProductHttpAdapter {
    /// Creates an adapter with explicit private HTTP origins allowed by deployment policy.
    pub fn new(
        default_timeout: Duration,
        allowed_private_origins: impl IntoIterator<Item = String>,
    ) -> Self {
        Self {
            default_timeout,
            allowed_private_origins: allowed_private_origins.into_iter().collect(),
        }
    }

    /// Dispatches the body only to the canonical target produced by Authority validation.
    pub async fn dispatch(
        &self,
        invocation: &ProductApiInvocationV2,
        target: &ExecutionTarget,
        timeout: Duration,
    ) -> Result<ProductApiResponseV2, ProductAdapterError> {
        validate_invocation_binding(invocation, target)?;
        let (url, resolved_addresses) =
            validate_endpoint(&target.endpoint, &self.allowed_private_origins).await?;
        let method = canonical_method(&target.http_method)?;
        let timeout = if timeout.is_zero() {
            self.default_timeout
        } else {
            timeout.min(self.default_timeout)
        };

        let mut client_builder = reqwest::Client::builder()
            .timeout(timeout)
            .redirect(reqwest::redirect::Policy::none())
            .no_proxy();
        if let Some(host) = url.host_str() {
            client_builder = client_builder.resolve_to_addrs(host, &resolved_addresses);
        }
        let client = client_builder.build().map_err(|error| {
            ProductAdapterError::InvalidTarget(format!("failed to build pinned client: {error}"))
        })?;
        let mut request = client
            .request(method, url)
            .timeout(timeout)
            .header(reqwest::header::CONTENT_TYPE, "application/json");

        match IdempotencySemantics::try_from(target.idempotency_semantics) {
            Ok(IdempotencySemantics::Required) => {
                if target.idempotency_key.is_empty() {
                    return Err(ProductAdapterError::InvalidInvocation(
                        "Authority required idempotency but returned no key".to_string(),
                    ));
                }
                request = request.header("Idempotency-Key", &target.idempotency_key);
            }
            Ok(IdempotencySemantics::Optional) if !target.idempotency_key.is_empty() => {
                request = request.header("Idempotency-Key", &target.idempotency_key);
            }
            Ok(IdempotencySemantics::NotSupported) => {}
            Ok(IdempotencySemantics::Optional) => {}
            Ok(IdempotencySemantics::Unspecified) | Err(_) => {
                return Err(ProductAdapterError::InvalidInvocation(
                    "Authority returned unspecified idempotency semantics".to_string(),
                ));
            }
        }

        if !invocation.json_body.is_empty() {
            request = request.body(invocation.json_body.clone());
        }

        let response = request.send().await.map_err(|error| {
            if error.is_timeout() {
                ProductAdapterError::Timeout(error.to_string())
            } else {
                ProductAdapterError::Network(error.to_string())
            }
        })?;
        let status_code = u32::from(response.status().as_u16());
        let content_type = response
            .headers()
            .get(reqwest::header::CONTENT_TYPE)
            .and_then(|value| value.to_str().ok())
            .unwrap_or("application/json")
            .to_string();
        let json_body = response.bytes().await.map_err(|error| {
            ProductAdapterError::Network(format!("failed to read Product response: {error}"))
        })?;

        Ok(ProductApiResponseV2 {
            status_code,
            json_body: json_body.to_vec(),
            content_type,
        })
    }
}

fn validate_invocation_binding(
    invocation: &ProductApiInvocationV2,
    target: &ExecutionTarget,
) -> Result<(), ProductAdapterError> {
    if invocation.owner_id.is_empty()
        || invocation.operation_id.is_empty()
        || target.target_component.is_empty()
        || target.scope.is_empty()
        || target.endpoint.is_empty()
    {
        return Err(ProductAdapterError::InvalidInvocation(
            "invocation or Authority target is missing a required binding".to_string(),
        ));
    }
    if invocation.resource_id != target.resource_id {
        return Err(ProductAdapterError::InvalidInvocation(
            "resource binding differs from Authority target".to_string(),
        ));
    }
    if !invocation.idempotency_key.is_empty()
        && !target.idempotency_key.is_empty()
        && invocation.idempotency_key != target.idempotency_key
    {
        return Err(ProductAdapterError::InvalidInvocation(
            "idempotency key differs from Authority target".to_string(),
        ));
    }
    Ok(())
}

fn canonical_method(value: &str) -> Result<Method, ProductAdapterError> {
    let method = Method::from_bytes(value.as_bytes()).map_err(|error| {
        ProductAdapterError::InvalidTarget(format!("invalid HTTP method: {error}"))
    })?;
    if !matches!(
        method,
        Method::GET | Method::POST | Method::PUT | Method::PATCH | Method::DELETE
    ) {
        return Err(ProductAdapterError::InvalidTarget(
            "HTTP method is outside the supported safe set".to_string(),
        ));
    }
    Ok(method)
}

async fn validate_endpoint(
    value: &str,
    allowed_private_origins: &HashSet<String>,
) -> Result<(Url, Vec<SocketAddr>), ProductAdapterError> {
    let url =
        Url::parse(value).map_err(|error| ProductAdapterError::InvalidTarget(error.to_string()))?;
    if !url.username().is_empty()
        || url.password().is_some()
        || url.fragment().is_some()
        || url.host_str().is_none()
    {
        return Err(ProductAdapterError::InvalidTarget(
            "target URL contains userinfo, fragment, or no host".to_string(),
        ));
    }
    let origin = url.origin().ascii_serialization();
    if url.scheme() != "https"
        && (url.scheme() != "http" || !allowed_private_origins.contains(origin.as_str()))
    {
        return Err(ProductAdapterError::InvalidTarget(
            "HTTPS is required unless the exact private HTTP origin is allowlisted".to_string(),
        ));
    }
    let host = url
        .host_str()
        .ok_or_else(|| ProductAdapterError::InvalidTarget("target URL has no host".to_string()))?;
    let port = url.port_or_known_default().ok_or_else(|| {
        ProductAdapterError::InvalidTarget("target URL has no known port".to_string())
    })?;
    let resolved_addresses: Vec<SocketAddr> = if let Ok(ip) = host.parse::<IpAddr>() {
        vec![SocketAddr::new(ip, port)]
    } else {
        tokio::net::lookup_host((host, port))
            .await
            .map_err(|error| {
                ProductAdapterError::InvalidTarget(format!("DNS lookup failed: {error}"))
            })?
            .collect()
    };
    if resolved_addresses.is_empty() {
        return Err(ProductAdapterError::InvalidTarget(
            "target host resolved to no addresses".to_string(),
        ));
    }
    if resolved_addresses
        .iter()
        .any(|address| is_disallowed_address(address.ip()))
    {
        return Err(ProductAdapterError::InvalidTarget(
            "target host resolves to an unspecified or multicast address".to_string(),
        ));
    }
    if resolved_addresses
        .iter()
        .any(|address| is_private_address(address.ip()))
        && !allowed_private_origins.contains(origin.as_str())
    {
        return Err(ProductAdapterError::InvalidTarget(
            "private target origin is not explicitly allowlisted".to_string(),
        ));
    }
    Ok((url, resolved_addresses))
}

fn is_private_address(ip: IpAddr) -> bool {
    match ip {
        IpAddr::V4(ip) => {
            let octets = ip.octets();
            ip.is_private()
                || ip.is_loopback()
                || ip.is_link_local()
                || (octets[0] == 100 && (64..=127).contains(&octets[1]))
                || (octets[0] == 192 && octets[1] == 0 && octets[2] == 0)
        }
        IpAddr::V6(ip) => ip.is_loopback() || ip.is_unique_local() || ip.is_unicast_link_local(),
    }
}

fn is_disallowed_address(ip: IpAddr) -> bool {
    match ip {
        IpAddr::V4(ip) => {
            let octets = ip.octets();
            ip.is_unspecified() || ip.is_multicast() || octets[0] == 0 || octets[0] >= 240
        }
        IpAddr::V6(ip) => ip.is_unspecified() || ip.is_multicast(),
    }
}
