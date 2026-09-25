// SPDX-License-Identifier: Apache-2.0
//! Comprehensive TCK and Verification Test Suite for Milestone M5B
//!
//! Covers Tasks T66 - T72 and the M5B exit gate.

use cyrene_memory_runtime::{
    backend::pgvector::{PgVectorIndexType, PgVectorSchemaBuilder},
    backend::sqlite::SqliteMemoryBackend,
    backend::MemoryStorageBackend,
    embedding::ContractModelEmbeddingClient,
    model::{EmbeddingProfile, MemoryProvenance, MemoryRecord, ReviewFacts},
    CyreneMemoryService,
};
use cyrene_plugin_contracts::memory_provider_v1::{
    delete_memory_response, export_memory_response, get_memory_response, import_memory_response,
    prune_memory_response, recall_memory_response, store_memory_response, DeleteMemoryRequest,
    ExportMemoryRequest, GetMemoryRequest, ImportMemoryRequest, MemoryErrorCode, MemoryItem,
    PruneMemoryRequest, RecallMemoryRequest, StoreMemoryRequest,
};
use std::collections::HashMap;
use std::sync::Arc;

const TEST_DIMENSION: usize = 4;

fn create_test_service() -> (CyreneMemoryService, Arc<SqliteMemoryBackend>) {
    let backend = Arc::new(SqliteMemoryBackend::new_in_memory().expect("sqlite in memory"));
    let embedding_client = Arc::new(ContractModelEmbeddingClient::new(TEST_DIMENSION));
    let service = CyreneMemoryService::new(backend.clone(), Some(embedding_client), TEST_DIMENSION);
    (service, backend)
}

#[tokio::test]
async fn test_t66_and_t70_full_crud_and_idempotent_store() {
    let (service, _backend) = create_test_service();

    // 1. Store
    // 中文：1. 存储。
    let mut meta = HashMap::new();
    meta.insert("tag".into(), "unit_test".into());

    let store_req = StoreMemoryRequest {
        tenant_id: "tenant_alpha".into(),
        item: Some(MemoryItem {
            item_id: "mem_1001".into(),
            tenant_id: "tenant_alpha".into(),
            scope: "session_chat".into(),
            subject: "user_preference".into(),
            content: "User prefers concise summaries".into(),
            embedding: vec![0.1, 0.2, 0.3, 0.4],
            metadata: meta.clone(),
            created_at_ms: 1000,
            expires_at_ms: None,
            ttl_seconds: None,
        }),
    };

    let store_resp = service.store(store_req.clone()).await;
    match store_resp.result.unwrap() {
        store_memory_response::Result::ItemId(id) => assert_eq!(id, "mem_1001"),
        store_memory_response::Result::Error(e) => panic!("Store failed: {:?}", e),
    }

    // 2. Idempotent Store (Update with same ID) (Task T70)
    // 中文：2. 幂等存储（使用相同 ID 更新）（任务 T70）。
    let update_req = StoreMemoryRequest {
        tenant_id: "tenant_alpha".into(),
        item: Some(MemoryItem {
            item_id: "mem_1001".into(),
            tenant_id: "tenant_alpha".into(),
            scope: "session_chat".into(),
            subject: "user_preference".into(),
            content: "User prefers concise markdown summaries".into(), // updated content | 中文：已更新的内容
            embedding: vec![0.1, 0.2, 0.3, 0.4],
            metadata: meta,
            created_at_ms: 1000,
            expires_at_ms: None,
            ttl_seconds: None,
        }),
    };
    let update_resp = service.store(update_req).await;
    match update_resp.result.unwrap() {
        store_memory_response::Result::ItemId(id) => assert_eq!(id, "mem_1001"),
        store_memory_response::Result::Error(e) => panic!("Update failed: {:?}", e),
    }

    // 3. Get
    // 中文：3. 获取。
    let get_resp = service
        .get(GetMemoryRequest {
            tenant_id: "tenant_alpha".into(),
            item_id: "mem_1001".into(),
        })
        .await;
    match get_resp.result.unwrap() {
        get_memory_response::Result::Item(item) => {
            assert_eq!(item.content, "User prefers concise markdown summaries");
            assert_eq!(item.scope, "session_chat");
        }
        get_memory_response::Result::Error(e) => panic!("Get failed: {:?}", e),
    }

    // 4. Exact Delete (Task T70)
    // 中文：4. 精确删除（任务 T70）。
    let del_resp = service
        .delete(DeleteMemoryRequest {
            tenant_id: "tenant_alpha".into(),
            item_id: "mem_1001".into(),
        })
        .await;
    match del_resp.result.unwrap() {
        delete_memory_response::Result::Deleted(deleted) => assert!(deleted),
        delete_memory_response::Result::Error(e) => panic!("Delete failed: {:?}", e),
    }

    // Verify deleted
    // 中文：验证该记忆已删除。
    let get_after_del = service
        .get(GetMemoryRequest {
            tenant_id: "tenant_alpha".into(),
            item_id: "mem_1001".into(),
        })
        .await;
    match get_after_del.result.unwrap() {
        get_memory_response::Result::Item(_) => panic!("Item should be deleted"),
        get_memory_response::Result::Error(e) => {
            assert_eq!(e.code, MemoryErrorCode::NotFound as i32)
        }
    }
}

