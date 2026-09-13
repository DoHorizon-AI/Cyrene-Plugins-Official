// SPDX-License-Identifier: Apache-2.0
//! Comprehensive Integration and Fail-Closed TCK Test Suite for cyrene-plugin-server (R08, R09)

use std::collections::HashMap;
use std::net::SocketAddr;
use std::time::Duration;
use tokio::net::TcpListener;
use tokio_stream::wrappers::TcpListenerStream;
use tonic::transport::{Channel, Server};

use prost::Message;

// Proto generated client & types
pub mod proto {
    #![allow(clippy::result_large_err)]
    tonic::include_proto!("cyrene.plugin.runtime.v1");
}

use proto::direct_invocation_error::Code;
use proto::direct_invocation_response::Result as InvocationResult;
use proto::direct_plugin_runtime_client::DirectPluginRuntimeClient;
use proto::direct_plugin_runtime_server::DirectPluginRuntimeServer;
use proto::{health_response, DirectInvocationRequest, DirectStreamMode, HealthRequest};

// Contract types
use cyrene_plugin_contracts::agent_runtime_v1::{
    agent_run_response, AgentRunRequest, AgentRunResponse,
};
use cyrene_plugin_contracts::computer_runtime_v1::{
    create_artifact_response, get_artifact_response, CreateArtifactRequest, CreateArtifactResponse,
    GetArtifactRequest, GetArtifactResponse,
};
use cyrene_plugin_contracts::memory_provider_v1::{
    delete_memory_response, get_memory_response, recall_memory_response, store_memory_response,
    DeleteMemoryRequest, DeleteMemoryResponse, GetMemoryRequest, GetMemoryResponse, MemoryItem,
    RecallMemoryRequest, RecallMemoryResponse, StoreMemoryRequest, StoreMemoryResponse,
};

// Service implementation
#[path = "../src/service.rs"]
mod service;

use service::DirectPluginRuntimeServiceImpl;

/// Helper to spin up an in-process server instance listening on an ephemeral port.
async fn start_test_server() -> (DirectPluginRuntimeClient<Channel>, SocketAddr) {
    start_server(DirectPluginRuntimeServiceImpl::with_simulated_dependencies_for_tests()).await
}

/// Starts the same service configuration used by the production binary.
async fn start_production_server() -> (DirectPluginRuntimeClient<Channel>, SocketAddr) {
    start_server(DirectPluginRuntimeServiceImpl::new()).await
}

async fn start_server(
    service: DirectPluginRuntimeServiceImpl,
) -> (DirectPluginRuntimeClient<Channel>, SocketAddr) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let incoming = TcpListenerStream::new(listener);

    tokio::spawn(async move {
        Server::builder()
            .add_service(DirectPluginRuntimeServer::new(service))
            .serve_with_incoming(incoming)
            .await
            .unwrap();
    });

    // Give server a moment to start
    tokio::time::sleep(Duration::from_millis(50)).await;

    let endpoint = format!("http://{}", addr);
    let client = DirectPluginRuntimeClient::connect(endpoint).await.unwrap();

    (client, addr)
}

#[tokio::test]
async fn test_production_host_does_not_advertise_simulated_capabilities() {
    let (mut client, _addr) = start_production_server().await;

    let health = client.health(HealthRequest {}).await.unwrap().into_inner();
    assert_eq!(health.capabilities, vec!["computer.runtime.v1"]);

    let request = AgentRunRequest::default();
    let response = client
        .invoke(DirectInvocationRequest {
            capability: "agent.runtime.v1".into(),
            method: "Run".into(),
            interface_version: "1".into(),
            payload_type_url: "type.cyrene.io/cyrene.agent.runtime.v1.AgentRunRequest".into(),
            payload: request.encode_to_vec(),
            request_id: "production-fail-closed".into(),
            stream_mode: DirectStreamMode::Invocation as i32,
        })
        .await
        .unwrap()
        .into_inner();

    let Some(InvocationResult::Error(error)) = response.result else {
        panic!("production agent invocation must fail closed");
    };
    assert_eq!(error.code, Code::Unavailable as i32);
    assert_eq!(error.domain_code, "DEPENDENCY_BINDINGS_UNAVAILABLE");
}

