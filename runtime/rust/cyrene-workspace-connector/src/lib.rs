//! ┌─────────────────────────────────────────────────────────────────────┐
//! │  📄 lib.rs                                                          │
//! │  Package: cyrene-workspace-connector                                │
//! │  Role: Durable Authority-approved invocation worker.                │
//! │                                                                     │
//! │  模块职责：经Relay领取并核验Authority调用，持久化阶段后执行本机产品。 │
//! └─────────────────────────────────────────────────────────────────────┘

#![forbid(unsafe_code)]

mod journal;

pub use crate::journal::JournalError;
use crate::journal::{DurableJournal, InvocationStage, JournalEntry, JOURNAL_SCHEMA_VERSION};
use cyrene_plugin_contracts::workspace_authority_v2 as authority;
pub use cyrene_plugin_contracts::workspace_authority_v2::{
    ApprovedInvocation, CanonicalInvocationEnvelope, ExecutionCredential, ExecutionOutcomeStatus,
    ExecutionTarget, IdempotencySemantics, InvocationAuthorizationSource,
};
pub use cyrene_plugin_contracts::workspace_product_v2::{
    ProductApiInvocationV2, ProductApiResponseV2,
};
use cyrene_workspace_client_sdk::AuthorityClient;
use cyrene_workspace_product_adapters::{GenericProductHttpAdapter, ProductAdapterError};
use prost::Message;
use sha2::{Digest, Sha256};
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use thiserror::Error;
use tracing::{error, warn};

const AUTHORITY_PROTOCOL_VERSION: u32 = 2;
const MAX_CREDENTIAL_TTL_SECONDS: u64 = 300;
const MAX_CLOCK_SKEW_SECONDS: u64 = 30;
const DELEGATED_INVOCATIONS_FEATURE: &str = "delegated_invocations_v1";

#[derive(Debug, Error)]
pub enum ConnectorError {
    #[error("CLIENT_SDK_ERROR: {0}")]
    ClientSdk(String),
    #[error("INVOCATION_ERROR: {0}")]
    Invocation(String),
    #[error("JOURNAL_ERROR: {0}")]
    Journal(#[from] JournalError),
}

/// Standalone Connector worker for one execution device and its durable outbox.
pub struct WorkspaceConnectorWorker {
    workspace_id: String,
    supported_components: Vec<String>,
    authority_client: AuthorityClient,
    http_adapter: Arc<GenericProductHttpAdapter>,
    journal: DurableJournal,
    protocol_negotiated: bool,
    delegated_invocations_negotiated: bool,
    ready: Arc<AtomicBool>,
}

impl WorkspaceConnectorWorker {
    /// Opens the durable journal before the worker can claim any outbox calls.
    pub fn new(
        workspace_id: impl Into<String>,
        supported_components: Vec<String>,
        authority_client: AuthorityClient,
        http_adapter: Arc<GenericProductHttpAdapter>,
        journal_path: impl AsRef<Path>,
    ) -> Result<Self, ConnectorError> {
        Self::new_with_readiness(
            workspace_id,
            supported_components,
            authority_client,
            http_adapter,
            journal_path,
            Arc::new(AtomicBool::new(false)),
        )
    }

    /// Opens a worker while sharing its readiness state with a prestarted health listener.
    pub fn new_with_readiness(
        workspace_id: impl Into<String>,
        supported_components: Vec<String>,
        authority_client: AuthorityClient,
        http_adapter: Arc<GenericProductHttpAdapter>,
        journal_path: impl AsRef<Path>,
        ready: Arc<AtomicBool>,
    ) -> Result<Self, ConnectorError> {
        Ok(Self {
            workspace_id: workspace_id.into(),
            supported_components,
            authority_client,
            http_adapter,
            journal: DurableJournal::open(journal_path)?,
            protocol_negotiated: false,
            delegated_invocations_negotiated: false,
            ready,
        })
    }

    /// Returns the latest Authority connectivity and protocol readiness state.
    pub fn readiness(&self) -> Arc<AtomicBool> {
        Arc::clone(&self.ready)
    }

