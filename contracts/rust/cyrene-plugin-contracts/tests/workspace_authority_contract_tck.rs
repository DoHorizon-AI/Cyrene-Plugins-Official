// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 workspace_authority_contract_tck.rs                             │
// │  Package: cyrene_plugin_contracts                                  │
// │  Role: TCK tests for cyrene.workspace.authority.v1 contract.        │
// │                                                                     │
// │  测试职责：验证 Authority RPC 契约的编解码、凭据一致性及未知结果判定。  │
// └─────────────────────────────────────────────────────────────────────┘

use cyrene_plugin_contracts::workspace_authority_v1::*;
use cyrene_plugin_contracts::workspace_product_v2::*;
use prost::Message;

#[test]
fn delivery_credential_round_trips_deterministically() {
    let cred = DeliveryCredential {
        invocation_id: "inv-1001".to_string(),
        request_digest_sha256: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
            .to_string(),
        target_component: "cyrene-workspace-connector".to_string(),
        workspace_id: "ws-alpha".to_string(),
        device_generation: 12,
        session_generation: 3,
        contract_activation_generation: 42,
        expires_at: Some(prost_types::Timestamp {
            seconds: 1800000000,
            nanos: 0,
        }),
        authority_signature: vec![1, 2, 3, 4, 5, 6, 7, 8],
    };

    let encoded = cred.encode_to_vec();
    let decoded = DeliveryCredential::decode(encoded.as_slice()).expect("decode credential");

    assert_eq!(decoded.invocation_id, "inv-1001");
    assert_eq!(decoded.target_component, "cyrene-workspace-connector");
    assert_eq!(decoded.workspace_id, "ws-alpha");
    assert_eq!(decoded.device_generation, 12);
    assert_eq!(decoded.session_generation, 3);
    assert_eq!(decoded.contract_activation_generation, 42);
    assert_eq!(decoded.authority_signature, vec![1, 2, 3, 4, 5, 6, 7, 8]);
    assert_eq!(decoded.expires_at.unwrap().seconds, 1800000000);
}

#[test]
fn approved_invocation_carries_opaque_delivery_contract() {
    let raw_invocation = ProductApiInvocationV2 {
        owner_id: "catalyst".to_string(),
        operation_id: "list_datasets".to_string(),
        json_body: br#"{"limit":10}"#.to_vec(),
        resource_id: "".to_string(),
        idempotency_key: "replay-key-99".to_string(),
    };

    let approved = ApprovedInvocation {
        invocation_id: "inv-2002".to_string(),
        credential: Some(DeliveryCredential {
            invocation_id: "inv-2002".to_string(),
            request_digest_sha256: "sha256:feedbeef".to_string(),
            target_component: "cyrene-workspace-connector".to_string(),
            workspace_id: "ws-dev".to_string(),
            device_generation: 1,
            session_generation: 1,
            contract_activation_generation: 10,
            expires_at: None,
            authority_signature: vec![42],
        }),
        raw_invocation: Some(raw_invocation),
        resolved_target_url: "http://127.0.0.1:18014/api/v1/datasets".to_string(),
        timeout_seconds: 30,
    };

    let encoded = approved.encode_to_vec();
    let decoded =
        ApprovedInvocation::decode(encoded.as_slice()).expect("decode approved invocation");

    assert_eq!(decoded.invocation_id, "inv-2002");
    assert_eq!(
        decoded.resolved_target_url,
        "http://127.0.0.1:18014/api/v1/datasets"
    );
    assert_eq!(decoded.timeout_seconds, 30);

    let raw = decoded.raw_invocation.expect("raw_invocation must exist");
    assert_eq!(raw.owner_id, "catalyst");
    assert_eq!(raw.operation_id, "list_datasets");
    assert_eq!(raw.idempotency_key, "replay-key-99");
}

#[test]
fn submit_result_supports_unknown_result_status_for_non_idempotent_loss() {
    let req = SubmitInvocationResultRequest {
        invocation_id: "inv-3003".to_string(),
        credential: None,
        outcome_status: ExecutionOutcomeStatus::UnknownResult as i32,
        product_response: None,
        error_message: "Connection lost while waiting for upstream Product response".to_string(),
    };

    let encoded = req.encode_to_vec();
    let decoded = SubmitInvocationResultRequest::decode(encoded.as_slice()).expect("decode");

    assert_eq!(
        decoded.outcome_status,
        ExecutionOutcomeStatus::UnknownResult as i32
    );
    assert!(decoded.error_message.contains("Connection lost"));
}

#[test]
fn catalog_view_projection_preserves_monotonic_generation() {
    let resp = GetCatalogViewResponse {
        catalog_generation: 99,
        operations: vec![
            OperationView {
                owner_id: "catalyst".to_string(),
                operation_id: "list_datasets".to_string(),
                description: "List datasets".to_string(),
                requires_idempotency_key: false,
                is_read_only: true,
            },
            OperationView {
                owner_id: "yield".to_string(),
                operation_id: "create_training_draft".to_string(),
                description: "Create draft".to_string(),
                requires_idempotency_key: true,
                is_read_only: false,
            },
        ],
    };

    let encoded = resp.encode_to_vec();
    let decoded = GetCatalogViewResponse::decode(encoded.as_slice()).expect("decode");

    assert_eq!(decoded.catalog_generation, 99);
    assert_eq!(decoded.operations.len(), 2);
    assert!(decoded.operations[0].is_read_only);
    assert!(!decoded.operations[1].is_read_only);
    assert!(decoded.operations[1].requires_idempotency_key);
}
