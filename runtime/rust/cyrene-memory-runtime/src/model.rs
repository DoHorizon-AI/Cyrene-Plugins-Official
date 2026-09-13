// SPDX-License-Identifier: Apache-2.0
//! Explicit Domain Models for Cyrene Memory Provider (Task T68)
//!
//! Models tenant, scope, subject, provenance, TTL, embedding profile,
//! timestamps, and review facts explicitly.

use serde::{Deserialize, Serialize};
use std::collections::HashMap;

/// Provenance metadata tracking the origin and source chain of a memory item.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct MemoryProvenance {
    pub origin_channel: String,
    pub source_entity_id: String,
    pub session_id: Option<String>,
    pub created_by: String,
}

/// Profile metadata for embedding vectors.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct EmbeddingProfile {
    pub model_id: String,
    pub dimension: usize,
    pub is_normalized: bool,
}

/// Audit and review facts associated with a memory item.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ReviewFacts {
    pub review_status: String, // e.g. "APPROVED", "PENDING", "REJECTED"
    pub reviewed_by: Option<String>,
    pub reviewed_at_ms: Option<i64>,
    pub flags: Vec<String>,
}

/// Explicit memory record encapsulating domain invariants.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct MemoryRecord {
    pub item_id: String,
    pub tenant_id: String,
    pub scope: String,
    pub subject: String,
    pub content: String,
    pub embedding: Vec<f32>,
    pub metadata: HashMap<String, String>,
    pub provenance: MemoryProvenance,
    pub embedding_profile: EmbeddingProfile,
    pub review_facts: ReviewFacts,
    pub created_at_ms: i64,
    pub updated_at_ms: i64,
    pub expires_at_ms: Option<i64>,
    pub ttl_seconds: Option<i64>,
}

impl MemoryRecord {
    pub fn is_expired(&self, current_time_ms: i64) -> bool {
        if let Some(expires_at) = self.expires_at_ms {
            return current_time_ms >= expires_at;
        }
        false
    }
}
