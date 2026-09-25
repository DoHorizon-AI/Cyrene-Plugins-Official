// SPDX-License-Identifier: Apache-2.0
//! SQLite Development Storage Backend (Task T67, T70, T72)
//!
//! Provides transactional storage, tenant-isolated vector recall, TTL pruning,
//! and atomic import rollback for local development and testing.
//! SQLite 开发存储后端（任务 T67、T70、T72）。
//! 为本地开发和测试提供事务化存储、tenant 隔离的向量 recall、TTL 清理以及导入失败时的原子回滚。

use crate::backend::{cosine_similarity, MemoryStorageBackend};
use crate::model::MemoryRecord;
use async_trait::async_trait;
use cyrene_plugin_contracts::memory_provider_v1::{
    MemoryError, MemoryErrorCode, MemoryItem, MemoryMatch,
};
use rusqlite::{params, Connection, OptionalExtension};
use std::sync::Arc;
use tokio::sync::Mutex;

pub struct SqliteMemoryBackend {
    conn: Arc<Mutex<Connection>>,
}

impl SqliteMemoryBackend {
    /// Initialize in-memory SQLite database.
    /// 初始化内存 SQLite 数据库。
    pub fn new_in_memory() -> Result<Self, rusqlite::Error> {
        let conn = Connection::open_in_memory()?;
        Self::init_schema(&conn)?;
        Ok(Self {
            conn: Arc::new(Mutex::new(conn)),
        })
    }

    /// Initialize SQLite database at the specified file path.
    /// 在指定文件路径初始化 SQLite 数据库。
    pub fn new_file(path: &str) -> Result<Self, rusqlite::Error> {
        let conn = Connection::open(path)?;
        Self::init_schema(&conn)?;
        Ok(Self {
            conn: Arc::new(Mutex::new(conn)),
        })
    }

    fn init_schema(conn: &Connection) -> Result<(), rusqlite::Error> {
        conn.execute_batch(
            r#"
            CREATE TABLE IF NOT EXISTS cyrene_memory_records (
                item_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                scope TEXT NOT NULL,
                subject TEXT NOT NULL,
                content TEXT NOT NULL,
                embedding_json TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                provenance_json TEXT NOT NULL,
                embedding_profile_json TEXT NOT NULL,
                review_facts_json TEXT NOT NULL,
                created_at_ms INTEGER NOT NULL,
                updated_at_ms INTEGER NOT NULL,
                expires_at_ms INTEGER,
                ttl_seconds INTEGER,
                PRIMARY KEY (tenant_id, item_id)
            );
            CREATE INDEX IF NOT EXISTS idx_mem_tenant_scope ON cyrene_memory_records(tenant_id, scope);
            CREATE INDEX IF NOT EXISTS idx_mem_tenant_expiry ON cyrene_memory_records(tenant_id, expires_at_ms);
            "#,
        )?;
        Ok(())
    }
}

#[async_trait]
impl MemoryStorageBackend for SqliteMemoryBackend {
    async fn store(&self, record: MemoryRecord) -> Result<String, MemoryError> {
        let conn = self.conn.lock().await;
        let embedding_json = serde_json::to_string(&record.embedding).map_err(|e| MemoryError {
            code: MemoryErrorCode::StorageFailed as i32,
            message: format!("Failed to serialize embedding: {}", e),
            retryable: false,
        })?;
        let metadata_json = serde_json::to_string(&record.metadata).unwrap_or_default();
        let provenance_json = serde_json::to_string(&record.provenance).unwrap_or_default();
        let profile_json = serde_json::to_string(&record.embedding_profile).unwrap_or_default();
        let review_json = serde_json::to_string(&record.review_facts).unwrap_or_default();

        conn.execute(
            r#"
            INSERT INTO cyrene_memory_records (
                item_id, tenant_id, scope, subject, content, embedding_json,
                metadata_json, provenance_json, embedding_profile_json, review_facts_json,
                created_at_ms, updated_at_ms, expires_at_ms, ttl_seconds
            ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14)
            ON CONFLICT(tenant_id, item_id) DO UPDATE SET
                scope = excluded.scope,
                subject = excluded.subject,
                content = excluded.content,
                embedding_json = excluded.embedding_json,
                metadata_json = excluded.metadata_json,
                provenance_json = excluded.provenance_json,
                embedding_profile_json = excluded.embedding_profile_json,
                review_facts_json = excluded.review_facts_json,
                updated_at_ms = excluded.updated_at_ms,
                expires_at_ms = excluded.expires_at_ms,
                ttl_seconds = excluded.ttl_seconds
            "#,
            params![
                record.item_id,
                record.tenant_id,
                record.scope,
                record.subject,
                record.content,
                embedding_json,
                metadata_json,
                provenance_json,
                profile_json,
                review_json,
                record.created_at_ms,
                record.updated_at_ms,
                record.expires_at_ms,
                record.ttl_seconds,
            ],
        )
        .map_err(|e| MemoryError {
            code: MemoryErrorCode::StorageFailed as i32,
            message: format!("SQLite store error: {}", e),
            retryable: false,
        })?;

        Ok(record.item_id)
    }