    /// Recovers durable unfinished records, then claims and processes one outbox batch.
    pub async fn process_batch(&mut self, max_batch: usize) -> Result<usize, ConnectorError> {
        self.ready.store(false, Ordering::Release);
        self.negotiate_protocol().await?;
        self.recover_pending().await?;
        let claim_resp = match self
            .authority_client
            .claim_invocations(authority::ClaimInvocationsRequest {
                max_batch_size: max_batch.min(u32::MAX as usize) as u32,
            })
            .await
        {
            Ok(response) => response,
            Err(error) => {
                self.ready.store(false, Ordering::Release);
                return Err(ConnectorError::ClientSdk(error.to_string()));
            }
        };
        self.ready.store(true, Ordering::Release);

        let count = claim_resp.invocations.len();
        for claimed in claim_resp.invocations {
            if let Err(error) = self.execute_claimed(claimed).await {
                error!("Connector invocation processing failed: {error}");
            }
        }
        Ok(count)
    }

    async fn negotiate_protocol(&mut self) -> Result<(), ConnectorError> {
        if self.protocol_negotiated {
            return Ok(());
        }
        let response = self
            .authority_client
            .negotiate_version(authority::NegotiateVersionRequest {
                minimum_version: AUTHORITY_PROTOCOL_VERSION,
                maximum_version: AUTHORITY_PROTOCOL_VERSION,
                requested_features: vec![DELEGATED_INVOCATIONS_FEATURE.to_string()],
            })
            .await
            .map_err(|error| ConnectorError::ClientSdk(error.to_string()))?;
        if response.selected_version != AUTHORITY_PROTOCOL_VERSION
            || !response
                .supported_versions
                .contains(&AUTHORITY_PROTOCOL_VERSION)
        {
            return Err(ConnectorError::Invocation(
                "Authority did not negotiate the required v2 protocol".to_string(),
            ));
        }
        self.delegated_invocations_negotiated = response
            .selected_features
            .iter()
            .any(|feature| feature == DELEGATED_INVOCATIONS_FEATURE);
        self.protocol_negotiated = true;
        Ok(())
    }

    async fn recover_pending(&mut self) -> Result<(), ConnectorError> {
        let entries: Vec<_> = self.journal.entries().cloned().collect();
        for entry in entries {
            match entry.stage {
                InvocationStage::Submitted => {}
                InvocationStage::Dispatching => {
                    let mut uncertain = entry;
                    uncertain.stage = InvocationStage::UnknownResult;
                    uncertain.outcome_status = ExecutionOutcomeStatus::UnknownResult as i32;
                    uncertain.response.clear();
                    uncertain.error_message =
                        "Connector restarted after dispatch began; product result is unknown"
                            .into();
                    self.journal.persist(uncertain.clone())?;
                    self.submit_journaled(&uncertain).await?;
                }
                InvocationStage::UnknownResult | InvocationStage::ResultReady => {
                    self.submit_journaled(&entry).await?;
                }
                InvocationStage::Claimed
                | InvocationStage::Acknowledging
                | InvocationStage::Acknowledged => {
                    let credential = ExecutionCredential::decode(entry.credential.as_slice())
                        .map_err(|error| ConnectorError::Invocation(error.to_string()))?;
                    let redeemed = self
                        .authority_client
                        .validate_execution_authorization(
                            authority::ValidateExecutionAuthorizationRequest {
                                credential: Some(credential),
                            },
                        )
                        .await
                        .map_err(|error| ConnectorError::ClientSdk(error.to_string()))?
                        .approved_invocation
                        .ok_or_else(|| {
                            ConnectorError::Invocation(
                                "Authority omitted canonical invocation during recovery".into(),
                            )
                        })?;
                    self.execute_validated(redeemed, Some(entry)).await?;
                }
            }
        }
        Ok(())
    }

    async fn execute_claimed(
        &mut self,
        claimed: ApprovedInvocation,
    ) -> Result<bool, ConnectorError> {
        let credential = claimed.credential.clone().ok_or_else(|| {
            ConnectorError::Invocation("Claim omitted execution credential".to_string())
        })?;
        let redeemed = self
            .authority_client
            .validate_execution_authorization(authority::ValidateExecutionAuthorizationRequest {
                credential: Some(credential),
            })
            .await
            .map_err(|error| ConnectorError::ClientSdk(error.to_string()))?
            .approved_invocation
            .ok_or_else(|| {
                ConnectorError::Invocation(
                    "Authority omitted canonical invocation during credential redemption".into(),
                )
            })?;
        self.execute_validated(redeemed, None).await
    }

