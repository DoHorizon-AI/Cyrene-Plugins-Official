//! ┌─────────────────────────────────────────────────────────────────────┐
//! │  📄 lib.rs                                                          │
//! │  Package: cyrene-workspace-product-adapters                         │
//! │  Role: Dispatch Authority-resolved Product operations.              │
//! │                                                                     │
//! │  模块职责：只执行Authority核验后给出的规范HTTP目标和操作语义。       │
//! └─────────────────────────────────────────────────────────────────────┘

#![forbid(unsafe_code)]

use std::collections::{HashMap, HashSet};
use std::fs::File;
use std::io::Read;
use std::net::{IpAddr, SocketAddr};
use std::path::{Path, PathBuf};
use std::time::Duration;

pub use cyrene_plugin_contracts::workspace_authority_v2::{
    ExecutionCredential, ExecutionTarget, IdempotencySemantics,
};
pub use cyrene_plugin_contracts::workspace_product_v2::{
    ProductApiInvocationV2, ProductApiResponseV2,
};
use reqwest::{Method, Url};
use serde::Deserialize;
use sha2::{Digest, Sha256};
use thiserror::Error;
use zeroize::Zeroize;

const MAX_CREDENTIAL_MAP_BYTES: u64 = 64 * 1024;
const MAX_CREDENTIAL_MAP_ENTRIES: usize = 1024;
const MIN_BEARER_TOKEN_BYTES: usize = 32;
const MAX_BEARER_TOKEN_BYTES: usize = 4096;

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

/// Reads an Authority-scope-bound Product credential map from a protected file.
///
/// The provider stores only the path and rereads the map for every dispatch, so an
/// atomic file replacement takes effect on the next call.
pub struct ProtectedCredentialProvider {
    path: PathBuf,
}

impl ProtectedCredentialProvider {
    /// Validates the configured protected map before the Connector starts polling.
    ///
    /// # Errors
    /// Returns a static error if the path, file protection, or map contents are invalid.
    pub fn new(path: impl AsRef<Path>) -> Result<Self, ProductAdapterError> {
        let provider = Self {
            path: path.as_ref().to_path_buf(),
        };
        provider.load_map()?;
        Ok(provider)
    }

    fn authorization_for(
        &self,
        credential: &ExecutionCredential,
        endpoint_origin: &str,
    ) -> Result<reqwest::header::HeaderValue, ProductAdapterError> {
        let map = self.load_map()?;
        let key = CredentialScopeKey::from_credential(credential, endpoint_origin);
        let mut matches = map
            .entries
            .get(&key)
            .into_iter()
            .flatten()
            .filter(|entry| entry.resource_constraint.matches(&credential.resource_id));
        let entry = matches.next().ok_or_else(|| {
            ProductAdapterError::InvalidInvocation(
                "no protected Product credential matches the Authority-approved scope".into(),
            )
        })?;
        if matches.next().is_some() {
            return Err(invalid_credential_map(
                "protected credential scope has ambiguous resource constraints",
            ));
        }
        entry.bearer_token.authorization_header()
    }