    async fn get(
        &self,
        tenant_id: &str,
        item_id: &str,
    ) -> Result<Option<MemoryRecord>, MemoryError> {
        let conn = self.conn.lock().await;
        let mut stmt = conn
            .prepare(
                r#"
            SELECT item_id, tenant_id, scope, subject, content, embedding_json,
                   metadata_json, provenance_json, embedding_profile_json, review_facts_json,
                   created_at_ms, updated_at_ms, expires_at_ms, ttl_seconds
            FROM cyrene_memory_records
            WHERE tenant_id = ?1 AND item_id = ?2
            "#,
            )
            .map_err(|e| MemoryError {
                code: MemoryErrorCode::StorageFailed as i32,
                message: e.to_string(),
                retryable: false,
            })?;

        let res = stmt
            .query_row(params![tenant_id, item_id], |row| {
                let item_id: String = row.get(0)?;
                let tenant_id: String = row.get(1)?;
                let scope: String = row.get(2)?;
                let subject: String = row.get(3)?;
                let content: String = row.get(4)?;
                let embedding_json: String = row.get(5)?;
                let metadata_json: String = row.get(6)?;
                let provenance_json: String = row.get(7)?;
                let profile_json: String = row.get(8)?;
                let review_json: String = row.get(9)?;
                let created_at_ms: i64 = row.get(10)?;
                let updated_at_ms: i64 = row.get(11)?;
                let expires_at_ms: Option<i64> = row.get(12)?;
                let ttl_seconds: Option<i64> = row.get(13)?;

                let embedding: Vec<f32> = serde_json::from_str(&embedding_json).unwrap_or_default();
                let metadata = serde_json::from_str(&metadata_json).unwrap_or_default();
                let provenance = serde_json::from_str(&provenance_json).unwrap_or_else(|_| {
                    crate::model::MemoryProvenance {
                        origin_channel: "default".into(),
                        source_entity_id: "default".into(),
                        session_id: None,
                        created_by: "system".into(),
                    }
                });
                let embedding_profile = serde_json::from_str(&profile_json).unwrap_or_else(|_| {
                    crate::model::EmbeddingProfile {
                        model_id: "default".into(),
                        dimension: embedding.len(),
                        is_normalized: true,
                    }
                });
                let review_facts = serde_json::from_str(&review_json).unwrap_or_else(|_| {
                    crate::model::ReviewFacts {
                        review_status: "APPROVED".into(),
                        reviewed_by: None,
                        reviewed_at_ms: None,
                        flags: vec![],
                    }
                });

                Ok(MemoryRecord {
                    item_id,
                    tenant_id,
                    scope,
                    subject,
                    content,
                    embedding,
                    metadata,
                    provenance,
                    embedding_profile,
                    review_facts,
                    created_at_ms,
                    updated_at_ms,
                    expires_at_ms,
                    ttl_seconds,
                })
            })
            .optional()
            .map_err(|e| MemoryError {
                code: MemoryErrorCode::StorageFailed as i32,
                message: e.to_string(),
                retryable: false,
            })?;

        Ok(res)
    }

