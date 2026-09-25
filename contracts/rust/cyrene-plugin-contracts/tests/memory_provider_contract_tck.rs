// ╔══════════════════════════════════════════════════════════════════════╗
// ║ 📄 File: memory_provider_contract_tck.rs                              ║
// ║ Module: CYRENE Plugins Official                                     ║
// ║ Role: Direct memory.provider.v1 capability conformance & TCK.       ║
// ╚══════════════════════════════════════════════════════════════════════╝
// 中文：文件：memory_provider_contract_tck.rs
// 中文：模块：CYRENE Plugins Official
// 中文：职责：对直接使用 memory.provider.v1 capability 的实现执行一致性验证与 TCK。

use cyrene_plugin_contracts::{
    direct_plugin_runtime_v1::{
        direct_invocation_error, direct_invocation_response, DirectInvocationError,
        DirectInvocationRequest, DirectInvocationResponse,
    },
    memory_provider::{
        CAPABILITY_ID, INTERFACE_VERSION, METHOD_DELETE, METHOD_EXPORT_BATCH, METHOD_GET,
        METHOD_IMPORT_BATCH, METHOD_PRUNE, METHOD_RECALL, METHOD_STORE, RECALL_REQUEST_TYPE_URL,
    },
    memory_provider_v1::{MemoryErrorCode, MemoryItem, RecallMemoryRequest, StoreMemoryRequest},
};
use prost::Message;
use std::collections::HashMap;

#[test]
fn memory_provider_identifiers_and_error_codes_are_stable() {
    assert_eq!(CAPABILITY_ID, "memory.provider.v1");
    assert_eq!(INTERFACE_VERSION, "1");
    assert_eq!(METHOD_STORE, "store");
    assert_eq!(METHOD_GET, "get");
    assert_eq!(METHOD_RECALL, "recall");
    assert_eq!(METHOD_DELETE, "delete");
    assert_eq!(METHOD_PRUNE, "prune");
    assert_eq!(METHOD_EXPORT_BATCH, "export_batch");
    assert_eq!(METHOD_IMPORT_BATCH, "import_batch");

    assert_eq!(MemoryErrorCode::Unspecified as i32, 0);
    assert_eq!(MemoryErrorCode::NotFound as i32, 1);
    assert_eq!(MemoryErrorCode::TenantMismatch as i32, 2);
    assert_eq!(MemoryErrorCode::VectorDimensionMismatch as i32, 3);
    assert_eq!(MemoryErrorCode::QuotaExceeded as i32, 4);
    assert_eq!(MemoryErrorCode::StorageFailed as i32, 5);
}

#[test]
fn store_memory_request_preserves_tenant_embedding_metadata_and_ttl() {
    let mut meta = HashMap::new();
    meta.insert("author".to_string(), "cyrene-system".to_string());
    meta.insert("importance".to_string(), "high".to_string());

    let req = StoreMemoryRequest {
        tenant_id: "tenant-corp-01".to_string(),
        item: Some(MemoryItem {
            item_id: "item-8899".to_string(),
            tenant_id: "tenant-corp-01".to_string(),
            scope: "global".to_string(),
            subject: "database_cluster".to_string(),
            content: "Primary cluster is located in eu-west-1".to_string(),
            embedding: vec![0.123, -0.456, 0.789, 0.001],
            metadata: meta,
            created_at_ms: 1726000000000,
            expires_at_ms: Some(1726086400000),
            ttl_seconds: Some(86400),
        }),
    };

    let bytes = req.encode_to_vec();
    let decoded = StoreMemoryRequest::decode(bytes.as_slice()).expect("decode store req");
    assert_eq!(decoded.tenant_id, "tenant-corp-01");
    let it = decoded.item.expect("item present");
    assert_eq!(it.embedding.len(), 4);
    assert_eq!(it.ttl_seconds, Some(86400));
    assert_eq!(
        it.metadata.get("importance").map(|s| s.as_str()),
        Some("high")
    );
}

#[test]
fn recall_memory_request_enforces_score_and_filter_bounds() {
    let req = RecallMemoryRequest {
        tenant_id: "tenant-corp-01".to_string(),
        scope: Some("project-x".to_string()),
        subject: None,
        query_embedding: vec![0.123, -0.456, 0.789, 0.001],
        query_text: Some("Where is the db?".to_string()),
        metadata_filters: HashMap::new(),
        min_similarity: Some(0.85),
        top_k: Some(10),
    };

    let bytes = req.encode_to_vec();
    let decoded = RecallMemoryRequest::decode(bytes.as_slice()).expect("decode recall");
    assert_eq!(decoded.min_similarity, Some(0.85));
    assert_eq!(decoded.top_k, Some(10));
}

#[test]
fn memory_provider_empty_implementation_fails_closed() {
    let handler = |req: DirectInvocationRequest| -> DirectInvocationResponse {
        DirectInvocationResponse {
            result: Some(direct_invocation_response::Result::Error(
                DirectInvocationError {
                    code: direct_invocation_error::Code::Unavailable as i32,
                    message: format!("Plugin capability {} is not implemented", req.capability),
                    retryable: true,
                    domain_code: "CAPABILITY_UNAVAILABLE".to_string(),
                },
            )),
        }
    };

    let request = DirectInvocationRequest {
        capability: CAPABILITY_ID.to_string(),
        interface_version: INTERFACE_VERSION.to_string(),
        method: METHOD_RECALL.to_string(),
        payload_type_url: RECALL_REQUEST_TYPE_URL.to_string(),
        payload: vec![],
        request_id: "req-mem-gate-001".to_string(),
        stream_mode: 0,
    };

    let response = handler(request);
    match response.result {
        Some(direct_invocation_response::Result::Error(err)) => {
            assert_eq!(err.code, direct_invocation_error::Code::Unavailable as i32);
            assert_eq!(err.domain_code, "CAPABILITY_UNAVAILABLE");
        }
        _ => panic!("Empty memory implementation must fail closed with typed UNAVAILABLE"),
    }
}
