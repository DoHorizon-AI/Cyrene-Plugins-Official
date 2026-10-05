//! ┌─────────────────────────────────────────────────────────────────────┐
//! │  📄 connector_authority_integration.rs                              │
//! │  Test: Connector authority.v2 lifecycle and outcome classification. │
//! └─────────────────────────────────────────────────────────────────────┘

use cyrene_plugin_contracts::workspace_authority_v2::{
    workspace_authority_service_server::{
        WorkspaceAuthorityService, WorkspaceAuthorityServiceServer,
    },
    AcknowledgeDeliveryRequest, AcknowledgeDeliveryResponse, ApproveAndEnqueueInvocationRequest,
    ApproveAndEnqueueInvocationResponse, ApprovedInvocation, CanonicalInvocationEnvelope,
    CatalogSnapshotRequest, CatalogSnapshotResponse, ClaimInvocationsRequest,
    ClaimInvocationsResponse, ExecutionCredential, ExecutionOutcomeStatus, IdempotencySemantics,
    InvocationState, NegotiateVersionRequest, NegotiateVersionResponse,
    SubmitInvocationResultRequest, SubmitInvocationResultResponse,
    ValidateExecutionAuthorizationRequest, ValidateExecutionAuthorizationResponse,
    VerifyIdentityRequest, VerifyIdentityResponse, WaitInvocationResultRequest,
    WaitInvocationResultResponse,
};
use cyrene_plugin_contracts::workspace_product_v2::ProductApiInvocationV2;
use cyrene_workspace_client_sdk::AuthorityClient;
use cyrene_workspace_connector::WorkspaceConnectorWorker;
use cyrene_workspace_product_adapters::{GenericProductHttpAdapter, ProtectedCredentialProvider};
use prost::Message;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::Path;
use std::sync::{Arc, Mutex};
use std::time::{SystemTime, UNIX_EPOCH};
use tonic::transport::Server;
use tonic::{Request, Response, Status};

#[derive(Default)]
struct MockAuthorityState {
    enqueued: Vec<ApprovedInvocation>,
    invocations: HashMap<String, ApprovedInvocation>,
    acknowledged: Vec<(String, Vec<u8>)>,
    submitted_results: Vec<(String, ExecutionOutcomeStatus)>,
    result_bindings_valid: Vec<bool>,
}

#[derive(Clone, Default)]
struct MockAuthorityService {
    state: Arc<Mutex<MockAuthorityState>>,
}

#[tonic::async_trait]
impl WorkspaceAuthorityService for MockAuthorityService {
    async fn negotiate_version(
        &self,
        _request: Request<NegotiateVersionRequest>,
    ) -> Result<Response<NegotiateVersionResponse>, Status> {
        Ok(Response::new(NegotiateVersionResponse {
            selected_version: 2,
            supported_versions: vec![2],
        }))
    }

    async fn verify_identity(
        &self,
        _request: Request<VerifyIdentityRequest>,
    ) -> Result<Response<VerifyIdentityResponse>, Status> {
        Ok(Response::new(VerifyIdentityResponse {
            principal_id: "user-test".to_string(),
            issuer: "issuer-test".to_string(),
            subject: "subject-test".to_string(),
            organization_id: "org-main".to_string(),
            workspace_id: "ws-main".to_string(),
            session_id: "session-1".to_string(),
            session_generation: 1,
            permitted_workspaces: vec!["ws-main".to_string()],
            directory_roles: vec!["admin".to_string()],
        }))
    }

    async fn get_catalog_snapshot(
        &self,
        _request: Request<CatalogSnapshotRequest>,
    ) -> Result<Response<CatalogSnapshotResponse>, Status> {
        Ok(Response::new(CatalogSnapshotResponse::default()))
    }

    async fn approve_and_enqueue_invocation(
        &self,
        _request: Request<ApproveAndEnqueueInvocationRequest>,
    ) -> Result<Response<ApproveAndEnqueueInvocationResponse>, Status> {
        Ok(Response::new(ApproveAndEnqueueInvocationResponse {
            invocation_id: String::new(),
            state: InvocationState::Unspecified as i32,
        }))
    }

    async fn claim_invocations(
        &self,
        _request: Request<ClaimInvocationsRequest>,
    ) -> Result<Response<ClaimInvocationsResponse>, Status> {
        let mut state = self.state.lock().unwrap();
        Ok(Response::new(ClaimInvocationsResponse {
            invocations: state.enqueued.drain(..).collect(),
        }))
    }