    #[allow(clippy::too_many_arguments)]
    async fn recall(
        &self,
        tenant_id: &str,
        query_embedding: &[f32],
        scope: Option<&str>,
        subject: Option<&str>,
        min_similarity: Option<f32>,
        top_k: usize,
        current_time_ms: i64,
    ) -> Result<Vec<MemoryMatch>, MemoryError> {
        let conn = self.conn.lock().await;
        let mut stmt = conn
            .prepare(
                r#"
            SELECT item_id, tenant_id, scope, subject, content, embedding_json,
                   metadata_json, created_at_ms, expires_at_ms, ttl_seconds
            FROM cyrene_memory_records
            WHERE tenant_id = ?1
              AND (expires_at_ms IS NULL OR expires_at_ms > ?2)
            "#,
            )
            .map_err(|e| MemoryError {
                code: MemoryErrorCode::StorageFailed as i32,
                message: e.to_string(),
                retryable: false,
            })?;

        let rows = stmt
            .query_map(params![tenant_id, current_time_ms], |row| {
                let item_id: String = row.get(0)?;
                let tenant_id: String = row.get(1)?;
                let item_scope: String = row.get(2)?;
                let item_subject: String = row.get(3)?;
                let content: String = row.get(4)?;
                let embedding_json: String = row.get(5)?;
                let metadata_json: String = row.get(6)?;
                let created_at_ms: i64 = row.get(7)?;
                let expires_at_ms: Option<i64> = row.get(8)?;
                let ttl_seconds: Option<i64> = row.get(9)?;

                let embedding: Vec<f32> = serde_json::from_str(&embedding_json).unwrap_or_default();
                let metadata = serde_json::from_str(&metadata_json).unwrap_or_default();

                Ok(MemoryItem {
                    item_id,
                    tenant_id,
                    scope: item_scope,
                    subject: item_subject,
                    content,
                    embedding,
                    metadata,
                    created_at_ms,
                    expires_at_ms,
                    ttl_seconds,
                })
            })
            .map_err(|e| MemoryError {
                code: MemoryErrorCode::StorageFailed as i32,
                message: e.to_string(),
                retryable: false,
            })?;

        let threshold = min_similarity.unwrap_or(0.0);
        let mut scored_matches = Vec::new();

        for row in rows {
            let item = row.map_err(|e| MemoryError {
                code: MemoryErrorCode::StorageFailed as i32,
                message: e.to_string(),
                retryable: false,
            })?;

            // Apply optional scope and subject filters
            // 应用可选的 scope 和 subject 过滤条件。
            if let Some(s) = scope {
                if item.scope != s {
                    continue;
                }
            }
            if let Some(sb) = subject {
                if item.subject != sb {
                    continue;
                }
            }

            let score = cosine_similarity(query_embedding, &item.embedding);
            if score >= threshold {
                scored_matches.push(MemoryMatch {
                    item: Some(item),
                    score,
                });
            }
        }

        // Sort descending by score
        // 按评分从高到低排序。
        scored_matches.sort_by(|a, b| {
            b.score
                .partial_cmp(&a.score)
                .unwrap_or(std::cmp::Ordering::Equal)
        });
        scored_matches.truncate(top_k);

        Ok(scored_matches)
    }

    async fn delete(&self, tenant_id: &str, item_id: &str) -> Result<bool, MemoryError> {
        let conn = self.conn.lock().await;
        let affected = conn
            .execute(
                "DELETE FROM cyrene_memory_records WHERE tenant_id = ?1 AND item_id = ?2",
                params![tenant_id, item_id],
            )
            .map_err(|e| MemoryError {
                code: MemoryErrorCode::StorageFailed as i32,
                message: e.to_string(),
                retryable: false,
            })?;
        Ok(affected > 0)
    }

    async fn prune(
        &self,
        tenant_id: &str,
        before_timestamp_ms: Option<i64>,
        current_time_ms: i64,
    ) -> Result<i64, MemoryError> {
        let conn = self.conn.lock().await;
        let affected = if let Some(before) = before_timestamp_ms {
            conn.execute(
                r#"
                DELETE FROM cyrene_memory_records
                WHERE tenant_id = ?1
                  AND (
                    (expires_at_ms IS NOT NULL AND expires_at_ms <= ?2)
                    OR created_at_ms < ?3
                  )
                "#,
                params![tenant_id, current_time_ms, before],
            )
        } else {
            conn.execute(
                r#"
                DELETE FROM cyrene_memory_records
                WHERE tenant_id = ?1
                  AND (expires_at_ms IS NOT NULL AND expires_at_ms <= ?2)
                "#,
                params![tenant_id, current_time_ms],
            )
        }
        .map_err(|e| MemoryError {
            code: MemoryErrorCode::StorageFailed as i32,
            message: e.to_string(),
            retryable: false,
        })?;

        Ok(affected as i64)
    }

