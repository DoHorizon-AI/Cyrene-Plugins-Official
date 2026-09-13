// ╔══════════════════════════════════════════════════════════════════════╗
// ║ 📄 File: contracts/rust/cy-proto/src/message_connector.rs
// ║ Module: CYRENE Plugins Official
// ║ Role: Stable identifiers and bounds for the connector contract.
// ║
// ║ 模块：CYRENE Plugins Official
// ║ 职责：连接器契约的稳定标识与边界。
// ╚══════════════════════════════════════════════════════════════════════╝
//! Stable identifiers and normative bounds for the canonical
//! `message.connector.v1` capability payload contract.

/// Canonical capability identifier used by direct Plugin bindings.
pub const CAPABILITY_ID: &str = "message.connector.v1";
/// Canonical capability interface version.
pub const INTERFACE_VERSION: &str = "1";
/// Canonical outbound method exposed by a Plugin binding.
pub const SEND_MESSAGE_METHOD: &str = "send_message";
/// Canonical inbound event type exposed by a Plugin binding.
pub const INBOUND_MESSAGE_EVENT_TYPE: &str = "inbound_message";

/// Standard protobuf `Any` type URL for an inbound message payload.
pub const INBOUND_MESSAGE_TYPE_URL: &str =
    "type.cyrene.io/cyrene.message.connector.v1.InboundMessagePayload";
/// Standard protobuf `Any` type URL for an outbound send request.
pub const SEND_MESSAGE_REQUEST_TYPE_URL: &str =
    "type.cyrene.io/cyrene.message.connector.v1.SendMessageRequest";
/// Standard protobuf `Any` type URL for a connector delivery result.
pub const DELIVERY_RESULT_TYPE_URL: &str =
    "type.cyrene.io/cyrene.message.connector.v1.DeliveryResult";

/// Maximum UTF-8 byte length of a vendor identifier on an extension.
pub const MAX_VENDOR_ID_BYTES: usize = 64;
/// Maximum number of facts in one `VendorExtension`.
pub const MAX_VENDOR_FACTS: usize = 32;
/// Maximum UTF-8 byte length of a vendor fact name.
pub const MAX_VENDOR_FACT_NAME_BYTES: usize = 64;
/// Maximum UTF-8 byte length of one vendor fact value.
pub const MAX_VENDOR_FACT_VALUE_BYTES: usize = 2_048;
/// Maximum aggregate UTF-8 name/value bytes in one extension.
pub const MAX_VENDOR_FACT_TOTAL_BYTES: usize = 8_192;
