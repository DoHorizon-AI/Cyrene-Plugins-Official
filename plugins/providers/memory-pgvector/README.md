# PostgreSQL pgvector Memory Provider

This independently installable Python package implements all seven methods of
`memory.provider.v1` over `cyrene.plugin.runtime.v1.DirectPluginRuntime`. It
uses a real psycopg connection pool, schema migration, PostgreSQL transactions,
JSONB metadata filters, and the pgvector cosine-distance operator. The complete
request tenant, scope, subject, exact metadata, expiry, model profile, and vector
dimension predicates are applied in SQL before results are scored and returned.

The provider keeps source/item IDs distinct even when content is identical.
Imports run in one transaction and use `(tenant_id, item_id)` as the idempotent
identity. Export uses a scope-bound keyset page token. Vectors retain their model
identity and dimension; searches only compare the active configured profile.
Each database is bound to a fingerprint of the provider model, stable vector
profile, dimension, and document/query preprocessing. If records exist, changing
any part of that fingerprint fails closed at startup. Rebuild into a fresh
database, re-embed and verify the complete new index, then switch the host
binding; keep the previous database for rollback. Keyset pages are bounded but
are not point-in-time snapshots, so pause writers during a backup/export that
requires exact count reconciliation.

If a model uses different document and query input forms, configure the two
prefixes and a distinct `embedding_profile_id`; the actual provider model ID is
still sent through `model.provider.v1` and checked against its response. The
plugin uses the document prefix for stored text and the query prefix for
`query_text`. Host-supplied vectors must already match that profile. Host-supplied
vectors and imported records must include the metadata keys
`cyrene.embedding.model_id`, `cyrene.embedding.provider_model_id`, and
`cyrene.embedding.dimensions`.

## Activation configuration

```json
{
  "database_dsn_ref": "memory-pgvector/database-dsn",
  "embedding_model_id": "local-embedding-v1",
  "embedding_profile_id": "local-embedding-v1-query-passage-v1",
  "embedding_document_prefix": "passage: ",
  "embedding_query_prefix": "query: ",
  "embedding_dimensions": 768,
  "embedding_endpoint": "grpc://127.0.0.1:18720",
  "pool_min_size": 1,
  "pool_max_size": 8,
  "connection_timeout_seconds": 10,
  "statement_timeout_seconds": 30,
  "hostSecretRefs": {
    "memory-pgvector/database-dsn": "CYRENE_MEMORY_PGVECTOR_DSN"
  }
}
```

The DSN is supplied only through the host's stable secret reference and an
environment variable named in `hostSecretRefs`; the secret itself is never part
of activation JSON, errors, or logs. The host should place PostgreSQL on the
acceptance Compose project's private network and keep it off published host
ports. pgvector is installed and schema migration is performed before the pool
is created. Failure to install the extension, connect, migrate, or configure the
pool prevents memory operations from succeeding.

The provider uses exact cosine search over rows that pass tenant/profile/scope/
subject/metadata/expiry predicates. It does not create an approximate HNSW/IVFFlat
index, which could underfill filtered result sets. The deployed pgvector vector
type supports vectors up to 16,000 dimensions; this package follows the
`memory.provider.v1` 4,096-dimension contract limit. It creates no approximate
vector index, so index-specific lower dimension limits do not apply.

## Local checks

Run package tests with the Plugins memory/model contract SDKs on the Python path.
Set `CYRENE_TEST_POSTGRES_DSN` to an isolated PostgreSQL database with permission
to install `vector` to run the database integration tests; this package never
uses the repository's legacy volumes or any cloud database.