    async fn export_batch(
        &self,
        tenant_id: &str,
        scope: Option<&str>,
        limit: Option<usize>,
    ) -> Result<Vec<MemoryRecord>, MemoryError> {
        let conn = self.conn.lock().await;
        let limit_val = limit.unwrap_or(1000) as i64;
        let sql = match scope {
            Some(_) => {
                r#"
                SELECT item_id, tenant_id, scope, subject, content, embedding_json,
                       metadata_json, provenance_json, embedding_profile_json, review_facts_json,
                       created_at_ms, updated_at_ms, expires_at_ms, ttl_seconds
                FROM cyrene_memory_records
                WHERE tenant_id = ?1 AND scope = ?2
                ORDER BY created_at_ms ASC
                LIMIT ?3
                "#
            }
            None => {
                r#"
                SELECT item_id, tenant_id, scope, subject, content, embedding_json,
                       metadata_json, provenance_json, embedding_profile_json, review_facts_json,
                       created_at_ms, updated_at_ms, expires_at_ms, ttl_seconds
                FROM cyrene_memory_records
                WHERE tenant_id = ?1
                ORDER BY created_at_ms ASC
                LIMIT ?2
                "#
            }
        };

        let mut stmt = conn.prepare(sql).map_err(|e| MemoryError {
            code: MemoryErrorCode::StorageFailed as i32,
            message: e.to_string(),
            retryable: false,
        })?;

        let map_fn = |row: &rusqlite::Row| {
            let item_id: String = row.get(0)?;
            let tenant_id: String = row.get(1)?;
            let scope: String = row.get(2)?;
            let subject: String = row.get(3)?;
            let content: String = row.get(4)?;
            let embedding_json: String = row.get(5)?;
            let metadata_json: String = row.get(6)?;
            let provenance_json: String = row.get(7)?;
            let profile_json: String = row.get(8)?;
            let review_json: String = row.get(9)?;
            let created_at_ms: i64 = row.get(10)?;
            let updated_at_ms: i64 = row.get(11)?;
            let expires_at_ms: Option<i64> = row.get(12)?;
            let ttl_seconds: Option<i64> = row.get(13)?;

            let embedding: Vec<f32> = serde_json::from_str(&embedding_json).unwrap_or_default();
            let metadata = serde_json::from_str(&metadata_json).unwrap_or_default();
            let provenance = serde_json::from_str(&provenance_json).unwrap_or_else(|_| {
                crate::model::MemoryProvenance {
                    origin_channel: "default".into(),
                    source_entity_id: "default".into(),
                    session_id: None,
                    created_by: "system".into(),
                }
            });
            let embedding_profile = serde_json::from_str(&profile_json).unwrap_or_else(|_| {
                crate::model::EmbeddingProfile {
                    model_id: "default".into(),
                    dimension: embedding.len(),
                    is_normalized: true,
                }
            });
            let review_facts =
                serde_json::from_str(&review_json).unwrap_or_else(|_| crate::model::ReviewFacts {
                    review_status: "APPROVED".into(),
                    reviewed_by: None,
                    reviewed_at_ms: None,
                    flags: vec![],
                });

            Ok(MemoryRecord {
                item_id,
                tenant_id,
                scope,
                subject,
                content,
                embedding,
                metadata,
                provenance,
                embedding_profile,
                review_facts,
                created_at_ms,
                updated_at_ms,
                expires_at_ms,
                ttl_seconds,
            })
        };

        let records = if let Some(s) = scope {
            stmt.query_map(params![tenant_id, s, limit_val], map_fn)
        } else {
            stmt.query_map(params![tenant_id, limit_val], map_fn)
        }
        .map_err(|e| MemoryError {
            code: MemoryErrorCode::StorageFailed as i32,
            message: e.to_string(),
            retryable: false,
        })?;

        let mut out = Vec::new();
        for r in records {
            out.push(r.map_err(|e| MemoryError {
                code: MemoryErrorCode::StorageFailed as i32,
                message: e.to_string(),
                retryable: false,
            })?);
        }
        Ok(out)
    }