    async fn execute_validated(
        &mut self,
        approved: ApprovedInvocation,
        prior: Option<JournalEntry>,
    ) -> Result<bool, ConnectorError> {
        let invocation_id = approved.invocation_id.clone();
        let envelope = approved.canonical_envelope.as_ref().ok_or_else(|| {
            ConnectorError::Invocation("Authority omitted canonical envelope".to_string())
        })?;
        let credential = approved.credential.as_ref().ok_or_else(|| {
            ConnectorError::Invocation("Authority omitted execution credential".to_string())
        })?;
        let target = envelope.target.as_ref().ok_or_else(|| {
            ConnectorError::Invocation("Authority omitted canonical execution target".into())
        })?;
        if approved.invocation_id != envelope.invocation_id {
            return Err(ConnectorError::Invocation(
                "approved invocation ID differs from its canonical envelope".into(),
            ));
        }
        validate_credential_binding(envelope, credential)?;
        require_negotiated_profile(
            credential.authorization_source,
            self.delegated_invocations_negotiated,
        )?;
        if envelope.workspace_id != self.workspace_id
            || !self
                .supported_components
                .iter()
                .any(|component| component == &target.target_component)
        {
            return Err(ConnectorError::Invocation(
                "canonical target is outside this Connector workspace or component set".into(),
            ));
        }
        let digest = hex_digest(&Sha256::digest(envelope.encode_to_vec()));

        let mut entry = if let Some(prior) = prior {
            if prior.call_digest != digest || prior.invocation_id != invocation_id {
                return Err(ConnectorError::Invocation(
                    "journal invocation binding changed across recovery".to_string(),
                ));
            }
            prior
        } else if let Some(existing) = self.journal.get(&invocation_id).cloned() {
            if existing.call_digest != digest {
                return Err(ConnectorError::Invocation(
                    "Authority reused an invocation ID with a different call digest".to_string(),
                ));
            }
            existing
        } else {
            let receipt = format!(
                "rcpt-{}-{}",
                safe_component(&envelope.execution_device_id),
                uuid::Uuid::new_v4()
            )
            .into_bytes();
            let entry = JournalEntry {
                schema_version: JOURNAL_SCHEMA_VERSION,
                invocation_id: invocation_id.clone(),
                call_digest: digest,
                receipt,
                credential: credential.encode_to_vec(),
                stage: InvocationStage::Claimed,
                outcome_status: ExecutionOutcomeStatus::Unspecified as i32,
                response: Vec::new(),
                error_message: String::new(),
            };
            self.journal.persist(entry.clone())?;
            entry
        };

        if entry.stage == InvocationStage::Submitted {
            return Ok(true);
        }

        if entry.stage == InvocationStage::Claimed || entry.stage == InvocationStage::Acknowledging
        {
            entry.stage = InvocationStage::Acknowledging;
            self.journal.persist(entry.clone())?;
            let ack = self
                .authority_client
                .acknowledge_delivery(authority::AcknowledgeDeliveryRequest {
                    invocation_id: invocation_id.clone(),
                    credential: Some(credential.clone()),
                    delivery_receipt: entry.receipt.clone(),
                })
                .await
                .map_err(|error| ConnectorError::ClientSdk(error.to_string()))?;
            if !ack.acknowledged {
                warn!("Authority rejected delivery acknowledgement for {invocation_id}");
                return Ok(false);
            }
            entry.stage = InvocationStage::Acknowledged;
            self.journal.persist(entry.clone())?;
        }

        if entry.stage == InvocationStage::Acknowledged {
            entry.stage = InvocationStage::Dispatching;
            self.journal.persist(entry.clone())?;
            let dispatch = self.http_adapter.dispatch(
                envelope.invocation.as_ref().ok_or_else(|| {
                    ConnectorError::Invocation("canonical invocation body is missing".into())
                })?,
                target,
                credential,
                Duration::ZERO,
            );
            let result = dispatch.await;
            match result {
                Ok(response) => {
                    entry.stage = InvocationStage::ResultReady;
                    entry.outcome_status = ExecutionOutcomeStatus::Success as i32;
                    entry.response = response.encode_to_vec();
                    entry.error_message.clear();
                }
                Err(
                    ProductAdapterError::InvalidTarget(message)
                    | ProductAdapterError::InvalidInvocation(message),
                ) => {
                    entry.stage = InvocationStage::ResultReady;
                    entry.outcome_status = ExecutionOutcomeStatus::Failed as i32;
                    entry.response.clear();
                    entry.error_message = message;
                }
                Err(
                    ProductAdapterError::Timeout(message) | ProductAdapterError::Network(message),
                ) => {
                    if !target_is_idempotent(envelope.target.as_ref()) {
                        entry.stage = InvocationStage::UnknownResult;
                        entry.outcome_status = ExecutionOutcomeStatus::UnknownResult as i32;
                        entry.response.clear();
                        entry.error_message = message;
                    } else {
                        entry.stage = InvocationStage::ResultReady;
                        entry.outcome_status = ExecutionOutcomeStatus::Failed as i32;
                        entry.response.clear();
                        entry.error_message = message;
                    }
                }
            }
            self.journal.persist(entry.clone())?;
        }

        self.submit_journaled(&entry).await
    }