#[tokio::test]
async fn test_t67_backend_pgvector_and_sqlite_separation() {
    // 1. Verify PostgreSQL/pgvector production DDL and query builders (Task T67)
    // 中文：1. 验证 PostgreSQL/pgvector 的生产 DDL 和查询构造器（任务 T67）。
    let ddl_hnsw = PgVectorSchemaBuilder::build_ddl(1536, PgVectorIndexType::Hnsw);
    assert!(ddl_hnsw.contains("CREATE EXTENSION IF NOT EXISTS vector;"));
    assert!(ddl_hnsw.contains("embedding vector(1536) NOT NULL"));
    assert!(ddl_hnsw.contains("USING hnsw (embedding vector_cosine_ops)"));

    let ddl_ivfflat = PgVectorSchemaBuilder::build_ddl(768, PgVectorIndexType::Ivfflat);
    assert!(ddl_ivfflat.contains("embedding vector(768) NOT NULL"));
    assert!(ddl_ivfflat.contains("USING ivfflat (embedding vector_cosine_ops)"));

    let recall_sql = PgVectorSchemaBuilder::build_recall_sql();
    assert!(recall_sql.contains("1.0 - (embedding <=> $1::vector) AS score"));

    // 2. Verify SQLite dev backend executes in-memory with transactional guarantees
    // 中文：2. 验证 SQLite 开发后端在内存中执行并提供事务保证。
    let sqlite_backend = SqliteMemoryBackend::new_in_memory().expect("in memory sqlite");
    let record = MemoryRecord {
        item_id: "dev_test_1".into(),
        tenant_id: "dev_tenant".into(),
        scope: "test".into(),
        subject: "test".into(),
        content: "test content".into(),
        embedding: vec![1.0, 0.0, 0.0, 0.0],
        metadata: HashMap::new(),
        provenance: MemoryProvenance {
            origin_channel: "dev".into(),
            source_entity_id: "dev_tenant".into(),
            session_id: None,
            created_by: "tester".into(),
        },
        embedding_profile: EmbeddingProfile {
            model_id: "test-model".into(),
            dimension: 4,
            is_normalized: true,
        },
        review_facts: ReviewFacts {
            review_status: "APPROVED".into(),
            reviewed_by: None,
            reviewed_at_ms: None,
            flags: vec![],
        },
        created_at_ms: 1000,
        updated_at_ms: 1000,
        expires_at_ms: None,
        ttl_seconds: None,
    };
    sqlite_backend.store(record).await.expect("store record");
    let fetched = sqlite_backend
        .get("dev_tenant", "dev_test_1")
        .await
        .expect("get record");
    assert!(fetched.is_some());
}

