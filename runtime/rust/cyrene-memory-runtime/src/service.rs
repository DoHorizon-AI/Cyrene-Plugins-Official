// SPDX-License-Identifier: Apache-2.0
//! Memory Provider Service Implementation (Tasks T66, T68, T69, T70, T71, T72)
//!
//! Exposes the canonical memory.provider.v1 methods:
//! store, get, recall, delete, prune, export_batch, import_batch.
//! Strictly pure capability: no profile inference, no dream audits, no proactive state.
//! Memory Provider Service 实现（任务 T66、T68、T69、T70、T71、T72）。
//! 暴露规范 memory.provider.v1 method：store、get、recall、delete、prune、export_batch、import_batch。
//! 这是纯 capability：不执行 profile 推断、dream audit 或主动状态管理。

use crate::backend::MemoryStorageBackend;
use crate::embedding::ModelEmbeddingClient;
use crate::model::{EmbeddingProfile, MemoryProvenance, MemoryRecord, ReviewFacts};
use cyrene_plugin_contracts::memory_provider_v1::{
    delete_memory_response, export_memory_response, get_memory_response, import_memory_response,
    prune_memory_response, recall_memory_response, store_memory_response, DeleteMemoryRequest,
    DeleteMemoryResponse, ExportBatch, ExportMemoryRequest, ExportMemoryResponse, GetMemoryRequest,
    GetMemoryResponse, ImportMemoryRequest, ImportMemoryResponse, MemoryError, MemoryErrorCode,
    MemoryItem, PruneMemoryRequest, PruneMemoryResponse, RecallBatch, RecallMemoryRequest,
    RecallMemoryResponse, StoreMemoryRequest, StoreMemoryResponse,
};
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

pub struct CyreneMemoryService {
    backend: Arc<dyn MemoryStorageBackend>,
    embedding_client: Option<Arc<dyn ModelEmbeddingClient>>,
    expected_dimension: usize,
}

impl CyreneMemoryService {
    pub fn new(
        backend: Arc<dyn MemoryStorageBackend>,
        embedding_client: Option<Arc<dyn ModelEmbeddingClient>>,
        expected_dimension: usize,
    ) -> Self {
        Self {
            backend,
            embedding_client,
            expected_dimension,
        }
    }