    async fn submit_journaled(&mut self, entry: &JournalEntry) -> Result<bool, ConnectorError> {
        let mut pending = entry.clone();
        for attempt in 0..2 {
            let credential = ExecutionCredential::decode(pending.credential.as_slice())
                .map_err(|error| ConnectorError::Invocation(error.to_string()))?;
            let response = if pending.response.is_empty() {
                None
            } else {
                Some(
                    ProductApiResponseV2::decode(pending.response.as_slice())
                        .map_err(|error| ConnectorError::Invocation(error.to_string()))?,
                )
            };
            let result_envelope = authority::CanonicalInvocationResultEnvelope {
                invocation_id: pending.invocation_id.clone(),
                request_digest_sha256: credential.request_digest_sha256.clone(),
                outcome_status: ExecutionOutcomeStatus::try_from(pending.outcome_status)
                    .unwrap_or(ExecutionOutcomeStatus::Unspecified)
                    as i32,
                product_response: response,
                error_code: if pending.stage == InvocationStage::UnknownResult {
                    "UNKNOWN_RESULT".to_string()
                } else if pending.outcome_status == ExecutionOutcomeStatus::Failed as i32 {
                    "PRODUCT_DISPATCH_FAILED".to_string()
                } else {
                    String::new()
                },
                error_message: pending.error_message.clone(),
                delivery_receipt: pending.receipt.clone(),
            };
            let result_digest = Sha256::digest(result_envelope.encode_to_vec()).to_vec();
            let submit = self
                .authority_client
                .submit_invocation_result(authority::SubmitInvocationResultRequest {
                    invocation_id: pending.invocation_id.clone(),
                    credential: Some(credential),
                    result: Some(result_envelope),
                    result_digest_sha256: result_digest,
                })
                .await
                .map_err(|error| ConnectorError::ClientSdk(error.to_string()))?;

            if submit.accepted {
                pending.stage = InvocationStage::Submitted;
                self.journal.persist(pending)?;
                return Ok(true);
            }
            if attempt == 0 && pending.outcome_status == ExecutionOutcomeStatus::Success as i32 {
                pending.stage = InvocationStage::ResultReady;
                pending.outcome_status = ExecutionOutcomeStatus::Failed as i32;
                pending.response.clear();
                pending.error_message =
                    "Authority rejected Product response schema or scope".into();
                self.journal.persist(pending.clone())?;
                continue;
            }
            return Err(ConnectorError::Invocation(
                "Authority rejected durable result submission".to_string(),
            ));
        }
        Err(ConnectorError::Invocation(
            "Authority did not accept the durable result".to_string(),
        ))
    }
}

fn validate_credential_binding(
    envelope: &CanonicalInvocationEnvelope,
    credential: &ExecutionCredential,
) -> Result<(), ConnectorError> {
    let invocation = envelope.invocation.as_ref().ok_or_else(|| {
        ConnectorError::Invocation("canonical envelope has no invocation".to_string())
    })?;
    let target = envelope.target.as_ref().ok_or_else(|| {
        ConnectorError::Invocation("canonical envelope has no execution target".to_string())
    })?;
    let authorization_source =
        InvocationAuthorizationSource::try_from(envelope.authorization_source).map_err(|_| {
            ConnectorError::Invocation("Authority returned an unknown authorization source".into())
        })?;
    let delegated_scope_valid = match authorization_source {
        InvocationAuthorizationSource::Delegation => {
            !envelope.delegation_id.is_empty()
                && envelope.delegation_generation > 0
                && !envelope.delegation_audience.trim().is_empty()
                && credential.delegation_id == envelope.delegation_id
                && credential.delegation_generation == envelope.delegation_generation
                && credential.delegation_audience == envelope.delegation_audience
                && envelope.session_id.is_empty()
                && envelope.session_generation == 0
                && credential.session_id.is_empty()
                && credential.session_generation == 0
        }
        InvocationAuthorizationSource::Unspecified
        | InvocationAuthorizationSource::InteractiveSession => {
            !envelope.session_id.is_empty()
                && envelope.session_generation > 0
                && credential.session_id == envelope.session_id
                && credential.session_generation == envelope.session_generation
                && envelope.delegation_id.is_empty()
                && envelope.delegation_generation == 0
                && envelope.delegation_audience.is_empty()
                && credential.delegation_id.is_empty()
                && credential.delegation_generation == 0
                && credential.delegation_audience.is_empty()
        }
    };
    let invocation_digest = Sha256::digest(invocation.encode_to_vec());
    let envelope_digest = Sha256::digest(envelope.encode_to_vec());
    let required = [
        &envelope.invocation_id,
        &envelope.organization_id,
        &envelope.workspace_id,
        &envelope.principal_issuer,
        &envelope.principal_subject,
        &invocation.owner_id,
        &invocation.operation_id,
        &target.target_component,
        &target.scope,
        &target.execution_device_id,
    ];
    if required.iter().any(|value| value.is_empty())
        || credential.invocation_id != envelope.invocation_id
        || credential.organization_id != envelope.organization_id
        || credential.workspace_id != envelope.workspace_id
        || credential.principal_issuer != envelope.principal_issuer
        || credential.principal_subject != envelope.principal_subject
        || credential.operation_owner_id != invocation.owner_id
        || credential.operation_id != invocation.operation_id
        || credential.scope != target.scope
        || credential.resource_id != target.resource_id
        || credential.target_component != target.target_component
        || credential.http_method != target.http_method
        || credential.endpoint != target.endpoint
        || credential.execution_device_id != envelope.execution_device_id
        || credential.execution_device_generation != envelope.execution_device_generation
        || credential.execution_authorization_id != envelope.execution_authorization_id
        || credential.authorization_source != envelope.authorization_source
        || !delegated_scope_valid
        || target.execution_device_id != envelope.execution_device_id
        || target.execution_device_generation != envelope.execution_device_generation
        || target.execution_authorization_id != envelope.execution_authorization_id
        || target.execution_device_certificate_sha256
            != envelope.execution_device_certificate_sha256
        || credential.execution_device_certificate_sha256
            != envelope.execution_device_certificate_sha256
        || target.execution_authorization_id.is_empty()
        || target.execution_device_generation == 0
        || credential.contract_activation_generation != envelope.contract_activation_generation
        || credential.idempotency_key != target.idempotency_key
        || credential.idempotency_semantics != target.idempotency_semantics
        || !matches!(
            IdempotencySemantics::try_from(credential.idempotency_semantics),
            Ok(IdempotencySemantics::NotSupported
                | IdempotencySemantics::Required
                | IdempotencySemantics::Optional)
        )
        || credential.invocation_digest_sha256.as_slice() != &invocation_digest[..]
        || credential.request_digest_sha256.as_slice() != &envelope_digest[..]
        || credential.signing_key_id.is_empty()
        || credential.signing_key_id.len() > 128
        || credential.authority_signature.len() != 64
        || credential.execution_device_certificate_sha256.len() != 32
        || target.http_method.is_empty()
        || target.endpoint.is_empty()
        || target.execution_authorization_id.len() != 16
        || target.execution_device_certificate_sha256.len() != 32
        || envelope.contract_activation_generation == 0
        || credential.contract_activation_generation == 0
    {
        return Err(ConnectorError::Invocation(
            "Authority credential does not bind the canonical call".to_string(),
        ));
    }
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|error| ConnectorError::Invocation(error.to_string()))?
        .as_secs();
    let issued = envelope
        .issued_at
        .as_ref()
        .ok_or_else(|| ConnectorError::Invocation("canonical issue time is missing".into()))?;
    let expires = envelope
        .expires_at
        .as_ref()
        .ok_or_else(|| ConnectorError::Invocation("canonical expiry is missing".into()))?;
    let credential_issued = credential
        .issued_at
        .as_ref()
        .ok_or_else(|| ConnectorError::Invocation("credential issue time is missing".into()))?;
    let credential_expires = credential
        .expires_at
        .as_ref()
        .ok_or_else(|| ConnectorError::Invocation("credential expiry is missing".into()))?;
    if issued != credential_issued
        || expires != credential_expires
        || issued.seconds < 0
        || expires.seconds <= 0
        || !(0..1_000_000_000).contains(&issued.nanos)
        || !(0..1_000_000_000).contains(&expires.nanos)
        || issued.seconds as u64 > now.saturating_add(MAX_CLOCK_SKEW_SECONDS)
        || expires.seconds as u64 <= now
        || expires.seconds.saturating_sub(issued.seconds) as u64 > MAX_CREDENTIAL_TTL_SECONDS
        || credential.request_digest_sha256.len() != 32
        || credential.invocation_digest_sha256.len() != 32
    {
        return Err(ConnectorError::Invocation(
            "Authority credential time or digest fields are invalid".to_string(),
        ));
    }
    Ok(())
}

