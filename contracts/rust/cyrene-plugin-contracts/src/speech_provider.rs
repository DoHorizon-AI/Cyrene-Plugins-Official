//! Stable identifiers for the canonical `speech.provider.v1` payloads.

pub const CAPABILITY_ID: &str = "speech.provider.v1";
pub const INTERFACE_VERSION: &str = "1";
pub const METHOD_TRANSCRIBE: &str = "transcribe";
pub const TRANSCRIBE_REQUEST_TYPE_URL: &str =
    "type.cyrene.io/cyrene.speech.provider.v1.TranscribeRequest";
pub const TRANSCRIBE_RESPONSE_TYPE_URL: &str =
    "type.cyrene.io/cyrene.speech.provider.v1.TranscribeResponse";