#[tokio::test]
async fn test_r08_health_check() {
    let (mut client, _addr) = start_test_server().await;

    let resp = client.health(HealthRequest {}).await.unwrap().into_inner();

    assert_eq!(resp.status, health_response::Status::Serving as i32);
    assert_eq!(resp.plugin_id, "cyrene.plugin.rust.server");
    assert_eq!(resp.plugin_version, "0.1.0");
    assert_eq!(
        resp.capabilities,
        vec![
            "agent.runtime.v1".to_string(),
            "memory.provider.v1".to_string(),
            "computer.runtime.v1".to_string(),
        ]
    );
}

#[tokio::test]
async fn test_r08_memory_crud_roundtrip() {
    let (mut client, _addr) = start_test_server().await;

    // 1. Store memory
    let item_to_store = MemoryItem {
        item_id: "item-r08-1".into(),
        tenant_id: "tenant-r08".into(),
        scope: "default".into(),
        subject: "test".into(),
        content: "Cyrene standalone native server operates smoothly".into(),
        embedding: vec![0.1, 0.2, 0.3, 0.4],
        metadata: HashMap::new(),
        created_at_ms: 1000,
        expires_at_ms: None,
        ttl_seconds: None,
    };
    let store_req = StoreMemoryRequest {
        tenant_id: "tenant-r08".into(),
        item: Some(item_to_store),
    };
    let mut store_buf = Vec::new();
    store_req.encode(&mut store_buf).unwrap();

    let inv_resp = client
        .invoke(DirectInvocationRequest {
            interface_version: "1".into(),
            capability: "memory.provider.v1".into(),
            method: "StoreMemory".into(),
            payload: store_buf,
            payload_type_url: "type.cyrene.io/cyrene.memory.provider.v1.StoreMemoryRequest".into(),
            request_id: "req-01".into(),
            stream_mode: DirectStreamMode::Unspecified as i32,
        })
        .await
        .unwrap()
        .into_inner();

    let store_result = match inv_resp.result.unwrap() {
        InvocationResult::Payload(p) => StoreMemoryResponse::decode(&p.value[..]).unwrap(),
        InvocationResult::Error(e) => panic!("StoreMemory failed: {:?}", e),
    };

    let stored_id = match store_result.result.unwrap() {
        store_memory_response::Result::ItemId(id) => id,
        store_memory_response::Result::Error(e) => panic!("Store error: {:?}", e),
    };
    assert_eq!(stored_id, "item-r08-1");

    // 2. Get memory
    let get_req = GetMemoryRequest {
        tenant_id: "tenant-r08".into(),
        item_id: stored_id.clone(),
    };
    let mut get_buf = Vec::new();
    get_req.encode(&mut get_buf).unwrap();

    let inv_resp = client
        .invoke(DirectInvocationRequest {
            interface_version: "1".into(),
            capability: "memory.provider.v1".into(),
            method: "GetMemory".into(),
            payload: get_buf,
            payload_type_url: "type.cyrene.io/cyrene.memory.provider.v1.GetMemoryRequest".into(),
            request_id: "req-02".into(),
            stream_mode: DirectStreamMode::Unspecified as i32,
        })
        .await
        .unwrap()
        .into_inner();

    let get_result = match inv_resp.result.unwrap() {
        InvocationResult::Payload(p) => GetMemoryResponse::decode(&p.value[..]).unwrap(),
        InvocationResult::Error(e) => panic!("GetMemory failed: {:?}", e),
    };

    let fetched = match get_result.result.unwrap() {
        get_memory_response::Result::Item(it) => it,
        get_memory_response::Result::Error(e) => panic!("Get error: {:?}", e),
    };
    assert_eq!(fetched.item_id, stored_id);
    assert_eq!(
        fetched.content,
        "Cyrene standalone native server operates smoothly"
    );

    // 3. Recall memory
    let recall_req = RecallMemoryRequest {
        tenant_id: "tenant-r08".into(),
        scope: None,
        subject: None,
        query_embedding: vec![0.1, 0.2, 0.3, 0.4],
        query_text: Some("Cyrene native".into()),
        metadata_filters: HashMap::new(),
        min_similarity: Some(0.0),
        top_k: Some(5),
    };
    let mut recall_buf = Vec::new();
    recall_req.encode(&mut recall_buf).unwrap();

    let inv_resp = client
        .invoke(DirectInvocationRequest {
            interface_version: "1".into(),
            capability: "memory.provider.v1".into(),
            method: "RecallMemory".into(),
            payload: recall_buf,
            payload_type_url: "type.cyrene.io/cyrene.memory.provider.v1.RecallMemoryRequest".into(),
            request_id: "req-03".into(),
            stream_mode: DirectStreamMode::Unspecified as i32,
        })
        .await
        .unwrap()
        .into_inner();

    let recall_result = match inv_resp.result.unwrap() {
        InvocationResult::Payload(p) => RecallMemoryResponse::decode(&p.value[..]).unwrap(),
        InvocationResult::Error(e) => panic!("RecallMemory failed: {:?}", e),
    };

    let matches = match recall_result.result.unwrap() {
        recall_memory_response::Result::Matches(m) => m,
        recall_memory_response::Result::Error(e) => panic!("Recall error: {:?}", e),
    };
    assert!(!matches.matches.is_empty());

    // 4. Delete memory
    let del_req = DeleteMemoryRequest {
        tenant_id: "tenant-r08".into(),
        item_id: stored_id.clone(),
    };
    let mut del_buf = Vec::new();
    del_req.encode(&mut del_buf).unwrap();

    let inv_resp = client
        .invoke(DirectInvocationRequest {
            interface_version: "1".into(),
            capability: "memory.provider.v1".into(),
            method: "DeleteMemory".into(),
            payload: del_buf,
            payload_type_url: "type.cyrene.io/cyrene.memory.provider.v1.DeleteMemoryRequest".into(),
            request_id: "req-04".into(),
            stream_mode: DirectStreamMode::Unspecified as i32,
        })
        .await
        .unwrap()
        .into_inner();

    let del_result = match inv_resp.result.unwrap() {
        InvocationResult::Payload(p) => DeleteMemoryResponse::decode(&p.value[..]).unwrap(),
        InvocationResult::Error(e) => panic!("DeleteMemory failed: {:?}", e),
    };

    match del_result.result.unwrap() {
        delete_memory_response::Result::Deleted(d) => assert!(d),
        delete_memory_response::Result::Error(e) => panic!("Delete error: {:?}", e),
    }
}