    async fn validate_execution_authorization(
        &self,
        request: Request<ValidateExecutionAuthorizationRequest>,
    ) -> Result<Response<ValidateExecutionAuthorizationResponse>, Status> {
        let credential = request
            .into_inner()
            .credential
            .ok_or_else(|| Status::invalid_argument("missing credential"))?;
        let state = self.state.lock().unwrap();
        Ok(Response::new(ValidateExecutionAuthorizationResponse {
            approved_invocation: state.invocations.get(&credential.invocation_id).cloned(),
        }))
    }

    async fn acknowledge_delivery(
        &self,
        request: Request<AcknowledgeDeliveryRequest>,
    ) -> Result<Response<AcknowledgeDeliveryResponse>, Status> {
        let request = request.into_inner();
        self.state
            .lock()
            .unwrap()
            .acknowledged
            .push((request.invocation_id, request.delivery_receipt));
        Ok(Response::new(AcknowledgeDeliveryResponse {
            acknowledged: true,
            state: InvocationState::Acknowledged as i32,
        }))
    }

    async fn submit_invocation_result(
        &self,
        request: Request<SubmitInvocationResultRequest>,
    ) -> Result<Response<SubmitInvocationResultResponse>, Status> {
        let request = request.into_inner();
        let outcome = request
            .result
            .as_ref()
            .and_then(|result| ExecutionOutcomeStatus::try_from(result.outcome_status).ok())
            .unwrap_or(ExecutionOutcomeStatus::Unspecified);
        let result_binding_valid = request.result.as_ref().is_some_and(|result| {
            let digest = Sha256::digest(result.encode_to_vec());
            request.result_digest_sha256.as_slice() == &digest[..]
                && result.invocation_id == request.invocation_id
                && request.credential.as_ref().is_some_and(|credential| {
                    result.request_digest_sha256 == credential.request_digest_sha256
                })
        });
        let mut state = self.state.lock().unwrap();
        state
            .submitted_results
            .push((request.invocation_id, outcome));
        state.result_bindings_valid.push(result_binding_valid);
        Ok(Response::new(SubmitInvocationResultResponse {
            accepted: true,
            state: InvocationState::Failed as i32,
            result_receipt: vec![1, 2, 3],
        }))
    }

    async fn wait_invocation_result(
        &self,
        _request: Request<WaitInvocationResultRequest>,
    ) -> Result<Response<WaitInvocationResultResponse>, Status> {
        Ok(Response::new(WaitInvocationResultResponse::default()))
    }
}