    async fn import_batch(
        &self,
        tenant_id: &str,
        records: Vec<MemoryRecord>,
        overwrite: bool,
    ) -> Result<i32, MemoryError> {
        let mut conn = self.conn.lock().await;
        // Atomic transaction with rollback on error (Task T72)
        // 使用原子事务；发生错误时回滚（任务 T72）。
        let tx = conn.transaction().map_err(|e| MemoryError {
            code: MemoryErrorCode::StorageFailed as i32,
            message: format!("Failed to open SQLite transaction: {}", e),
            retryable: false,
        })?;

        let mut imported = 0;
        for record in records {
            if record.tenant_id != tenant_id {
                return Err(MemoryError {
                    code: MemoryErrorCode::TenantMismatch as i32,
                    message: format!(
                        "Record tenant_id '{}' does not match request tenant '{}'",
                        record.tenant_id, tenant_id
                    ),
                    retryable: false,
                });
            }

            let embedding_json = serde_json::to_string(&record.embedding).unwrap_or_default();
            let metadata_json = serde_json::to_string(&record.metadata).unwrap_or_default();
            let provenance_json = serde_json::to_string(&record.provenance).unwrap_or_default();
            let profile_json = serde_json::to_string(&record.embedding_profile).unwrap_or_default();
            let review_json = serde_json::to_string(&record.review_facts).unwrap_or_default();

            if overwrite {
                tx.execute(
                    r#"
                    INSERT INTO cyrene_memory_records (
                        item_id, tenant_id, scope, subject, content, embedding_json,
                        metadata_json, provenance_json, embedding_profile_json, review_facts_json,
                        created_at_ms, updated_at_ms, expires_at_ms, ttl_seconds
                    ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14)
                    ON CONFLICT(tenant_id, item_id) DO UPDATE SET
                        scope = excluded.scope,
                        subject = excluded.subject,
                        content = excluded.content,
                        embedding_json = excluded.embedding_json,
                        metadata_json = excluded.metadata_json,
                        provenance_json = excluded.provenance_json,
                        embedding_profile_json = excluded.embedding_profile_json,
                        review_facts_json = excluded.review_facts_json,
                        updated_at_ms = excluded.updated_at_ms,
                        expires_at_ms = excluded.expires_at_ms,
                        ttl_seconds = excluded.ttl_seconds
                    "#,
                    params![
                        record.item_id,
                        record.tenant_id,
                        record.scope,
                        record.subject,
                        record.content,
                        embedding_json,
                        metadata_json,
                        provenance_json,
                        profile_json,
                        review_json,
                        record.created_at_ms,
                        record.updated_at_ms,
                        record.expires_at_ms,
                        record.ttl_seconds,
                    ],
                )
                .map_err(|e| MemoryError {
                    code: MemoryErrorCode::StorageFailed as i32,
                    message: format!("SQLite import error: {}", e),
                    retryable: false,
                })?;
            } else {
                let res = tx
                    .execute(
                        r#"
                    INSERT OR IGNORE INTO cyrene_memory_records (
                        item_id, tenant_id, scope, subject, content, embedding_json,
                        metadata_json, provenance_json, embedding_profile_json, review_facts_json,
                        created_at_ms, updated_at_ms, expires_at_ms, ttl_seconds
                    ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14)
                    "#,
                        params![
                            record.item_id,
                            record.tenant_id,
                            record.scope,
                            record.subject,
                            record.content,
                            embedding_json,
                            metadata_json,
                            provenance_json,
                            profile_json,
                            review_json,
                            record.created_at_ms,
                            record.updated_at_ms,
                            record.expires_at_ms,
                            record.ttl_seconds,
                        ],
                    )
                    .map_err(|e| MemoryError {
                        code: MemoryErrorCode::StorageFailed as i32,
                        message: format!("SQLite import error: {}", e),
                        retryable: false,
                    })?;
                if res == 0 {
                    continue; // skipped duplicate; 已跳过重复项
                }
            }
            imported += 1;
        }

        tx.commit().map_err(|e| MemoryError {
            code: MemoryErrorCode::StorageFailed as i32,
            message: format!("Failed to commit SQLite transaction: {}", e),
            retryable: false,
        })?;

        Ok(imported)
    }
}