#[tokio::test]
async fn test_r08_agent_run_roundtrip() {
    let (mut client, _addr) = start_test_server().await;

    let run_req = AgentRunRequest {
        run_id: "run-test-01".into(),
        session_id: "sess-01".into(),
        prompt: "Hello Cyrene Agent Runtime".into(),
        messages: vec![],
        available_tools: vec![],
        config: None,
    };
    let mut run_buf = Vec::new();
    run_req.encode(&mut run_buf).unwrap();

    let inv_resp = client
        .invoke(DirectInvocationRequest {
            interface_version: "1".into(),
            capability: "agent.runtime.v1".into(),
            method: "Run".into(),
            payload: run_buf,
            payload_type_url: "type.cyrene.io/cyrene.agent.runtime.v1.AgentRunRequest".into(),
            request_id: "req-run-01".into(),
            stream_mode: DirectStreamMode::Unspecified as i32,
        })
        .await
        .unwrap()
        .into_inner();

    let run_result = match inv_resp.result.unwrap() {
        InvocationResult::Payload(p) => AgentRunResponse::decode(&p.value[..]).unwrap(),
        InvocationResult::Error(e) => panic!("Agent Run failed: {:?}", e),
    };

    match run_result.result.unwrap() {
        agent_run_response::Result::Success(succ) => {
            assert_eq!(succ.run_id, "run-test-01");
            assert!(!succ.output_text.is_empty());
        }
        agent_run_response::Result::Error(e) => panic!("Agent returned error: {:?}", e),
    }
}