fn approved(invocation_id: &str, key: &str, endpoint: &str) -> ApprovedInvocation {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs() as i64;
    let invocation = ProductApiInvocationV2 {
        owner_id: "catalyst".to_string(),
        operation_id: "op_custom_analysis".to_string(),
        json_body: br#"{"query":"dynamic test"}"#.to_vec(),
        resource_id: String::new(),
        idempotency_key: key.to_string(),
    };
    let target = cyrene_plugin_contracts::workspace_authority_v2::ExecutionTarget {
        target_component: "catalyst".to_string(),
        http_method: "POST".to_string(),
        endpoint: endpoint.to_string(),
        resource_id: String::new(),
        scope: "workspace:ws-main:catalyst:op_custom_analysis".to_string(),
        idempotency_key: key.to_string(),
        idempotency_semantics: if key.is_empty() {
            IdempotencySemantics::NotSupported as i32
        } else {
            IdempotencySemantics::Required as i32
        },
        execution_device_id: "device-1".to_string(),
        execution_device_generation: 1,
        execution_authorization_id: vec![4; 16],
        execution_device_certificate_sha256: vec![5; 32],
    };
    let envelope = CanonicalInvocationEnvelope {
        invocation_id: invocation_id.to_string(),
        invocation: Some(invocation),
        target: Some(target),
        organization_id: "org-main".to_string(),
        workspace_id: "ws-main".to_string(),
        principal_issuer: "issuer-test".to_string(),
        principal_subject: "subject-test".to_string(),
        execution_device_id: "device-1".to_string(),
        execution_device_generation: 1,
        execution_authorization_id: vec![4; 16],
        execution_device_certificate_sha256: vec![5; 32],
        session_id: "session-1".to_string(),
        session_generation: 1,
        contract_activation_generation: 1,
        issued_at: Some(prost_types::Timestamp {
            seconds: now,
            nanos: 0,
        }),
        expires_at: Some(prost_types::Timestamp {
            seconds: now + 300,
            nanos: 0,
        }),
    };
    let invocation_digest =
        Sha256::digest(envelope.invocation.as_ref().unwrap().encode_to_vec()).to_vec();
    let request_digest = Sha256::digest(envelope.encode_to_vec()).to_vec();
    let credential = ExecutionCredential {
        invocation_id: invocation_id.to_string(),
        invocation_digest_sha256: invocation_digest,
        request_digest_sha256: request_digest,
        organization_id: envelope.organization_id.clone(),
        workspace_id: envelope.workspace_id.clone(),
        principal_issuer: envelope.principal_issuer.clone(),
        principal_subject: envelope.principal_subject.clone(),
        operation_owner_id: "catalyst".to_string(),
        operation_id: "op_custom_analysis".to_string(),
        scope: envelope.target.as_ref().unwrap().scope.clone(),
        resource_id: String::new(),
        target_component: "catalyst".to_string(),
        http_method: "POST".to_string(),
        endpoint: endpoint.to_string(),
        execution_device_id: envelope.execution_device_id.clone(),
        execution_device_generation: 1,
        execution_authorization_id: vec![4; 16],
        execution_device_certificate_sha256: vec![5; 32],
        session_id: "session-1".to_string(),
        session_generation: 1,
        contract_activation_generation: 1,
        idempotency_key: key.to_string(),
        idempotency_semantics: envelope.target.as_ref().unwrap().idempotency_semantics,
        signing_key_id: "test-key".to_string(),
        issued_at: envelope.issued_at,
        expires_at: envelope.expires_at,
        authority_signature: vec![9; 64],
    };
    // The test authority returns the exact canonical data expected from redemption.
    ApprovedInvocation {
        invocation_id: invocation_id.to_string(),
        canonical_envelope: Some(envelope),
        credential: Some(credential),
    }
}

fn hex(bytes: &[u8]) -> String {
    let mut value = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        use std::fmt::Write;
        let _ = write!(value, "{byte:02x}");
    }
    value
}

fn credential_map_row(credential: &ExecutionCredential, bearer_token: &str) -> Value {
    let scheme_end = credential.endpoint.find("://").unwrap() + 3;
    let authority_end = credential.endpoint[scheme_end..]
        .find('/')
        .map(|offset| scheme_end + offset)
        .unwrap_or(credential.endpoint.len());
    json!({
        "organizationId": credential.organization_id,
        "workspaceId": credential.workspace_id,
        "operationOwnerId": credential.operation_owner_id,
        "operationId": credential.operation_id,
        "scope": credential.scope,
        "resourceConstraint": { "kind": "authorityApprovedResource" },
        "targetComponent": credential.target_component,
        "httpMethod": credential.http_method,
        "endpointOrigin": &credential.endpoint[..authority_end],
        "executionDeviceId": credential.execution_device_id,
        "executionDeviceGeneration": credential.execution_device_generation,
        "executionAuthorizationIdHex": hex(&credential.execution_authorization_id),
        "executionDeviceCertificateSha256Hex": hex(&credential.execution_device_certificate_sha256),
        "contractActivationGeneration": credential.contract_activation_generation,
        "bearerToken": bearer_token,
    })
}

fn write_credential_map(path: &Path, rows: Vec<Value>) {
    let contents = serde_json::to_vec(&json!({ "version": 2, "credentials": rows })).unwrap();
    let mut file = OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .mode(0o600)
        .open(path)
        .unwrap();
    file.write_all(&contents).unwrap();
    file.sync_all().unwrap();
    fs::set_permissions(path, fs::Permissions::from_mode(0o600)).unwrap();
}