    fn load_map(&self) -> Result<ProtectedCredentialMap, ProductAdapterError> {
        // ── Phase 1: Open the final path without following links and verify its fd ──
        // 第一阶段：拒绝末级链接，并按已打开文件描述符检查文件保护属性。
        let mut file = open_protected_map(&self.path)?;
        let before = file
            .metadata()
            .map_err(|_| invalid_credential_map("cannot inspect protected credential map"))?;
        validate_protected_file_metadata(&before)?;
        if before.len() == 0 || before.len() > MAX_CREDENTIAL_MAP_BYTES {
            return Err(invalid_credential_map(
                "protected credential map has an invalid size",
            ));
        }

        // ── Phase 2: Read a bounded snapshot and erase the raw JSON buffer ──
        // 第二阶段：有界读取配置快照，并在解析后清除含令牌的原始字节。
        let mut bytes = Vec::with_capacity(before.len() as usize);
        if (&mut file)
            .take(MAX_CREDENTIAL_MAP_BYTES + 1)
            .read_to_end(&mut bytes)
            .is_err()
        {
            bytes.zeroize();
            return Err(invalid_credential_map(
                "cannot read protected credential map",
            ));
        }
        if bytes.len() as u64 > MAX_CREDENTIAL_MAP_BYTES {
            bytes.zeroize();
            return Err(invalid_credential_map(
                "protected credential map exceeds the size limit",
            ));
        }
        let after = match file.metadata() {
            Ok(metadata) => metadata,
            Err(_) => {
                bytes.zeroize();
                return Err(invalid_credential_map(
                    "cannot inspect protected credential map",
                ));
            }
        };
        if !same_protected_file_metadata(&before, &after) {
            bytes.zeroize();
            return Err(invalid_credential_map(
                "protected credential map changed while being read",
            ));
        }

        let decoded = serde_json::from_slice::<CredentialMapFile>(&bytes);
        bytes.zeroize();
        let decoded =
            decoded.map_err(|_| invalid_credential_map("protected credential map is invalid"))?;

        // ── Phase 3: Validate all scopes and reject duplicates before dispatch ──
        // 第三阶段：校验所有授权 scope，并在任何 Product 请求前拒绝重复项。
        if decoded.version != 2
            || decoded.credentials.is_empty()
            || decoded.credentials.len() > MAX_CREDENTIAL_MAP_ENTRIES
        {
            return Err(invalid_credential_map(
                "protected credential map has an unsupported version or entry count",
            ));
        }

        let mut entries: HashMap<CredentialScopeKey, Vec<ProtectedScopeEntry>> =
            HashMap::with_capacity(decoded.credentials.len());
        let mut token_digests = HashSet::with_capacity(decoded.credentials.len());
        for row in decoded.credentials {
            let entry = row.into_entry()?;
            let key = entry.key.clone();
            let siblings = entries.entry(key).or_default();
            if siblings.iter().any(|existing| {
                existing
                    .resource_constraint
                    .overlaps(&entry.resource_constraint)
            }) {
                return Err(invalid_credential_map(
                    "protected credential map contains overlapping scope constraints",
                ));
            }
            if !token_digests.insert(entry.bearer_token.digest()) {
                return Err(invalid_credential_map(
                    "protected credential map contains a duplicate bearer token",
                ));
            }
            siblings.push(entry);
        }
        Ok(ProtectedCredentialMap { entries })
    }
}

struct ProtectedCredentialMap {
    entries: HashMap<CredentialScopeKey, Vec<ProtectedScopeEntry>>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
struct CredentialMapFile {
    version: u32,
    credentials: Vec<CredentialMapRow>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
struct CredentialMapRow {
    organization_id: String,
    workspace_id: String,
    operation_owner_id: String,
    operation_id: String,
    scope: String,
    resource_constraint: ResourceConstraint,
    target_component: String,
    http_method: String,
    endpoint_origin: String,
    execution_device_id: String,
    execution_device_generation: u64,
    execution_authorization_id_hex: String,
    execution_device_certificate_sha256_hex: String,
    contract_activation_generation: u64,
    bearer_token: SecretToken,
}

struct ProtectedScopeEntry {
    key: CredentialScopeKey,
    resource_constraint: ResourceConstraint,
    bearer_token: SecretToken,
}

#[derive(Deserialize)]
#[serde(
    tag = "kind",
    rename_all = "camelCase",
    rename_all_fields = "camelCase",
    deny_unknown_fields
)]
enum ResourceConstraint {
    Exact { resource_id: String },
    AuthorityApprovedResource,
}

impl ResourceConstraint {
    fn matches(&self, resource_id: &str) -> bool {
        match self {
            Self::Exact {
                resource_id: expected,
            } => expected == resource_id,
            Self::AuthorityApprovedResource => true,
        }
    }

    fn overlaps(&self, other: &Self) -> bool {
        match (self, other) {
            (Self::AuthorityApprovedResource, _) | (_, Self::AuthorityApprovedResource) => true,
            (Self::Exact { resource_id: left }, Self::Exact { resource_id: right }) => {
                left == right
            }
        }
    }
}

