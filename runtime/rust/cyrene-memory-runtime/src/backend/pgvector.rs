// SPDX-License-Identifier: Apache-2.0
//! PostgreSQL / pgvector Production Storage Backend (Task T67)
//!
//! Production-grade backend specification, schema migrations, and parameterized
//! query generators using the pgvector extension for dense vector similarity.

#[derive(Debug, Clone)]
pub struct PgVectorConfig {
    pub connection_url: String,
    pub max_connections: u32,
    pub min_connections: u32,
    pub vector_dimensions: usize,
    pub index_type: PgVectorIndexType,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PgVectorIndexType {
    Hnsw,
    Ivfflat,
}

pub struct PgVectorSchemaBuilder;

impl PgVectorSchemaBuilder {
    /// Generates production DDL statements to install pgvector extension and schemas.
    pub fn build_ddl(dimension: usize, index_type: PgVectorIndexType) -> String {
        let index_clause = match index_type {
            PgVectorIndexType::Hnsw => {
                "CREATE INDEX IF NOT EXISTS idx_mem_embedding_hnsw ON cyrene_memory_records USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);".to_string()
            }
            PgVectorIndexType::Ivfflat => {
                "CREATE INDEX IF NOT EXISTS idx_mem_embedding_ivfflat ON cyrene_memory_records USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);".to_string()
            }
        };

        format!(
            r#"
            CREATE EXTENSION IF NOT EXISTS vector;

            CREATE TABLE IF NOT EXISTS cyrene_memory_records (
                item_id VARCHAR(128) NOT NULL,
                tenant_id VARCHAR(128) NOT NULL,
                scope VARCHAR(128) NOT NULL,
                subject VARCHAR(256) NOT NULL,
                content TEXT NOT NULL,
                embedding vector({dimension}) NOT NULL,
                metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                provenance JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                embedding_profile JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                review_facts JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                created_at_ms BIGINT NOT NULL,
                updated_at_ms BIGINT NOT NULL,
                expires_at_ms BIGINT,
                ttl_seconds BIGINT,
                PRIMARY KEY (tenant_id, item_id)
            );

            CREATE INDEX IF NOT EXISTS idx_mem_tenant_scope ON cyrene_memory_records (tenant_id, scope);
            CREATE INDEX IF NOT EXISTS idx_mem_tenant_expiry ON cyrene_memory_records (tenant_id, expires_at_ms) WHERE expires_at_ms IS NOT NULL;
            {index_clause}
            "#
        )
    }

    /// Builds parameterized query for vector recall using pgvector cosine distance `<=>`.
    pub fn build_recall_sql() -> &'static str {
        r#"
        SELECT item_id, tenant_id, scope, subject, content, embedding::text,
               metadata, created_at_ms, expires_at_ms, ttl_seconds,
               1.0 - (embedding <=> $1::vector) AS score
        FROM cyrene_memory_records
        WHERE tenant_id = $2
          AND (expires_at_ms IS NULL OR expires_at_ms > $3)
          AND ($4::text IS NULL OR scope = $4)
          AND ($5::text IS NULL OR subject = $5)
          AND (1.0 - (embedding <=> $1::vector)) >= $6
        ORDER BY embedding <=> $1::vector ASC
        LIMIT $7;
        "#
    }
}