    fn now_ms(&self) -> i64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_millis() as i64)
            .unwrap_or(0)
    }

    /// 1. Store
    /// 1. 存储（Store）。
    pub async fn store(&self, req: StoreMemoryRequest) -> StoreMemoryResponse {
        let tenant_id = req.tenant_id.trim();
        if tenant_id.is_empty() {
            return StoreMemoryResponse {
                result: Some(store_memory_response::Result::Error(MemoryError {
                    code: MemoryErrorCode::TenantMismatch as i32,
                    message: "tenant_id cannot be empty".into(),
                    retryable: false,
                })),
            };
        }

        let item = match req.item {
            Some(i) => i,
            None => {
                return StoreMemoryResponse {
                    result: Some(store_memory_response::Result::Error(MemoryError {
                        code: MemoryErrorCode::StorageFailed as i32,
                        message: "item cannot be null".into(),
                        retryable: false,
                    })),
                };
            }
        };

        // Dimension mismatch validation (Task T72)
        // 维度不匹配校验（任务 T72）。
        if !item.embedding.is_empty() && item.embedding.len() != self.expected_dimension {
            return StoreMemoryResponse {
                result: Some(store_memory_response::Result::Error(MemoryError {
                    code: MemoryErrorCode::VectorDimensionMismatch as i32,
                    message: format!(
                        "Dimension mismatch: expected {}, got {}",
                        self.expected_dimension,
                        item.embedding.len()
                    ),
                    retryable: false,
                })),
            };
        }

        // Embedding generation if needed (Task T69)
        // 按需生成 embedding（任务 T69）。
        let embedding = if item.embedding.is_empty() {
            if let Some(ref client) = self.embedding_client {
                match client
                    .generate_embeddings(crate::embedding::EmbeddingRequest {
                        model: "default-embedding".into(),
                        input_texts: vec![item.content.clone()],
                    })
                    .await
                {
                    Ok(resp) => {
                        if resp.embeddings.is_empty() {
                            vec![0.0f32; self.expected_dimension]
                        } else {
                            resp.embeddings[0].clone()
                        }
                    }
                    Err(e) => {
                        return StoreMemoryResponse {
                            result: Some(store_memory_response::Result::Error(MemoryError {
                                code: MemoryErrorCode::StorageFailed as i32,
                                message: format!(
                                    "Failed to obtain embedding from model.provider.v1: {}",
                                    e
                                ),
                                retryable: true,
                            })),
                        };
                    }
                }
            } else {
                vec![0.0f32; self.expected_dimension]
            }
        } else {
            item.embedding
        };

        let now = self.now_ms();
        let expires_at_ms = item
            .expires_at_ms
            .or_else(|| item.ttl_seconds.map(|ttl| now + (ttl * 1000)));

        let record = MemoryRecord {
            item_id: if item.item_id.is_empty() {
                format!("mem_{}", now)
            } else {
                item.item_id
            },
            tenant_id: tenant_id.into(),
            scope: if item.scope.is_empty() {
                "default".into()
            } else {
                item.scope
            },
            subject: if item.subject.is_empty() {
                "general".into()
            } else {
                item.subject
            },
            content: item.content,
            embedding,
            metadata: item.metadata,
            provenance: MemoryProvenance {
                origin_channel: "direct".into(),
                source_entity_id: tenant_id.into(),
                session_id: None,
                created_by: "system".into(),
            },
            embedding_profile: EmbeddingProfile {
                model_id: "default".into(),
                dimension: self.expected_dimension,
                is_normalized: true,
            },
            review_facts: ReviewFacts {
                review_status: "APPROVED".into(),
                reviewed_by: None,
                reviewed_at_ms: None,
                flags: vec![],
            },
            created_at_ms: if item.created_at_ms > 0 {
                item.created_at_ms
            } else {
                now
            },
            updated_at_ms: now,
            expires_at_ms,
            ttl_seconds: item.ttl_seconds,
        };

        match self.backend.store(record).await {
            Ok(id) => StoreMemoryResponse {
                result: Some(store_memory_response::Result::ItemId(id)),
            },
            Err(e) => StoreMemoryResponse {
                result: Some(store_memory_response::Result::Error(e)),
            },
        }
    }

    /// 2. Get
    /// 2. 获取（Get）。
    pub async fn get(&self, req: GetMemoryRequest) -> GetMemoryResponse {
        let tenant_id = req.tenant_id.trim();
        match self.backend.get(tenant_id, &req.item_id).await {
            Ok(Some(record)) => {
                let now = self.now_ms();
                if record.is_expired(now) {
                    return GetMemoryResponse {
                        result: Some(get_memory_response::Result::Error(MemoryError {
                            code: MemoryErrorCode::NotFound as i32,
                            message: "Memory item has expired".into(),
                            retryable: false,
                        })),
                    };
                }
                GetMemoryResponse {
                    result: Some(get_memory_response::Result::Item(MemoryItem {
                        item_id: record.item_id,
                        tenant_id: record.tenant_id,
                        scope: record.scope,
                        subject: record.subject,
                        content: record.content,
                        embedding: record.embedding,
                        metadata: record.metadata,
                        created_at_ms: record.created_at_ms,
                        expires_at_ms: record.expires_at_ms,
                        ttl_seconds: record.ttl_seconds,
                    })),
                }
            }
            Ok(None) => GetMemoryResponse {
                result: Some(get_memory_response::Result::Error(MemoryError {
                    code: MemoryErrorCode::NotFound as i32,
                    message: "Memory item not found".into(),
                    retryable: false,
                })),
            },
            Err(e) => GetMemoryResponse {
                result: Some(get_memory_response::Result::Error(e)),
            },
        }
    }

    /// 3. Recall
    /// 3. 召回（Recall）。
    pub async fn recall(&self, req: RecallMemoryRequest) -> RecallMemoryResponse {
        let tenant_id = req.tenant_id.trim();
        if tenant_id.is_empty() {
            return RecallMemoryResponse {
                result: Some(recall_memory_response::Result::Error(MemoryError {
                    code: MemoryErrorCode::TenantMismatch as i32,
                    message: "tenant_id cannot be empty".into(),
                    retryable: false,
                })),
            };
        }

        let query_embedding = if !req.query_embedding.is_empty() {
            if req.query_embedding.len() != self.expected_dimension {
                return RecallMemoryResponse {
                    result: Some(recall_memory_response::Result::Error(MemoryError {
                        code: MemoryErrorCode::VectorDimensionMismatch as i32,
                        message: format!(
                            "Query dimension mismatch: expected {}, got {}",
                            self.expected_dimension,
                            req.query_embedding.len()
                        ),
                        retryable: false,
                    })),
                };
            }
            req.query_embedding
        } else if let Some(ref text) = req.query_text {
            if let Some(ref client) = self.embedding_client {
                match client
                    .generate_embeddings(crate::embedding::EmbeddingRequest {
                        model: "default-embedding".into(),
                        input_texts: vec![text.clone()],
                    })
                    .await
                {
                    Ok(resp) => {
                        if resp.embeddings.is_empty() {
                            vec![0.0f32; self.expected_dimension]
                        } else {
                            resp.embeddings[0].clone()
                        }
                    }
                    Err(e) => {
                        return RecallMemoryResponse {
                            result: Some(recall_memory_response::Result::Error(MemoryError {
                                code: MemoryErrorCode::StorageFailed as i32,
                                message: format!("Failed to obtain query embedding: {}", e),
                                retryable: true,
                            })),
                        };
                    }
                }
            } else {
                vec![0.0f32; self.expected_dimension]
            }
        } else {
            return RecallMemoryResponse {
                result: Some(recall_memory_response::Result::Error(MemoryError {
                    code: MemoryErrorCode::StorageFailed as i32,
                    message: "Either query_embedding or query_text must be provided".into(),
                    retryable: false,
                })),
            };
        };

        let top_k = req.top_k.unwrap_or(10).max(1) as usize;
        let now = self.now_ms();

        match self
            .backend
            .recall(
                tenant_id,
                &query_embedding,
                req.scope.as_deref(),
                req.subject.as_deref(),
                req.min_similarity,
                top_k,
                now,
            )
            .await
        {
            Ok(matches) => RecallMemoryResponse {
                result: Some(recall_memory_response::Result::Matches(RecallBatch {
                    matches,
                })),
            },
            Err(e) => RecallMemoryResponse {
                result: Some(recall_memory_response::Result::Error(e)),
            },
        }
    }

    /// 4. Delete
    /// 4. 删除（Delete）。
    pub async fn delete(&self, req: DeleteMemoryRequest) -> DeleteMemoryResponse {
        let tenant_id = req.tenant_id.trim();
        match self.backend.delete(tenant_id, &req.item_id).await {
            Ok(deleted) => DeleteMemoryResponse {
                result: Some(delete_memory_response::Result::Deleted(deleted)),
            },
            Err(e) => DeleteMemoryResponse {
                result: Some(delete_memory_response::Result::Error(e)),
            },
        }
    }

    /// 5. Prune
    /// 5. 清理（Prune）。
    pub async fn prune(&self, req: PruneMemoryRequest) -> PruneMemoryResponse {
        let tenant_id = req.tenant_id.trim();
        let now = self.now_ms();
        match self
            .backend
            .prune(tenant_id, req.before_timestamp_ms, now)
            .await
        {
            Ok(count) => PruneMemoryResponse {
                result: Some(prune_memory_response::Result::PrunedCount(count)),
            },
            Err(e) => PruneMemoryResponse {
                result: Some(prune_memory_response::Result::Error(e)),
            },
        }
    }

    /// 6. Export Batch
    /// 6. 批量导出（Export Batch）。
    pub async fn export_batch(&self, req: ExportMemoryRequest) -> ExportMemoryResponse {
        let tenant_id = req.tenant_id.trim();
        let limit = req.limit.map(|l| l.max(1) as usize);
        match self
            .backend
            .export_batch(tenant_id, req.scope.as_deref(), limit)
            .await
        {
            Ok(records) => {
                let items = records
                    .into_iter()
                    .map(|r| MemoryItem {
                        item_id: r.item_id,
                        tenant_id: r.tenant_id,
                        scope: r.scope,
                        subject: r.subject,
                        content: r.content,
                        embedding: r.embedding,
                        metadata: r.metadata,
                        created_at_ms: r.created_at_ms,
                        expires_at_ms: r.expires_at_ms,
                        ttl_seconds: r.ttl_seconds,
                    })
                    .collect();
                ExportMemoryResponse {
                    result: Some(export_memory_response::Result::Batch(ExportBatch { items })),
                }
            }
            Err(e) => ExportMemoryResponse {
                result: Some(export_memory_response::Result::Error(e)),
            },
        }
    }

    /// 7. Import Batch
    /// 7. 批量导入（Import Batch）。
    pub async fn import_batch(&self, req: ImportMemoryRequest) -> ImportMemoryResponse {
        let tenant_id = req.tenant_id.trim();
        let now = self.now_ms();
        let mut records = Vec::with_capacity(req.items.len());

        for item in req.items {
            if item.tenant_id != tenant_id {
                return ImportMemoryResponse {
                    result: Some(import_memory_response::Result::Error(MemoryError {
                        code: MemoryErrorCode::TenantMismatch as i32,
                        message: format!(
                            "Item tenant '{}' does not match request tenant '{}'",
                            item.tenant_id, tenant_id
                        ),
                        retryable: false,
                    })),
                };
            }
            if !item.embedding.is_empty() && item.embedding.len() != self.expected_dimension {
                return ImportMemoryResponse {
                    result: Some(import_memory_response::Result::Error(MemoryError {
                        code: MemoryErrorCode::VectorDimensionMismatch as i32,
                        message: format!(
                            "Dimension mismatch in import: expected {}, got {}",
                            self.expected_dimension,
                            item.embedding.len()
                        ),
                        retryable: false,
                    })),
                };
            }
            let expires_at_ms = item
                .expires_at_ms
                .or_else(|| item.ttl_seconds.map(|ttl| now + (ttl * 1000)));

            records.push(MemoryRecord {
                item_id: item.item_id,
                tenant_id: item.tenant_id,
                scope: if item.scope.is_empty() {
                    "default".into()
                } else {
                    item.scope
                },
                subject: if item.subject.is_empty() {
                    "general".into()
                } else {
                    item.subject
                },
                content: item.content,
                embedding: if item.embedding.is_empty() {
                    vec![0.0; self.expected_dimension]
                } else {
                    item.embedding
                },
                metadata: item.metadata,
                provenance: MemoryProvenance {
                    origin_channel: "import".into(),
                    source_entity_id: tenant_id.into(),
                    session_id: None,
                    created_by: "system".into(),
                },
                embedding_profile: EmbeddingProfile {
                    model_id: "default".into(),
                    dimension: self.expected_dimension,
                    is_normalized: true,
                },
                review_facts: ReviewFacts {
                    review_status: "APPROVED".into(),
                    reviewed_by: None,
                    reviewed_at_ms: None,
                    flags: vec![],
                },
                created_at_ms: if item.created_at_ms > 0 {
                    item.created_at_ms
                } else {
                    now
                },
                updated_at_ms: now,
                expires_at_ms,
                ttl_seconds: item.ttl_seconds,
            });
        }

        match self
            .backend
            .import_batch(tenant_id, records, req.overwrite)
            .await
        {
            Ok(count) => ImportMemoryResponse {
                result: Some(import_memory_response::Result::ImportedCount(count)),
            },
            Err(e) => ImportMemoryResponse {
                result: Some(import_memory_response::Result::Error(e)),
            },
        }
    }
}
