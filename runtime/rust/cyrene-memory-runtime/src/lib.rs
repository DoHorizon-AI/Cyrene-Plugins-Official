// SPDX-License-Identifier: Apache-2.0
//! Cyrene Canonical Memory Provider Runtime (Milestone M5B)
//!
//! Provides pure memory capability:
//! - Pure capability layer: memory.provider.v1
//! - Multi-tenant isolation by tenant_id
//! - Vector recall with similarity scoring
//! - PostgreSQL/pgvector production backend builder & SQLite development backend
//! - Zero profile inference, proactive state, dream audits, or knowledge graphs

pub mod backend;
pub mod embedding;
pub mod model;
pub mod service;

pub use backend::pgvector::{PgVectorConfig, PgVectorIndexType, PgVectorSchemaBuilder};
pub use backend::sqlite::SqliteMemoryBackend;
pub use backend::MemoryStorageBackend;
pub use embedding::{ContractModelEmbeddingClient, ModelEmbeddingClient};
pub use model::{EmbeddingProfile, MemoryProvenance, MemoryRecord, ReviewFacts};
pub use service::CyreneMemoryService;
