//! Stable identifiers for the canonical `agent.runtime.v1` payloads.

pub const CAPABILITY_ID: &str = "agent.runtime.v1";
pub const INTERFACE_VERSION: &str = "1";

pub const METHOD_RUN: &str = "run";
pub const METHOD_RUN_STREAM: &str = "run_stream";

pub const RUN_REQUEST_TYPE_URL: &str = "type.cyrene.io/cyrene.agent.runtime.v1.AgentRunRequest";
pub const RUN_RESPONSE_TYPE_URL: &str = "type.cyrene.io/cyrene.agent.runtime.v1.AgentRunResponse";
pub const STREAM_EVENT_TYPE_URL: &str = "type.cyrene.io/cyrene.agent.runtime.v1.AgentStreamEvent";
