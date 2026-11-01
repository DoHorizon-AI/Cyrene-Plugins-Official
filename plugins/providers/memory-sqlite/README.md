# SQLite Memory Provider

This independently installable Python package implements all seven methods of
`memory.provider.v1` over `cyrene.plugin.runtime.v1.DirectPluginRuntime`. It
stores records in a private SQLite file, scopes every read and write by the
request tenant, preserves distinct item/source identities, and keeps embedding
model identity and dimensions with each vector.

Recall applies tenant, embedding profile, expiry, scope, subject, and exact
metadata filters before cosine scoring. It reads rows in bounded batches and
retains only the configured top-k results. Similarity is mapped to the contract
range `[0, 1]` from cosine `[-1, 1]`.

The provider never synthesizes vectors. A store request may carry a real vector
from the host, or the binding may configure a local `model.provider.v1`
DirectPluginRuntime endpoint. Text recall without a vector uses that endpoint.
The model ID and vector dimension must match the binding profile; zero, non-finite,
wrong-dimension, and wrong-model vectors fail closed. Changing the configured
profile keeps old rows tagged with their original profile and excludes them from
new-profile recall until a separate re-embedding process writes replacement
vectors.

## Activation configuration

```json
{
  "database_path": "/var/lib/cyrene/memory/memory.sqlite3",
  "embedding_model_id": "local-embedding-v1",
  "embedding_dimensions": 768,
  "embedding_endpoint": "grpc://127.0.0.1:18720",
  "embedding_timeout_seconds": 30,
  "recall_batch_size": 256,
  "max_top_k": 100
}
```

`database_path`, `embedding_model_id`, and `embedding_dimensions` are required.
The model endpoint is optional only when the host supplies precomputed vectors.
The path must be absolute, the state directory must not be group/world writable,
and the database file is restricted to owner read/write. The endpoint is limited
to a local loopback gRPC address; endpoint and model selection are owned by the
host binding.

Models that require different document and query input forms can set
`embedding_document_prefix` and `embedding_query_prefix`. When either prefix is
non-empty, `embedding_profile_id` is required and must identify that transform
as well as the model. The provider applies the document prefix when embedding
stored text and the query prefix when embedding `query_text`; host-supplied
vectors must already use the same profile.

The plugin uses SQLite WAL mode, `synchronous=FULL`, an idempotent schema
migration, a process lock for connection access, and a transaction for each
import batch. Import is idempotent by `(tenant_id, item_id)` and never merges
records because their text matches. Export uses a bounded keyset cursor. Uninstall
retains the database; deletion and pruning are explicit capability operations.

## Local checks

Run package tests with the repository's memory and model contract SDKs available
on the Python path, then run Ruff on `src/` and `tests/`. The shared SDK packages
are declared in `pyproject.toml`; the package release producer vendors their
source explicitly.