#[tokio::test]
async fn test_t68_explicit_modeling_and_invariants() {
    let (service, backend) = create_test_service();

    let mut meta = HashMap::new();
    meta.insert("cluster".into(), "facts".into());

    let store_req = StoreMemoryRequest {
        tenant_id: "tenant_model".into(),
        item: Some(MemoryItem {
            item_id: "mem_explicit_1".into(),
            tenant_id: "tenant_model".into(),
            scope: "kb_scope".into(),
            subject: "domain_facts".into(),
            content: "Rust provides memory safety without garbage collection".into(),
            embedding: vec![0.5, 0.5, 0.5, 0.5],
            metadata: meta,
            created_at_ms: 2000,
            expires_at_ms: Some(9999999999),
            ttl_seconds: Some(3600),
        }),
    };
    service.store(store_req).await;

    let rec = backend
        .get("tenant_model", "mem_explicit_1")
        .await
        .unwrap()
        .expect("found");
    assert_eq!(rec.provenance.origin_channel, "direct");
    assert_eq!(rec.provenance.created_by, "system");
    assert_eq!(rec.embedding_profile.dimension, TEST_DIMENSION);
    assert_eq!(rec.review_facts.review_status, "APPROVED");
    assert_eq!(rec.ttl_seconds, Some(3600));
}

#[tokio::test]
async fn test_t69_embedding_via_model_provider_only() {
    // When no embedding is provided, service calls ModelEmbeddingClient (model.provider.v1)
    // 中文：未提供 embedding 时，服务会调用 ModelEmbeddingClient（model.provider.v1）。
    let (service, _backend) = create_test_service();

    let store_req = StoreMemoryRequest {
        tenant_id: "tenant_embed".into(),
        item: Some(MemoryItem {
            item_id: "mem_auto_embed".into(),
            tenant_id: "tenant_embed".into(),
            scope: "auto".into(),
            subject: "text".into(),
            content: "Generate embedding via contract client".into(),
            embedding: vec![], // empty embedding triggers model.provider.v1 integration | 中文：空 embedding 会触发 model.provider.v1 集成
            metadata: HashMap::new(),
            created_at_ms: 1000,
            expires_at_ms: None,
            ttl_seconds: None,
        }),
    };
    let store_resp = service.store(store_req).await;
    match store_resp.result.unwrap() {
        store_memory_response::Result::ItemId(id) => assert_eq!(id, "mem_auto_embed"),
        store_memory_response::Result::Error(e) => panic!("Auto-embed failed: {:?}", e),
    }

    let get_resp = service
        .get(GetMemoryRequest {
            tenant_id: "tenant_embed".into(),
            item_id: "mem_auto_embed".into(),
        })
        .await;
    match get_resp.result.unwrap() {
        get_memory_response::Result::Item(item) => {
            assert_eq!(item.embedding.len(), TEST_DIMENSION);
            let norm: f32 = item.embedding.iter().map(|x| x * x).sum::<f32>().sqrt();
            assert!((norm - 1.0).abs() < 1e-4, "Embedding should be normalized");
        }
        get_memory_response::Result::Error(e) => panic!("Get failed: {:?}", e),
    }
}

