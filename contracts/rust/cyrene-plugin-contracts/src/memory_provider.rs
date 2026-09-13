//! Stable identifiers for the canonical `memory.provider.v1` payloads.

pub const CAPABILITY_ID: &str = "memory.provider.v1";
pub const INTERFACE_VERSION: &str = "1";

pub const METHOD_STORE: &str = "store";
pub const METHOD_GET: &str = "get";
pub const METHOD_RECALL: &str = "recall";
pub const METHOD_DELETE: &str = "delete";
pub const METHOD_PRUNE: &str = "prune";
pub const METHOD_EXPORT_BATCH: &str = "export_batch";
pub const METHOD_IMPORT_BATCH: &str = "import_batch";

pub const STORE_REQUEST_TYPE_URL: &str =
    "type.cyrene.io/cyrene.memory.provider.v1.StoreMemoryRequest";
pub const STORE_RESPONSE_TYPE_URL: &str =
    "type.cyrene.io/cyrene.memory.provider.v1.StoreMemoryResponse";
pub const RECALL_REQUEST_TYPE_URL: &str =
    "type.cyrene.io/cyrene.memory.provider.v1.RecallMemoryRequest";
pub const RECALL_RESPONSE_TYPE_URL: &str =
    "type.cyrene.io/cyrene.memory.provider.v1.RecallMemoryResponse";
