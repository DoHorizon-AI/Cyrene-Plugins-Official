//! ┌─────────────────────────────────────────────────────────────────────┐
//! │  📄 connector_authority_integration.rs                             │
//! │  Test: Full integration between Connector and Authority RPC.       │
//! └─────────────────────────────────────────────────────────────────────┘

use std::sync::{Arc, Mutex};
use tonic::{Request, Response, Status};
use tonic::transport::Server;
use cyrene_plugin_contracts::workspace_authority_v1::{
    workspace_authority_service_server::{WorkspaceAuthorityService, WorkspaceAuthorityServiceServer},
    AcknowledgeDeliveryRequest, AcknowledgeDeliveryResponse,
    ApproveAndEnqueueInvocationRequest, ApproveAndEnqueueInvocationResponse,
    ApprovedInvocation, ClaimInvocationsRequest, ClaimInvocationsResponse,
    DeliveryCredential, DiscoverWorkspacesRequest, DiscoverWorkspacesResponse,
    ExecutionOutcomeStatus, GetCatalogViewRequest, GetCatalogViewResponse,
    SubmitInvocationResultRequest, SubmitInvocationResultResponse,
    VerifyIdentityRequest, VerifyIdentityResponse,
};
use cyrene_plugin_contracts::workspace_product_v2::ProductApiInvocationV2;
use cyrene_workspace_client_sdk::AuthorityClient;
use cyrene_workspace_connector::WorkspaceConnectorWorker;
use cyrene_workspace_product_adapters::GenericProductHttpAdapter;

#[derive(Default)]
struct MockAuthorityState {
    enqueued: Vec<ApprovedInvocation>,
    claimed: Vec<String>,
    acknowledged: Vec<(String, Vec<u8>)>,
    submitted_results: Vec<(String, ExecutionOutcomeStatus)>,
}

#[derive(Clone, Default)]
struct MockAuthorityService {
    state: Arc<Mutex<MockAuthorityState>>,
}

#[tonic::async_trait]
impl WorkspaceAuthorityService for MockAuthorityService {
    async fn verify_identity(
        &self,
        _req: Request<VerifyIdentityRequest>,
    ) -> Result<Response<VerifyIdentityResponse>, Status> {
        Ok(Response::new(VerifyIdentityResponse {
            valid: true,
            principal_id: "user-test".to_string(),
            session_generation: 1,
            device_generation: 1,
            permitted_workspaces: vec!["ws-main".to_string()],
            roles: vec!["admin".to_string()],
            error_reason: String::new(),
        }))
    }

    async fn discover_workspaces(
        &self,
        _req: Request<DiscoverWorkspacesRequest>,
    ) -> Result<Response<DiscoverWorkspacesResponse>, Status> {
        Ok(Response::new(DiscoverWorkspacesResponse {
            workspaces: vec![],
        }))
    }

    async fn approve_and_enqueue_invocation(
        &self,
        _req: Request<ApproveAndEnqueueInvocationRequest>,
    ) -> Result<Response<ApproveAndEnqueueInvocationResponse>, Status> {
        Ok(Response::new(ApproveAndEnqueueInvocationResponse {
            approved_invocation: None,
            error: None,
        }))
    }

    async fn claim_invocations(
        &self,
        _req: Request<ClaimInvocationsRequest>,
    ) -> Result<Response<ClaimInvocationsResponse>, Status> {
        let mut state = self.state.lock().unwrap();
        let enqueued: Vec<_> = state.enqueued.drain(..).collect();
        for inv in &enqueued {
            state.claimed.push(inv.invocation_id.clone());
        }
        Ok(Response::new(ClaimInvocationsResponse {
            invocations: enqueued,
        }))
    }

    async fn acknowledge_delivery(
        &self,
        req: Request<AcknowledgeDeliveryRequest>,
    ) -> Result<Response<AcknowledgeDeliveryResponse>, Status> {
        let r = req.into_inner();
        let mut state = self.state.lock().unwrap();
        state.acknowledged.push((r.invocation_id, r.delivery_receipt));
        Ok(Response::new(AcknowledgeDeliveryResponse {
            acknowledged: true,
        }))
    }

    async fn submit_invocation_result(
        &self,
        req: Request<SubmitInvocationResultRequest>,
    ) -> Result<Response<SubmitInvocationResultResponse>, Status> {
        let r = req.into_inner();
        let mut state = self.state.lock().unwrap();
        let status = ExecutionOutcomeStatus::try_from(r.outcome_status).unwrap_or(ExecutionOutcomeStatus::Unspecified);
        state.submitted_results.push((r.invocation_id, status));
        Ok(Response::new(SubmitInvocationResultResponse {
            accepted: true,
            validation_error: None,
        }))
    }