fn require_negotiated_profile(
    authorization_source: i32,
    delegated_invocations_negotiated: bool,
) -> Result<(), ConnectorError> {
    match InvocationAuthorizationSource::try_from(authorization_source) {
        Ok(InvocationAuthorizationSource::Delegation) if !delegated_invocations_negotiated => {
            Err(ConnectorError::Invocation(
                "Authority profile did not negotiate delegated invocation support".to_string(),
            ))
        }
        Ok(_) => Ok(()),
        Err(_) => Err(ConnectorError::Invocation(
            "Authority returned an unknown authorization source".to_string(),
        )),
    }
}

fn target_is_idempotent(target: Option<&ExecutionTarget>) -> bool {
    target.is_some_and(|target| {
        !target.idempotency_key.is_empty()
            && matches!(
                IdempotencySemantics::try_from(target.idempotency_semantics),
                Ok(IdempotencySemantics::Required | IdempotencySemantics::Optional)
            )
    })
}

fn safe_component(value: &str) -> String {
    let normalized: String = value
        .chars()
        .filter(|character| character.is_ascii_alphanumeric() || *character == '-')
        .take(48)
        .collect();
    if normalized.is_empty() {
        "device".to_string()
    } else {
        normalized
    }
}

fn hex_digest(bytes: &[u8]) -> String {
    let mut value = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        use std::fmt::Write;
        let _ = write!(value, "{byte:02x}");
    }
    value
}