impl CredentialMapRow {
    fn into_entry(self) -> Result<ProtectedScopeEntry, ProductAdapterError> {
        let string_fields = [
            self.organization_id.as_str(),
            self.workspace_id.as_str(),
            self.operation_owner_id.as_str(),
            self.operation_id.as_str(),
            self.scope.as_str(),
            self.target_component.as_str(),
            self.http_method.as_str(),
            self.execution_device_id.as_str(),
        ];
        if string_fields.iter().any(|value| value.is_empty())
            || self.execution_device_generation == 0
            || self.contract_activation_generation == 0
        {
            return Err(invalid_credential_map(
                "protected credential map contains an incomplete scope",
            ));
        }
        let execution_authorization_id =
            decode_lower_hex::<16>(&self.execution_authorization_id_hex).ok_or_else(|| {
                invalid_credential_map("protected credential scope encoding is invalid")
            })?;
        let execution_device_certificate_sha256 = decode_lower_hex::<32>(
            &self.execution_device_certificate_sha256_hex,
        )
        .ok_or_else(|| invalid_credential_map("protected credential scope encoding is invalid"))?;
        let endpoint_origin = validate_configured_endpoint_origin(&self.endpoint_origin)?;

        Ok(ProtectedScopeEntry {
            key: CredentialScopeKey {
                organization_id: self.organization_id,
                workspace_id: self.workspace_id,
                operation_owner_id: self.operation_owner_id,
                operation_id: self.operation_id,
                scope: self.scope,
                target_component: self.target_component,
                http_method: self.http_method,
                endpoint_origin,
                execution_device_id: self.execution_device_id,
                execution_device_generation: self.execution_device_generation,
                execution_authorization_id: execution_authorization_id.to_vec(),
                execution_device_certificate_sha256: execution_device_certificate_sha256.to_vec(),
                contract_activation_generation: self.contract_activation_generation,
            },
            resource_constraint: self.resource_constraint,
            bearer_token: self.bearer_token,
        })
    }
}

#[derive(Clone, Eq, Hash, PartialEq)]
struct CredentialScopeKey {
    organization_id: String,
    workspace_id: String,
    operation_owner_id: String,
    operation_id: String,
    scope: String,
    target_component: String,
    http_method: String,
    endpoint_origin: String,
    execution_device_id: String,
    execution_device_generation: u64,
    execution_authorization_id: Vec<u8>,
    execution_device_certificate_sha256: Vec<u8>,
    contract_activation_generation: u64,
}

impl CredentialScopeKey {
    fn from_credential(credential: &ExecutionCredential, endpoint_origin: &str) -> Self {
        Self {
            organization_id: credential.organization_id.clone(),
            workspace_id: credential.workspace_id.clone(),
            operation_owner_id: credential.operation_owner_id.clone(),
            operation_id: credential.operation_id.clone(),
            scope: credential.scope.clone(),
            target_component: credential.target_component.clone(),
            http_method: credential.http_method.clone(),
            endpoint_origin: endpoint_origin.to_string(),
            execution_device_id: credential.execution_device_id.clone(),
            execution_device_generation: credential.execution_device_generation,
            execution_authorization_id: credential.execution_authorization_id.clone(),
            execution_device_certificate_sha256: credential
                .execution_device_certificate_sha256
                .clone(),
            contract_activation_generation: credential.contract_activation_generation,
        }
    }
}

/// Secret bearer bytes; deliberately has no `Debug` or `Clone` implementation.
struct SecretToken(Vec<u8>);

impl SecretToken {
    fn is_valid(&self) -> bool {
        let bytes = &self.0;
        (MIN_BEARER_TOKEN_BYTES..=MAX_BEARER_TOKEN_BYTES).contains(&bytes.len())
            && bytes.iter().all(|byte| {
                byte.is_ascii_alphanumeric()
                    || matches!(byte, b'-' | b'.' | b'_' | b'~' | b'+' | b'/' | b'=')
            })
    }

    fn digest(&self) -> [u8; 32] {
        Sha256::digest(&self.0).into()
    }

    fn authorization_header(&self) -> Result<reqwest::header::HeaderValue, ProductAdapterError> {
        let mut header = Vec::with_capacity(7 + self.0.len());
        header.extend_from_slice(b"Bearer ");
        header.extend_from_slice(&self.0);
        let value = reqwest::header::HeaderValue::from_bytes(&header);
        header.zeroize();
        let mut value =
            value.map_err(|_| invalid_credential_map("protected bearer token is invalid"))?;
        value.set_sensitive(true);
        Ok(value)
    }
}

impl Drop for SecretToken {
    fn drop(&mut self) {
        self.0.zeroize();
    }
}

impl<'de> Deserialize<'de> for SecretToken {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        let token = Self(String::deserialize(deserializer)?.into_bytes());
        if !token.is_valid() {
            return Err(serde::de::Error::custom("invalid bearer token"));
        }
        Ok(token)
    }
}

fn decode_lower_hex<const N: usize>(value: &str) -> Option<[u8; N]> {
    if value.len() != N * 2
        || !value
            .as_bytes()
            .iter()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(byte))
    {
        return None;
    }
    let mut bytes = [0; N];
    for (index, pair) in value.as_bytes().as_chunks::<2>().0.iter().enumerate() {
        let high = (pair[0] as char).to_digit(16)? as u8;
        let low = (pair[1] as char).to_digit(16)? as u8;
        bytes[index] = (high << 4) | low;
    }
    Some(bytes)
}