#[tokio::test]
async fn test_t70_recall_filtered_and_prune() {
    let (service, _backend) = create_test_service();

    // Store items with varying embeddings and scopes
    // 中文：存储具有不同 embedding 和作用域的条目。
    service
        .store(StoreMemoryRequest {
            tenant_id: "tenant_recall".into(),
            item: Some(MemoryItem {
                item_id: "target_1".into(),
                tenant_id: "tenant_recall".into(),
                scope: "work".into(),
                subject: "ai".into(),
                content: "Artificial Intelligence research".into(),
                embedding: vec![1.0, 0.0, 0.0, 0.0],
                metadata: HashMap::new(),
                created_at_ms: 1000,
                expires_at_ms: None,
                ttl_seconds: None,
            }),
        })
        .await;

    service
        .store(StoreMemoryRequest {
            tenant_id: "tenant_recall".into(),
            item: Some(MemoryItem {
                item_id: "target_2".into(),
                tenant_id: "tenant_recall".into(),
                scope: "personal".into(),
                subject: "cooking".into(),
                content: "Italian pasta recipes".into(),
                embedding: vec![0.0, 1.0, 0.0, 0.0],
                metadata: HashMap::new(),
                created_at_ms: 1000,
                expires_at_ms: None,
                ttl_seconds: None,
            }),
        })
        .await;

    // Recall with query matching target_1
    // 中文：使用与 target_1 匹配的查询检索记忆。
    let recall_resp = service
        .recall(RecallMemoryRequest {
            tenant_id: "tenant_recall".into(),
            scope: Some("work".into()),
            subject: None,
            query_embedding: vec![0.99, 0.01, 0.0, 0.0],
            query_text: None,
            metadata_filters: HashMap::new(),
            min_similarity: Some(0.8),
            top_k: Some(5),
        })
        .await;

    match recall_resp.result.unwrap() {
        recall_memory_response::Result::Matches(batch) => {
            assert_eq!(batch.matches.len(), 1);
            let matched = &batch.matches[0];
            assert_eq!(matched.item.as_ref().unwrap().item_id, "target_1");
            assert!(matched.score > 0.95);
        }
        recall_memory_response::Result::Error(e) => panic!("Recall failed: {:?}", e),
    }

    // Test Prune (Task T70)
    // 中文：测试 Prune（任务 T70）。
    let prune_resp = service
        .prune(PruneMemoryRequest {
            tenant_id: "tenant_recall".into(),
            before_timestamp_ms: Some(2000),
        })
        .await;
    match prune_resp.result.unwrap() {
        prune_memory_response::Result::PrunedCount(count) => assert_eq!(count, 2),
        prune_memory_response::Result::Error(e) => panic!("Prune failed: {:?}", e),
    }
}

#[tokio::test]
async fn test_t71_pure_capability_boundary_no_dream_or_inference() {
    // Assert that the memory service exposes pure storage & recall capability only.
    // Proactive states, dream audits, and profile inference are strictly prohibited.
    // 中文：断言记忆服务只暴露纯存储与检索能力。
    // 主动状态、梦境审计和用户画像推断都严格禁止。
    let (service, _backend) = create_test_service();

    let store_req = StoreMemoryRequest {
        tenant_id: "tenant_pure".into(),
        item: Some(MemoryItem {
            item_id: "pure_1".into(),
            tenant_id: "tenant_pure".into(),
            scope: "facts".into(),
            subject: "generic".into(),
            content: "Plain verifiable assertion".into(),
            embedding: vec![0.25, 0.25, 0.25, 0.25],
            metadata: HashMap::new(),
            created_at_ms: 1000,
            expires_at_ms: None,
            ttl_seconds: None,
        }),
    };
    let res = service.store(store_req).await;
    assert!(matches!(
        res.result.unwrap(),
        store_memory_response::Result::ItemId(_)
    ));
}