#[tokio::test]
async fn connector_persists_receipts_and_classifies_uncertain_results() {
    let mock_authority = MockAuthorityService::default();
    let state = Arc::clone(&mock_authority.state);
    let product_listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let product_address = product_listener.local_addr().unwrap();
    let product_endpoint = format!("http://{product_address}/api/custom-analysis");
    let product_origin = format!("http://{product_address}");
    let product_authorization = Arc::new(Mutex::new(None));
    let captured_authorization = Arc::clone(&product_authorization);
    tokio::spawn(async move {
        let (mut stream, _) = product_listener.accept().await.unwrap();
        let mut request = Vec::new();
        let mut chunk = [0_u8; 1024];
        loop {
            let count = tokio::io::AsyncReadExt::read(&mut stream, &mut chunk)
                .await
                .unwrap();
            if count == 0 {
                break;
            }
            request.extend_from_slice(&chunk[..count]);
            if request.windows(4).any(|window| window == b"\r\n\r\n") {
                break;
            }
        }
        let authorization = String::from_utf8_lossy(&request).lines().find_map(|line| {
            let (name, value) = line.split_once(':')?;
            name.eq_ignore_ascii_case("authorization")
                .then(|| value.trim().to_string())
        });
        *captured_authorization.lock().unwrap() = authorization;
        let body = br#"{"accepted":true}"#;
        let response = format!(
            "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{}",
            body.len(),
            std::str::from_utf8(body).unwrap()
        );
        tokio::io::AsyncWriteExt::write_all(&mut stream, response.as_bytes())
            .await
            .unwrap();
    });
    let non_idempotent = approved(
        "inv-non-idempotent",
        "",
        "http://127.0.0.1:9/api/custom-analysis",
    );
    let idempotent = approved(
        "inv-idempotent",
        "idempotency-2",
        "http://127.0.0.1:9/api/custom-analysis",
    );
    let successful = approved("inv-success", "", &product_endpoint);
    let credentials_for_map = [non_idempotent.clone(), successful.clone()];
    {
        let mut state = state.lock().unwrap();
        for invocation in [non_idempotent, idempotent, successful] {
            state
                .invocations
                .insert(invocation.invocation_id.clone(), invocation.clone());
            state.enqueued.push(invocation);
        }
    }

    let temp_dir = tempfile::tempdir().unwrap();
    let socket_path = temp_dir.path().join("authority.sock");
    let uds = tokio::net::UnixListener::bind(&socket_path).unwrap();
    let uds_stream = tokio_stream::wrappers::UnixListenerStream::new(uds);
    tokio::spawn(async move {
        Server::builder()
            .add_service(WorkspaceAuthorityServiceServer::new(mock_authority))
            .serve_with_incoming(uds_stream)
            .await
            .unwrap();
    });

    tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    let client = AuthorityClient::connect_uds(&socket_path).await.unwrap();
    let credential_map_path = temp_dir.path().join("workspace-product-credentials.json");
    let map_rows = credentials_for_map
        .iter()
        .map(|approved| {
            credential_map_row(
                approved.credential.as_ref().unwrap(),
                if approved.invocation_id == "inv-success" {
                    "test-workspace-token-9876543210zyxwvutsrqpon"
                } else {
                    "test-workspace-token-0123456789abcdefghij"
                },
            )
        })
        .collect();
    write_credential_map(&credential_map_path, map_rows);
    let credential_provider = ProtectedCredentialProvider::new(&credential_map_path).unwrap();
    let adapter = Arc::new(GenericProductHttpAdapter::new(
        std::time::Duration::from_secs(1),
        vec!["http://127.0.0.1:9".to_string(), product_origin],
        credential_provider,
    ));
    let journal_path = temp_dir.path().join("private/journal.bin");
    let mut worker = WorkspaceConnectorWorker::new(
        "ws-main",
        vec!["catalyst".to_string()],
        client,
        adapter,
        &journal_path,
    )
    .unwrap();

    let processed = worker.process_batch(10).await.unwrap();
    assert_eq!(processed, 3);

    let state = state.lock().unwrap();
    assert_eq!(state.acknowledged.len(), 3);
    assert!(String::from_utf8_lossy(&state.acknowledged[0].1).starts_with("rcpt-device-1-"));
    assert_eq!(
        state.submitted_results[0].1,
        ExecutionOutcomeStatus::UnknownResult
    );
    assert_eq!(state.submitted_results[1].1, ExecutionOutcomeStatus::Failed);
    assert_eq!(
        state.submitted_results[2].1,
        ExecutionOutcomeStatus::Success
    );
    assert_eq!(state.result_bindings_valid, vec![true, true, true]);
    assert!(
        product_authorization.lock().unwrap().as_deref()
            == Some("Bearer test-workspace-token-9876543210zyxwvutsrqpon")
    );
    let journal_bytes = fs::read(journal_path).unwrap();
    for test_token in [
        b"test-workspace-token-0123456789abcdefghij".as_slice(),
        b"test-workspace-token-9876543210zyxwvutsrqpon".as_slice(),
    ] {
        assert!(!journal_bytes
            .windows(test_token.len())
            .any(|window| window == test_token));
    }
}
