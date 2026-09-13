//! Stable identifiers for the generic `DirectPluginRuntime` protocol.

pub const CAPABILITY_ID: &str = "cyrene.plugin.runtime.direct.v1";
pub const INTERFACE_VERSION: &str = "1";

pub const METHOD_INVOKE: &str = "Invoke";
pub const METHOD_INVOKE_STREAM: &str = "InvokeStream";
pub const METHOD_HEALTH: &str = "Health";

pub const HEALTH_REQUEST_TYPE_URL: &str = "type.cyrene.io/cyrene.plugin.runtime.v1.HealthRequest";
pub const HEALTH_RESPONSE_TYPE_URL: &str = "type.cyrene.io/cyrene.plugin.runtime.v1.HealthResponse";
