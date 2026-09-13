// SPDX-License-Identifier: Apache-2.0
//! Storage Backends for Cyrene Memory Provider (Task T67)
//!
//! PostgreSQL/pgvector is designated for production; SQLite is used for development/testing.

pub mod pgvector;
pub mod sqlite;

use crate::model::MemoryRecord;
use async_trait::async_trait;
use cyrene_plugin_contracts::memory_provider_v1::{MemoryError, MemoryMatch};

#[async_trait]
pub trait MemoryStorageBackend: Send + Sync {
    /// Idempotent store of a memory record.
    async fn store(&self, record: MemoryRecord) -> Result<String, MemoryError>;

    /// Retrieve a single item by tenant and item_id.
    async fn get(
        &self,
        tenant_id: &str,
        item_id: &str,
    ) -> Result<Option<MemoryRecord>, MemoryError>;

    /// Perform vector similarity recall filtered by tenant, scope, subject, and min_similarity.
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
    ) -> Result<Vec<MemoryMatch>, MemoryError>;

    /// Exact delete by tenant and item_id.
    async fn delete(&self, tenant_id: &str, item_id: &str) -> Result<bool, MemoryError>;

    /// Prune expired records or records created before a specified timestamp.
    async fn prune(
        &self,
        tenant_id: &str,
        before_timestamp_ms: Option<i64>,
        current_time_ms: i64,
    ) -> Result<i64, MemoryError>;

    /// Export records for a given tenant with optional scope and limit.
    async fn export_batch(
        &self,
        tenant_id: &str,
        scope: Option<&str>,
        limit: Option<usize>,
    ) -> Result<Vec<MemoryRecord>, MemoryError>;

    /// Import a batch of records with atomic transaction rollback on failure.
    async fn import_batch(
        &self,
        tenant_id: &str,
        records: Vec<MemoryRecord>,
        overwrite: bool,
    ) -> Result<i32, MemoryError>;
}

/// Helper to compute cosine similarity between two float vectors.
pub fn cosine_similarity(a: &[f32], b: &[f32]) -> f32 {
    if a.len() != b.len() || a.is_empty() {
        return 0.0;
    }
    let mut dot = 0.0f32;
    let mut norm_a = 0.0f32;
    let mut norm_b = 0.0f32;
    for (x, y) in a.iter().zip(b.iter()) {
        dot += x * y;
        norm_a += x * x;
        norm_b += y * y;
    }
    let denom = norm_a.sqrt() * norm_b.sqrt();
    if denom > 1e-6 {
        (dot / denom).clamp(-1.0, 1.0)
    } else {
        0.0
    }
}
