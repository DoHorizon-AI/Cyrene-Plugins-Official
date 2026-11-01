// ╔══════════════════════════════════════════════════════════════════════╗
// ║ 📄 File: knowledge_graph_contract_tck.rs                             ║
// ║ Module: CYRENE Plugins Official                                     ║
// ║ Role: knowledge.graph.v1 payload conformance and identifiers.        ║
// ║                                                                      ║
// ║ 模块：CYRENE Plugins Official                                       ║
// ║ 职责：验证 knowledge.graph.v1 负载及稳定标识符。                    ║
// ╚══════════════════════════════════════════════════════════════════════╝

use cyrene_plugin_contracts::{
    knowledge_graph::{
        CAPABILITY_ID, EXPORT_BATCH_REQUEST_TYPE_URL, INTERFACE_VERSION, METHOD_EXPAND,
        METHOD_EXPORT_BATCH, METHOD_UPSERT_NODES,
    },
    knowledge_graph_v1::{
        export_graph_response, ExportGraphBatch, ExportGraphRequest, ExportGraphResponse,
        GraphNode, UpsertNodesRequest,
    },
};
use prost::Message;
use std::collections::HashMap;

#[test]
fn graph_capability_methods_and_type_urls_are_stable() {
    assert_eq!(CAPABILITY_ID, "knowledge.graph.v1");
    assert_eq!(INTERFACE_VERSION, "1");
    assert_eq!(METHOD_UPSERT_NODES, "upsert_nodes");
    assert_eq!(METHOD_EXPAND, "expand");
    assert_eq!(METHOD_EXPORT_BATCH, "export_batch");
    assert_eq!(
        EXPORT_BATCH_REQUEST_TYPE_URL,
        "type.cyrene.io/cyrene.knowledge.graph.v1.ExportGraphRequest"
    );
}

#[test]
fn graph_node_source_metadata_and_export_cursor_round_trip() {
    let mut metadata = HashMap::new();
    metadata.insert("evidence_kind".to_string(), "message".to_string());
    let request = UpsertNodesRequest {
        tenant_id: "tenant-a".to_string(),
        namespace: "conversation".to_string(),
        nodes: vec![GraphNode {
            node_id: "node-1".to_string(),
            tenant_id: "tenant-a".to_string(),
            namespace: "conversation".to_string(),
            kind: "person".to_string(),
            label: "Morgan".to_string(),
            description: Some("source-backed entity".to_string()),
            metadata,
            source_refs: vec!["message-8".to_string()],
            created_at_ms: 1726000000000,
            updated_at_ms: 1726000000001,
        }],
    };
    let decoded = UpsertNodesRequest::decode(request.encode_to_vec().as_slice()).unwrap();
    assert_eq!(decoded, request);
    assert_eq!(decoded.nodes[0].source_refs, ["message-8"]);

    let response = ExportGraphResponse {
        result: Some(export_graph_response::Result::Batch(ExportGraphBatch {
            nodes: vec![],
            edges: vec![],
            next_page_token: "cursor-2".to_string(),
        })),
    };
    let decoded_response =
        ExportGraphResponse::decode(response.encode_to_vec().as_slice()).unwrap();
    assert_eq!(decoded_response, response);
    let request = ExportGraphRequest {
        tenant_id: "tenant-a".to_string(),
        namespace: "conversation".to_string(),
        page_size: Some(10),
        page_token: Some("cursor-1".to_string()),
    };
    assert_eq!(
        ExportGraphRequest::decode(request.encode_to_vec().as_slice()).unwrap(),
        request
    );
}