#[cfg(test)]
mod delegation_credential_tests {
    use super::{
        require_negotiated_profile, validate_credential_binding, ConnectorError,
        InvocationAuthorizationSource,
    };
    use crate::{
        CanonicalInvocationEnvelope, ExecutionCredential, ExecutionTarget, IdempotencySemantics,
        ProductApiInvocationV2,
    };
    use prost::Message;
    use sha2::{Digest, Sha256};
    use std::time::{SystemTime, UNIX_EPOCH};

    fn credential_pair(
        source: InvocationAuthorizationSource,
    ) -> (CanonicalInvocationEnvelope, ExecutionCredential) {
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("system clock")
            .as_secs() as i64;
        let invocation = ProductApiInvocationV2 {
            owner_id: "exchange".into(),
            operation_id: "integration.issue.comment".into(),
            json_body: br#"{"issueNumber":12,"body":"safe test"}"#.to_vec(),
            resource_id: "repo-12#12".into(),
            idempotency_key: "integration-op-12".into(),
        };
        let target = ExecutionTarget {
            target_component: "exchange".into(),
            http_method: "POST".into(),
            endpoint: "https://exchange.example.test/api/v1/integrations/action".into(),
            resource_id: "repo-12#12".into(),
            scope: "organization=org-1;workspace=ws-1;resource=repo-12#12".into(),
            idempotency_key: "integration-op-12".into(),
            idempotency_semantics: IdempotencySemantics::Required as i32,
            execution_device_id: "connector-1".into(),
            execution_device_generation: 2,
            execution_authorization_id: vec![0x11; 16],
            execution_device_certificate_sha256: vec![0x22; 32],
        };
        let delegated = source == InvocationAuthorizationSource::Delegation;
        let envelope = CanonicalInvocationEnvelope {
            invocation_id: "invocation-12".into(),
            invocation: Some(invocation),
            target: Some(target),
            organization_id: "org-1".into(),
            workspace_id: "ws-1".into(),
            principal_issuer: "https://identity.example.test".into(),
            principal_subject: "actor-12".into(),
            execution_device_id: "connector-1".into(),
            execution_device_generation: 2,
            execution_authorization_id: vec![0x11; 16],
            execution_device_certificate_sha256: vec![0x22; 32],
            session_id: if delegated {
                String::new()
            } else {
                "session-12".into()
            },
            session_generation: if delegated { 0 } else { 3 },
            contract_activation_generation: 9,
            issued_at: Some(prost_types::Timestamp {
                seconds: now,
                nanos: 0,
            }),
            expires_at: Some(prost_types::Timestamp {
                seconds: now + 180,
                nanos: 0,
            }),
            authorization_source: source as i32,
            delegation_id: if delegated {
                "00000000-0000-0000-0000-000000000012".into()
            } else {
                String::new()
            },
            delegation_generation: if delegated { 4 } else { 0 },
            delegation_audience: if delegated {
                "exchange-worker".into()
            } else {
                String::new()
            },
        };
        let invocation_digest = Sha256::digest(
            envelope
                .invocation
                .as_ref()
                .expect("invocation")
                .encode_to_vec(),
        )
        .to_vec();
        let request_digest = Sha256::digest(envelope.encode_to_vec()).to_vec();
        let target = envelope.target.as_ref().expect("target");
        let credential = ExecutionCredential {
            invocation_id: envelope.invocation_id.clone(),
            invocation_digest_sha256: invocation_digest,
            request_digest_sha256: request_digest,
            organization_id: envelope.organization_id.clone(),
            workspace_id: envelope.workspace_id.clone(),
            principal_issuer: envelope.principal_issuer.clone(),
            principal_subject: envelope.principal_subject.clone(),
            operation_owner_id: "exchange".into(),
            operation_id: "integration.issue.comment".into(),
            scope: target.scope.clone(),
            resource_id: target.resource_id.clone(),
            target_component: target.target_component.clone(),
            http_method: target.http_method.clone(),
            endpoint: target.endpoint.clone(),
            execution_device_id: envelope.execution_device_id.clone(),
            execution_device_generation: 2,
            execution_authorization_id: vec![0x11; 16],
            execution_device_certificate_sha256: vec![0x22; 32],
            session_id: envelope.session_id.clone(),
            session_generation: envelope.session_generation,
            contract_activation_generation: 9,
            idempotency_key: target.idempotency_key.clone(),
            idempotency_semantics: target.idempotency_semantics,
            signing_key_id: "authority-key-1".into(),
            issued_at: envelope.issued_at,
            expires_at: envelope.expires_at,
            authority_signature: vec![0x33; 64],
            authorization_source: envelope.authorization_source,
            delegation_id: envelope.delegation_id.clone(),
            delegation_generation: envelope.delegation_generation,
            delegation_audience: envelope.delegation_audience.clone(),
        };
        (envelope, credential)
    }