    async fn get_catalog_view(
        &self,
        _req: Request<GetCatalogViewRequest>,
    ) -> Result<Response<GetCatalogViewResponse>, Status> {
        Ok(Response::new(GetCatalogViewResponse {
            catalog_generation: 1,
            operations: vec![],
        }))
    }
}

#[tokio::test]
async fn test_connector_claims_and_executes_dynamic_product_operation() {
    let mock_authority = MockAuthorityService::default();
    let state = Arc::clone(&mock_authority.state);

    // 1. Non-idempotent operation (empty idempotency_key): on network drop -> UNKNOWN_RESULT
    let non_idemp_approved = ApprovedInvocation {
        invocation_id: "inv-non-idemp-1".to_string(),
        credential: Some(DeliveryCredential {
            invocation_id: "inv-non-idemp-1".to_string(),
            request_digest_sha256: "sha256:abcd".to_string(),
            target_component: "catalyst".to_string(),
            workspace_id: "ws-main".to_string(),
            device_generation: 1,
            session_generation: 1,
            contract_activation_generation: 1,
            expires_at: None,
            authority_signature: vec![1, 2, 3, 4],
        }),
        raw_invocation: Some(ProductApiInvocationV2 {
            owner_id: "catalyst".to_string(),
            operation_id: "op_custom_analysis".to_string(),
            json_body: br#"{"query": "dynamic_test"}"#.to_vec(),
            resource_id: String::new(),
            idempotency_key: String::new(), // Non-idempotent!
        }),
        resolved_target_url: "http://127.0.0.1:9".to_string(), // Unreachable -> simulates network drop
        timeout_seconds: 1,
    };

    // 2. Idempotent operation: on network drop -> FAILED (safe to retry)
    let idemp_approved = ApprovedInvocation {
        invocation_id: "inv-idemp-2".to_string(),
        credential: Some(DeliveryCredential {
            invocation_id: "inv-idemp-2".to_string(),
            request_digest_sha256: "sha256:ef01".to_string(),
            target_component: "catalyst".to_string(),
            workspace_id: "ws-main".to_string(),
            device_generation: 1,
            session_generation: 1,
            contract_activation_generation: 1,
            expires_at: None,
            authority_signature: vec![5, 6, 7, 8],
        }),
        raw_invocation: Some(ProductApiInvocationV2 {
            owner_id: "catalyst".to_string(),
            operation_id: "op_custom_analysis".to_string(),
            json_body: br#"{"query": "dynamic_test_2"}"#.to_vec(),
            resource_id: String::new(),
            idempotency_key: "idemp-key-2".to_string(), // Idempotent!
        }),
        resolved_target_url: "http://127.0.0.1:9".to_string(),
        timeout_seconds: 1,
    };

    {
        let mut st = state.lock().unwrap();
        st.enqueued.push(non_idemp_approved);
        st.enqueued.push(idemp_approved);
    }

    // Bind mock authority server to ephemeral UDS
    let tmp_dir = tempfile::tempdir().unwrap();
    let socket_path = tmp_dir.path().join("authority.sock");
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
    let http_adapter = Arc::new(GenericProductHttpAdapter::default());

    let mut worker = WorkspaceConnectorWorker::new(
        "conn-01",
        "ws-main",
        vec!["catalyst".to_string()],
        client,
        http_adapter,
    );

    let processed = worker.process_batch(10).await.unwrap();
    assert_eq!(processed, 2);

    let state_guard = state.lock().unwrap();
    assert_eq!(state_guard.claimed.len(), 2);
    assert_eq!(state_guard.claimed[0], "inv-non-idemp-1");
    assert_eq!(state_guard.claimed[1], "inv-idemp-2");

    // Both had receipts generated with connector prefix
    assert_eq!(state_guard.acknowledged.len(), 2);
    let r1 = String::from_utf8_lossy(&state_guard.acknowledged[0].1);
    let r2 = String::from_utf8_lossy(&state_guard.acknowledged[1].1);
    assert!(r1.starts_with("rcpt-conn-01-"));
    assert!(r2.starts_with("rcpt-conn-01-"));

    // Outcome for non-idempotent: UNKNOWN_RESULT (forbids auto-replay)
    assert_eq!(
        state_guard.submitted_results[0],
        ("inv-non-idemp-1".to_string(), ExecutionOutcomeStatus::UnknownResult)
    );

    // Outcome for idempotent: FAILED (permitted to retry)
    assert_eq!(
        state_guard.submitted_results[1],
        ("inv-idemp-2".to_string(), ExecutionOutcomeStatus::Failed)
    );
}
