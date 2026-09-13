//! Stable identifiers for the canonical `model.provider.v1` payloads.
//!
//! Interface version 1 remains the compatible text/embedding surface; the
//! additive `chat_completion_v2` method carries structured tool metadata.

/// Canonical capability identifier used by direct Plugin bindings.
pub const CAPABILITY_ID: &str = "model.provider.v1";
/// Canonical capability interface version.
pub const INTERFACE_VERSION: &str = "1";
/// Canonical embedding method exposed by a Plugin binding.
pub const EMBEDDINGS_METHOD: &str = "embeddings";
/// Canonical chat-completion method exposed by a Plugin binding.
pub const CHAT_COMPLETION_METHOD: &str = "chat_completion";
/// Structured chat-completion method exposed by a Plugin binding.
pub const CHAT_COMPLETION_V2_METHOD: &str = "chat_completion_v2";
/// Canonical structured-chat interface version.
pub const CHAT_COMPLETION_V2_INTERFACE_VERSION: &str = "2";

/// Standard protobuf `Any` type URL for an embedding request.
pub const EMBEDDINGS_REQUEST_TYPE_URL: &str =
    "type.cyrene.io/cyrene.model.provider.v1.EmbeddingsRequest";
/// Standard protobuf `Any` type URL for an embedding result.
pub const EMBEDDINGS_RESPONSE_TYPE_URL: &str =
    "type.cyrene.io/cyrene.model.provider.v1.EmbeddingsResponse";
/// Standard protobuf `Any` type URL for a chat-completion request.
pub const CHAT_COMPLETION_REQUEST_TYPE_URL: &str =
    "type.cyrene.io/cyrene.model.provider.v1.ChatCompletionRequest";
/// Standard protobuf `Any` type URL for a chat-completion result.
pub const CHAT_COMPLETION_RESPONSE_TYPE_URL: &str =
    "type.cyrene.io/cyrene.model.provider.v1.ChatCompletionResponse";