#[tokio::test]
async fn test_r08_computer_artifact_lifecycle() {
    let (mut client, _addr) = start_test_server().await;

    // 1. Create Artifact
    let artifact_data = b"Hello from Cyrene Server Artifact".to_vec();
    let create_art_req = CreateArtifactRequest {
        name: "test.txt".into(),
        mime_type: "text/plain".into(),
        data: artifact_data.clone(),
    };
    let mut art_buf = Vec::new();
    create_art_req.encode(&mut art_buf).unwrap();

    let inv_resp = client
        .invoke(DirectInvocationRequest {
            interface_version: "1".into(),
            capability: "computer.runtime.v1".into(),
            method: "CreateArtifact".into(),
            payload: art_buf,
            payload_type_url: "type.cyrene.io/cyrene.computer.runtime.v1.CreateArtifactRequest"
                .into(),
            request_id: "req-art-01".into(),
            stream_mode: DirectStreamMode::Unspecified as i32,
        })
        .await
        .unwrap()
        .into_inner();

    let art_resp = match inv_resp.result.unwrap() {
        InvocationResult::Payload(p) => CreateArtifactResponse::decode(&p.value[..]).unwrap(),
        InvocationResult::Error(e) => panic!("CreateArtifact failed: {:?}", e),
    };

    let meta = match art_resp.result.unwrap() {
        create_artifact_response::Result::Artifact(a) => a,
        create_artifact_response::Result::Error(e) => panic!("CreateArtifact error: {:?}", e),
    };
    let artifact_id = meta.artifact_id.clone();
    assert_eq!(meta.name, "test.txt");
    assert_eq!(meta.size_bytes, artifact_data.len() as i64);

    // 2. Get Artifact
    let get_art_req = GetArtifactRequest {
        artifact_id: artifact_id.clone(),
    };
    let mut get_art_buf = Vec::new();
    get_art_req.encode(&mut get_art_buf).unwrap();

    let inv_resp = client
        .invoke(DirectInvocationRequest {
            interface_version: "1".into(),
            capability: "computer.runtime.v1".into(),
            method: "GetArtifact".into(),
            payload: get_art_buf,
            payload_type_url: "type.cyrene.io/cyrene.computer.runtime.v1.GetArtifactRequest".into(),
            request_id: "req-art-02".into(),
            stream_mode: DirectStreamMode::Unspecified as i32,
        })
        .await
        .unwrap()
        .into_inner();

    let get_art_resp = match inv_resp.result.unwrap() {
        InvocationResult::Payload(p) => GetArtifactResponse::decode(&p.value[..]).unwrap(),
        InvocationResult::Error(e) => panic!("GetArtifact failed: {:?}", e),
    };

    let payload = match get_art_resp.result.unwrap() {
        get_artifact_response::Result::Payload(p) => p,
        get_artifact_response::Result::Error(e) => panic!("GetArtifact error: {:?}", e),
    };
    assert_eq!(payload.data, artifact_data);
    assert_eq!(payload.metadata.unwrap().artifact_id, artifact_id);
}