fn invalid_credential_map(message: &'static str) -> ProductAdapterError {
    ProductAdapterError::InvalidInvocation(message.to_string())
}

fn validate_configured_endpoint_origin(value: &str) -> Result<String, ProductAdapterError> {
    let url = Url::parse(value)
        .map_err(|_| invalid_credential_map("protected credential endpoint origin is invalid"))?;
    if !matches!(url.scheme(), "http" | "https")
        || !url.username().is_empty()
        || url.password().is_some()
        || url.path() != "/"
        || url.query().is_some()
        || url.fragment().is_some()
    {
        return Err(invalid_credential_map(
            "protected credential endpoint origin is invalid",
        ));
    }
    let origin = url.origin().ascii_serialization();
    if origin == "null" || origin != value {
        return Err(invalid_credential_map(
            "protected credential endpoint origin must be canonical",
        ));
    }
    Ok(origin)
}

#[cfg(unix)]
fn open_protected_map(path: &Path) -> Result<File, ProductAdapterError> {
    use rustix::fs::{open, Mode, OFlags};

    let descriptor = open(
        path,
        OFlags::RDONLY | OFlags::CLOEXEC | OFlags::NOFOLLOW | OFlags::NONBLOCK,
        Mode::empty(),
    )
    .map_err(|_| invalid_credential_map("cannot open protected credential map"))?;
    Ok(File::from(descriptor))
}

#[cfg(not(unix))]
fn open_protected_map(_path: &Path) -> Result<File, ProductAdapterError> {
    Err(invalid_credential_map(
        "protected credential maps require a Unix filesystem",
    ))
}

#[cfg(unix)]
fn validate_protected_file_metadata(
    metadata: &std::fs::Metadata,
) -> Result<(), ProductAdapterError> {
    use rustix::process::{getegid, geteuid};
    use std::os::unix::fs::MetadataExt;

    if !protected_file_metadata_is_valid(
        metadata.is_file(),
        metadata.nlink(),
        metadata.mode(),
        metadata.uid(),
        metadata.gid(),
        geteuid().as_raw(),
        getegid().as_raw(),
    ) {
        return Err(invalid_credential_map(
            "protected credential map ownership, mode, link count, or type is invalid",
        ));
    }
    Ok(())
}

#[cfg(unix)]
fn protected_file_metadata_is_valid(
    is_regular_file: bool,
    link_count: u64,
    raw_mode: u32,
    file_uid: u32,
    file_gid: u32,
    effective_uid: u32,
    effective_gid: u32,
) -> bool {
    let permissions_and_special_bits = raw_mode & 0o7777;
    is_regular_file
        && link_count == 1
        && matches!(permissions_and_special_bits, 0o400 | 0o600)
        && file_uid == effective_uid
        && (file_gid == 0 || file_gid == effective_gid)
}

#[cfg(not(unix))]
fn validate_protected_file_metadata(
    _metadata: &std::fs::Metadata,
) -> Result<(), ProductAdapterError> {
    Err(invalid_credential_map(
        "protected credential maps require a Unix filesystem",
    ))
}

fn same_protected_file_metadata(before: &std::fs::Metadata, after: &std::fs::Metadata) -> bool {
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        before.dev() == after.dev()
            && before.ino() == after.ino()
            && before.len() == after.len()
            && before.mode() == after.mode()
            && before.uid() == after.uid()
            && before.gid() == after.gid()
            && before.nlink() == after.nlink()
            && before.mtime() == after.mtime()
            && before.mtime_nsec() == after.mtime_nsec()
            && before.ctime() == after.ctime()
            && before.ctime_nsec() == after.ctime_nsec()
    }
    #[cfg(not(unix))]
    {
        before.len() == after.len()
    }
}

/// HTTP adapter for targets returned by Authority after credential redemption.
pub struct GenericProductHttpAdapter {
    default_timeout: Duration,
    allowed_private_origins: HashSet<String>,
    credential_provider: ProtectedCredentialProvider,
}

impl GenericProductHttpAdapter {
    /// Creates an adapter with a required protected credential map and explicit private origins.
    pub fn new(
        default_timeout: Duration,
        allowed_private_origins: impl IntoIterator<Item = String>,
        credential_provider: ProtectedCredentialProvider,
    ) -> Self {
        Self {
            default_timeout,
            allowed_private_origins: allowed_private_origins.into_iter().collect(),
            credential_provider,
        }
    }