    #[test]
    fn delegated_signature_digest_binds_grant_source_generation_and_audience() {
        let (envelope, credential) = credential_pair(InvocationAuthorizationSource::Delegation);
        validate_credential_binding(&envelope, &credential).expect("valid delegated scope");

        let mut changed = credential.clone();
        changed.delegation_generation += 1;
        assert!(validate_credential_binding(&envelope, &changed).is_err());

        let mut changed_audience = credential;
        changed_audience.delegation_audience = "another-workload".into();
        assert!(validate_credential_binding(&envelope, &changed_audience).is_err());
    }

    #[test]
    fn delegated_credentials_fail_when_negotiation_does_not_advertise_the_profile() {
        let source = InvocationAuthorizationSource::Delegation as i32;
        assert!(matches!(
            require_negotiated_profile(source, false),
            Err(ConnectorError::Invocation(_))
        ));
        assert!(require_negotiated_profile(source, true).is_ok());

        let legacy_interactive = InvocationAuthorizationSource::Unspecified as i32;
        assert!(require_negotiated_profile(legacy_interactive, false).is_ok());
    }

    #[test]
    fn legacy_interactive_credentials_keep_delegation_fields_empty() {
        let (envelope, credential) = credential_pair(InvocationAuthorizationSource::Unspecified);
        assert!(envelope.delegation_id.is_empty());
        assert_eq!(envelope.delegation_generation, 0);
        assert!(envelope.delegation_audience.is_empty());
        validate_credential_binding(&envelope, &credential)
            .expect("legacy interactive envelope remains compatible");
    }
}
