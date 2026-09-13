//! Stable identifiers for the canonical `computer.runtime.v1` payloads.

pub const CAPABILITY_ID: &str = "computer.runtime.v1";
pub const INTERFACE_VERSION: &str = "1";

pub const METHOD_EXECUTE_COMMAND: &str = "execute_command";
pub const METHOD_EXECUTE_COMMAND_STREAM: &str = "execute_command_stream";
pub const METHOD_READ_FILE: &str = "read_file";
pub const METHOD_WRITE_FILE: &str = "write_file";
pub const METHOD_LIST_DIR: &str = "list_dir";
pub const METHOD_CREATE_ARTIFACT: &str = "create_artifact";
pub const METHOD_GET_ARTIFACT: &str = "get_artifact";

pub const EXECUTE_COMMAND_REQUEST_TYPE_URL: &str =
    "type.cyrene.io/cyrene.computer.runtime.v1.CommandExecutionRequest";
pub const EXECUTE_COMMAND_RESPONSE_TYPE_URL: &str =
    "type.cyrene.io/cyrene.computer.runtime.v1.CommandExecutionResponse";
pub const COMMAND_STREAM_EVENT_TYPE_URL: &str =
    "type.cyrene.io/cyrene.computer.runtime.v1.CommandStreamEvent";
