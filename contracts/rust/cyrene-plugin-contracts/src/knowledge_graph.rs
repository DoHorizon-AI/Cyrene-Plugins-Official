//! Stable identifiers for the canonical `knowledge.graph.v1` payloads.

pub const CAPABILITY_ID: &str = "knowledge.graph.v1";
pub const INTERFACE_VERSION: &str = "1";

pub const METHOD_UPSERT_NODES: &str = "upsert_nodes";
pub const METHOD_GET_NODE: &str = "get_node";
pub const METHOD_SEARCH_NODES: &str = "search_nodes";
pub const METHOD_DELETE_NODE: &str = "delete_node";
pub const METHOD_UPSERT_EDGES: &str = "upsert_edges";
pub const METHOD_GET_EDGE: &str = "get_edge";
pub const METHOD_SEARCH_EDGES: &str = "search_edges";
pub const METHOD_DELETE_EDGE: &str = "delete_edge";
pub const METHOD_EXPAND: &str = "expand";
pub const METHOD_IMPORT_BATCH: &str = "import_batch";
pub const METHOD_EXPORT_BATCH: &str = "export_batch";

pub const UPSERT_NODES_REQUEST_TYPE_URL: &str =
    "type.cyrene.io/cyrene.knowledge.graph.v1.UpsertNodesRequest";
pub const UPSERT_NODES_RESPONSE_TYPE_URL: &str =
    "type.cyrene.io/cyrene.knowledge.graph.v1.UpsertNodesResponse";
pub const GET_NODE_REQUEST_TYPE_URL: &str =
    "type.cyrene.io/cyrene.knowledge.graph.v1.GetNodeRequest";
pub const GET_NODE_RESPONSE_TYPE_URL: &str =
    "type.cyrene.io/cyrene.knowledge.graph.v1.GetNodeResponse";
pub const SEARCH_NODES_REQUEST_TYPE_URL: &str =
    "type.cyrene.io/cyrene.knowledge.graph.v1.SearchNodesRequest";
pub const SEARCH_NODES_RESPONSE_TYPE_URL: &str =
    "type.cyrene.io/cyrene.knowledge.graph.v1.SearchNodesResponse";
pub const DELETE_NODE_REQUEST_TYPE_URL: &str =
    "type.cyrene.io/cyrene.knowledge.graph.v1.DeleteNodeRequest";
pub const DELETE_NODE_RESPONSE_TYPE_URL: &str =
    "type.cyrene.io/cyrene.knowledge.graph.v1.DeleteNodeResponse";
pub const UPSERT_EDGES_REQUEST_TYPE_URL: &str =
    "type.cyrene.io/cyrene.knowledge.graph.v1.UpsertEdgesRequest";
pub const UPSERT_EDGES_RESPONSE_TYPE_URL: &str =
    "type.cyrene.io/cyrene.knowledge.graph.v1.UpsertEdgesResponse";
pub const GET_EDGE_REQUEST_TYPE_URL: &str =
    "type.cyrene.io/cyrene.knowledge.graph.v1.GetEdgeRequest";
pub const GET_EDGE_RESPONSE_TYPE_URL: &str =
    "type.cyrene.io/cyrene.knowledge.graph.v1.GetEdgeResponse";
pub const SEARCH_EDGES_REQUEST_TYPE_URL: &str =
    "type.cyrene.io/cyrene.knowledge.graph.v1.SearchEdgesRequest";
pub const SEARCH_EDGES_RESPONSE_TYPE_URL: &str =
    "type.cyrene.io/cyrene.knowledge.graph.v1.SearchEdgesResponse";
pub const DELETE_EDGE_REQUEST_TYPE_URL: &str =
    "type.cyrene.io/cyrene.knowledge.graph.v1.DeleteEdgeRequest";
pub const DELETE_EDGE_RESPONSE_TYPE_URL: &str =
    "type.cyrene.io/cyrene.knowledge.graph.v1.DeleteEdgeResponse";
pub const EXPAND_REQUEST_TYPE_URL: &str =
    "type.cyrene.io/cyrene.knowledge.graph.v1.ExpandGraphRequest";
pub const EXPAND_RESPONSE_TYPE_URL: &str =
    "type.cyrene.io/cyrene.knowledge.graph.v1.ExpandGraphResponse";
pub const IMPORT_BATCH_REQUEST_TYPE_URL: &str =
    "type.cyrene.io/cyrene.knowledge.graph.v1.ImportGraphRequest";
pub const IMPORT_BATCH_RESPONSE_TYPE_URL: &str =
    "type.cyrene.io/cyrene.knowledge.graph.v1.ImportGraphResponse";
pub const EXPORT_BATCH_REQUEST_TYPE_URL: &str =
    "type.cyrene.io/cyrene.knowledge.graph.v1.ExportGraphRequest";
pub const EXPORT_BATCH_RESPONSE_TYPE_URL: &str =
    "type.cyrene.io/cyrene.knowledge.graph.v1.ExportGraphResponse";