#[tokio::test]
async fn test_r09_fail_closed_guarantees() {
    let (mut client, _addr) = start_test_server().await;

    // Case 1: Unsupported interface version
    let inv_resp = client
        .invoke(DirectInvocationRequest {
            interface_version: "999.0".into(),
            capability: "memory.provider.v1".into(),
            method: "GetMemory".into(),
            payload: vec![],
            payload_type_url: "type.cyrene.io/cyrene.memory.provider.v1.GetMemoryRequest".into(),
            request_id: "req-fc-01".into(),
            stream_mode: DirectStreamMode::Unspecified as i32,
        })
        .await
        .unwrap()
        .into_inner();

    match inv_resp.result.unwrap() {
        InvocationResult::Error(e) => {
            assert_eq!(e.code, Code::InvalidRequest as i32);
            assert_eq!(e.domain_code, "UNSUPPORTED_INTERFACE_VERSION");
            assert!(!e.retryable);
        }
        InvocationResult::Payload(_) => {
            panic!("Expected fail-closed error for interface version mismatch")
        }
    }

    // Case 2: Unknown capability
    let inv_resp = client
        .invoke(DirectInvocationRequest {
            interface_version: "1".into(),
            capability: "unknown.capability.v1".into(),
            method: "DoSomething".into(),
            payload: vec![],
            payload_type_url: "type.cyrene.io/unknown.Request".into(),
            request_id: "req-fc-02".into(),
            stream_mode: DirectStreamMode::Unspecified as i32,
        })
        .await
        .unwrap()
        .into_inner();

    match inv_resp.result.unwrap() {
        InvocationResult::Error(e) => {
            assert_eq!(e.code, Code::MethodNotFound as i32);
            assert_eq!(e.domain_code, "CAPABILITY_NOT_FOUND");
            assert!(!e.retryable);
        }
        InvocationResult::Payload(_) => panic!("Expected fail-closed error for unknown capability"),
    }

    // Case 3: Unknown method within known capability
    let inv_resp = client
        .invoke(DirectInvocationRequest {
            interface_version: "1".into(),
            capability: "memory.provider.v1".into(),
            method: "FormatHardDrive".into(),
            payload: vec![],
            payload_type_url: "type.cyrene.io/cyrene.memory.provider.v1.FormatRequest".into(),
            request_id: "req-fc-03".into(),
            stream_mode: DirectStreamMode::Unspecified as i32,
        })
        .await
        .unwrap()
        .into_inner();

    match inv_resp.result.unwrap() {
        InvocationResult::Error(e) => {
            assert_eq!(e.code, Code::MethodNotFound as i32);
            assert_eq!(e.domain_code, "METHOD_NOT_FOUND");
            assert!(!e.retryable);
        }
        InvocationResult::Payload(_) => panic!("Expected fail-closed error for unknown method"),
    }

    // Case 4: Invalid / Malicious Type URL
    let inv_resp = client
        .invoke(DirectInvocationRequest {
            interface_version: "1".into(),
            capability: "memory.provider.v1".into(),
            method: "GetMemory".into(),
            payload: vec![1, 2, 3],
            payload_type_url: "type.googleapis.com/malicious.Payload".into(),
            request_id: "req-fc-04".into(),
            stream_mode: DirectStreamMode::Unspecified as i32,
        })
        .await
        .unwrap()
        .into_inner();

    match inv_resp.result.unwrap() {
        InvocationResult::Error(e) => {
            assert_eq!(e.code, Code::InvalidRequest as i32);
            assert_eq!(e.domain_code, "INVALID_TYPE_URL");
            assert!(!e.retryable);
        }
        InvocationResult::Payload(_) => panic!("Expected fail-closed error for invalid Type URL"),
    }

    // Case 5: Corrupted Protobuf payload
    let inv_resp = client
        .invoke(DirectInvocationRequest {
            interface_version: "1".into(),
            capability: "memory.provider.v1".into(),
            method: "GetMemory".into(),
            payload: vec![0xFF, 0xFF, 0xFF, 0xFF], // Illegal protobuf varint bytes
            payload_type_url: "type.cyrene.io/cyrene.memory.provider.v1.GetMemoryRequest".into(),
            request_id: "req-fc-05".into(),
            stream_mode: DirectStreamMode::Unspecified as i32,
        })
        .await
        .unwrap()
        .into_inner();

    match inv_resp.result.unwrap() {
        InvocationResult::Error(e) => {
            assert_eq!(e.code, Code::InvalidRequest as i32);
            assert_eq!(e.domain_code, "DECODE_ERROR");
            assert!(!e.retryable);
        }
        InvocationResult::Payload(_) => panic!("Expected fail-closed error for corrupted payload"),
    }
}