    /// Dispatches only after the redeemed Authority scope matches a protected credential row.
    pub async fn dispatch(
        &self,
        invocation: &ProductApiInvocationV2,
        target: &ExecutionTarget,
        credential: &ExecutionCredential,
        timeout: Duration,
    ) -> Result<ProductApiResponseV2, ProductAdapterError> {
        validate_invocation_binding(invocation, target)?;
        validate_credential_target_binding(invocation, target, credential)?;
        let (url, resolved_addresses) =
            validate_endpoint(&target.endpoint, &self.allowed_private_origins).await?;
        let method = canonical_method(&target.http_method)?;
        let endpoint_origin = url.origin().ascii_serialization();
        let authorization = self
            .credential_provider
            .authorization_for(credential, &endpoint_origin)?;
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
            .header(reqwest::header::CONTENT_TYPE, "application/json")
            .header(reqwest::header::AUTHORIZATION, authorization);

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

fn validate_credential_target_binding(
    invocation: &ProductApiInvocationV2,
    target: &ExecutionTarget,
    credential: &ExecutionCredential,
) -> Result<(), ProductAdapterError> {
    if credential.organization_id.is_empty()
        || credential.workspace_id.is_empty()
        || credential.operation_owner_id != invocation.owner_id
        || credential.operation_id != invocation.operation_id
        || credential.scope != target.scope
        || credential.resource_id != target.resource_id
        || credential.target_component != target.target_component
        || credential.http_method != target.http_method
        || credential.endpoint != target.endpoint
        || credential.execution_device_id != target.execution_device_id
        || credential.execution_device_generation != target.execution_device_generation
        || credential.execution_authorization_id != target.execution_authorization_id
        || credential.execution_device_certificate_sha256
            != target.execution_device_certificate_sha256
        || credential.idempotency_key != target.idempotency_key
        || credential.idempotency_semantics != target.idempotency_semantics
        || credential.execution_device_generation == 0
        || credential.execution_authorization_id.len() != 16
        || credential.execution_device_certificate_sha256.len() != 32
        || credential.contract_activation_generation == 0
        || credential.authority_signature.len() != 64
    {
        return Err(ProductAdapterError::InvalidInvocation(
            "redeemed Authority credential does not bind the Product target".to_string(),
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

#[cfg(all(test, unix))]
mod protected_credential_security_tests {
    use super::{protected_file_metadata_is_valid, SecretToken};

    #[test]
    fn systemd_readonly_root_group_credential_metadata_is_accepted() {
        // systemd LoadCredential on the supported Ubuntu service setup reports
        // service UID, root GID, one link, and owner-read-only permissions.
        assert!(protected_file_metadata_is_valid(
            true,
            1,
            0o100000 | 0o400,
            999,
            0,
            999,
            999,
        ));
        assert!(protected_file_metadata_is_valid(
            true,
            1,
            0o100000 | 0o600,
            999,
            999,
            999,
            999,
        ));
    }

    #[test]
    fn protected_credential_metadata_rejects_other_owners_and_unsafe_modes() {
        for mode in [
            0o200, 0o100, 0o440, 0o444, 0o640, 0o644, 0o700, 0o1400, 0o2400,
        ] {
            assert!(
                !protected_file_metadata_is_valid(true, 1, 0o100000 | mode, 999, 0, 999, 999),
                "accepted unsafe permission mode {mode:o}"
            );
        }
        assert!(!protected_file_metadata_is_valid(
            true,
            1,
            0o100000 | 0o400,
            1000,
            0,
            999,
            999,
        ));
        assert!(!protected_file_metadata_is_valid(
            true,
            1,
            0o100000 | 0o400,
            999,
            998,
            999,
            999,
        ));
        assert!(!protected_file_metadata_is_valid(
            true,
            2,
            0o100000 | 0o400,
            999,
            0,
            999,
            999,
        ));
        assert!(!protected_file_metadata_is_valid(
            false,
            1,
            0o100000 | 0o400,
            999,
            0,
            999,
            999,
        ));
    }

    #[test]
    fn bearer_authorization_header_is_marked_sensitive_and_debug_redacted() {
        const DUMMY_TOKEN: &str = "unit-test-bearer-token-0123456789";
        let token = SecretToken(DUMMY_TOKEN.as_bytes().to_vec());
        let header = token.authorization_header().unwrap();
        let request = reqwest::Client::new()
            .post("https://example.test/")
            .header(reqwest::header::AUTHORIZATION, header)
            .build()
            .unwrap();
        let headers = request.headers();
        assert!(headers
            .get(reqwest::header::AUTHORIZATION)
            .unwrap()
            .is_sensitive());
        assert!(!format!("{headers:?}").contains(DUMMY_TOKEN));
    }
}