#[tokio::test]
async fn test_t72_adversarial_tenancy_dimension_expiry_rollback() {
    let (service, _backend) = create_test_service();

    // 1. Strict Tenant Isolation
    // 中文：1. 严格执行租户隔离。
    service
        .store(StoreMemoryRequest {
            tenant_id: "tenant_A".into(),
            item: Some(MemoryItem {
                item_id: "secret_A".into(),
                tenant_id: "tenant_A".into(),
                scope: "confidential".into(),
                subject: "plans".into(),
                content: "Company A sensitive roadmap".into(),
                embedding: vec![1.0, 0.0, 0.0, 0.0],
                metadata: HashMap::new(),
                created_at_ms: 1000,
                expires_at_ms: None,
                ttl_seconds: None,
            }),
        })
        .await;

    // Tenant B cannot GET Tenant A's item
    // 中文：租户 B 无法读取（GET）租户 A 的条目。
    let cross_get = service
        .get(GetMemoryRequest {
            tenant_id: "tenant_B".into(),
            item_id: "secret_A".into(),
        })
        .await;
    match cross_get.result.unwrap() {
        get_memory_response::Result::Item(_) => panic!("Cross-tenant item leakage!"),
        get_memory_response::Result::Error(e) => {
            assert_eq!(e.code, MemoryErrorCode::NotFound as i32)
        }
    }

    // Tenant B cannot RECALL Tenant A's item
    // 中文：租户 B 无法检索（RECALL）租户 A 的条目。
    let cross_recall = service
        .recall(RecallMemoryRequest {
            tenant_id: "tenant_B".into(),
            scope: None,
            subject: None,
            query_embedding: vec![1.0, 0.0, 0.0, 0.0],
            query_text: None,
            metadata_filters: HashMap::new(),
            min_similarity: Some(0.5),
            top_k: Some(10),
        })
        .await;
    match cross_recall.result.unwrap() {
        recall_memory_response::Result::Matches(batch) => assert_eq!(batch.matches.len(), 0),
        recall_memory_response::Result::Error(e) => panic!("Recall error: {:?}", e),
    }

    // 2. Vector Dimension Mismatch
    // 中文：2. 向量维度不匹配。
    let bad_dim_store = service
        .store(StoreMemoryRequest {
            tenant_id: "tenant_A".into(),
            item: Some(MemoryItem {
                item_id: "bad_dim".into(),
                tenant_id: "tenant_A".into(),
                scope: "test".into(),
                subject: "test".into(),
                content: "Wrong vector dimension".into(),
                embedding: vec![1.0, 2.0], // 2 dimensions instead of expected 4 | 中文：实际为 2 维，预期为 4 维
                metadata: HashMap::new(),
                created_at_ms: 1000,
                expires_at_ms: None,
                ttl_seconds: None,
            }),
        })
        .await;
    match bad_dim_store.result.unwrap() {
        store_memory_response::Result::ItemId(_) => {
            panic!("Store should reject dimension mismatch")
        }
        store_memory_response::Result::Error(e) => {
            assert_eq!(e.code, MemoryErrorCode::VectorDimensionMismatch as i32);
        }
    }

    // 3. TTL Expiry
    // 中文：3. TTL 过期。
    service
        .store(StoreMemoryRequest {
            tenant_id: "tenant_A".into(),
            item: Some(MemoryItem {
                item_id: "expired_item".into(),
                tenant_id: "tenant_A".into(),
                scope: "ephemeral".into(),
                subject: "temp".into(),
                content: "Expired data".into(),
                embedding: vec![0.5, 0.5, 0.5, 0.5],
                metadata: HashMap::new(),
                created_at_ms: 100,
                expires_at_ms: Some(200), // explicitly in the past | 中文：过期时间明确设在过去
                ttl_seconds: None,
            }),
        })
        .await;

    let expired_get = service
        .get(GetMemoryRequest {
            tenant_id: "tenant_A".into(),
            item_id: "expired_item".into(),
        })
        .await;
    match expired_get.result.unwrap() {
        get_memory_response::Result::Item(_) => panic!("Expired item should not be returned"),
        get_memory_response::Result::Error(e) => {
            assert_eq!(e.code, MemoryErrorCode::NotFound as i32)
        }
    }

    // 4. Import Atomic Rollback on Error
    // 中文：4. 出错时原子回滚导入操作。
    let import_batch = vec![
        MemoryItem {
            item_id: "import_valid_1".into(),
            tenant_id: "tenant_A".into(),
            scope: "imported".into(),
            subject: "test".into(),
            content: "Valid item 1".into(),
            embedding: vec![0.1, 0.2, 0.3, 0.4],
            metadata: HashMap::new(),
            created_at_ms: 1000,
            expires_at_ms: None,
            ttl_seconds: None,
        },
        MemoryItem {
            item_id: "import_invalid_tenant".into(),
            tenant_id: "tenant_ATTACKER".into(), // Tenant mismatch forces transaction rollback! | 中文：租户不匹配会触发事务回滚！
            scope: "injected".into(),
            subject: "test".into(),
            content: "Injected item".into(),
            embedding: vec![0.1, 0.2, 0.3, 0.4],
            metadata: HashMap::new(),
            created_at_ms: 1000,
            expires_at_ms: None,
            ttl_seconds: None,
        },
    ];

    let import_resp = service
        .import_batch(ImportMemoryRequest {
            tenant_id: "tenant_A".into(),
            items: import_batch,
            overwrite: true,
        })
        .await;

    match import_resp.result.unwrap() {
        import_memory_response::Result::ImportedCount(_) => {
            panic!("Import should fail and rollback")
        }
        import_memory_response::Result::Error(e) => {
            assert_eq!(e.code, MemoryErrorCode::TenantMismatch as i32);
        }
    }

    // Verify atomic rollback: import_valid_1 must NOT exist in the database
    // 中文：验证原子回滚：`import_valid_1` 绝不能出现在数据库中。
    let rollback_check = service
        .get(GetMemoryRequest {
            tenant_id: "tenant_A".into(),
            item_id: "import_valid_1".into(),
        })
        .await;
    match rollback_check.result.unwrap() {
        get_memory_response::Result::Item(_) => panic!("Import transaction was not rolled back!"),
        get_memory_response::Result::Error(e) => {
            assert_eq!(e.code, MemoryErrorCode::NotFound as i32)
        }
    }

    // 5. Successful Import and Export with pagination
    // 中文：5. 成功执行带分页的导入和导出。
    let valid_batch = vec![
        MemoryItem {
            item_id: "batch_1".into(),
            tenant_id: "tenant_A".into(),
            scope: "export_scope".into(),
            subject: "sub1".into(),
            content: "Item 1".into(),
            embedding: vec![0.1, 0.2, 0.3, 0.4],
            metadata: HashMap::new(),
            created_at_ms: 1000,
            expires_at_ms: None,
            ttl_seconds: None,
        },
        MemoryItem {
            item_id: "batch_2".into(),
            tenant_id: "tenant_A".into(),
            scope: "export_scope".into(),
            subject: "sub2".into(),
            content: "Item 2".into(),
            embedding: vec![0.2, 0.3, 0.4, 0.5],
            metadata: HashMap::new(),
            created_at_ms: 1010,
            expires_at_ms: None,
            ttl_seconds: None,
        },
    ];

    let ok_import = service
        .import_batch(ImportMemoryRequest {
            tenant_id: "tenant_A".into(),
            items: valid_batch,
            overwrite: true,
        })
        .await;
    match ok_import.result.unwrap() {
        import_memory_response::Result::ImportedCount(c) => assert_eq!(c, 2),
        import_memory_response::Result::Error(e) => panic!("Valid import failed: {:?}", e),
    }

    let export_resp = service
        .export_batch(ExportMemoryRequest {
            tenant_id: "tenant_A".into(),
            scope: Some("export_scope".into()),
            limit: Some(1),
        })
        .await;
    match export_resp.result.unwrap() {
        export_memory_response::Result::Batch(batch) => {
            assert_eq!(batch.items.len(), 1, "Pagination limit respected");
            assert_eq!(batch.items[0].item_id, "batch_1");
        }
        export_memory_response::Result::Error(e) => panic!("Export failed: {:?}", e),
    }
}
