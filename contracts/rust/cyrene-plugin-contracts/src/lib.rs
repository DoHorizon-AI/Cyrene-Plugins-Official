//! Rust projections for Plugin-owned Cyrene capability payload contracts.
//!
//! Products use these types when they exchange business payloads directly with
//! a Plugin endpoint. Platform control-plane types are intentionally absent.

pub mod agent_runtime;
pub mod computer_runtime;
pub mod direct_plugin_runtime;
pub mod memory_provider;
pub mod message_connector;
pub mod model_provider;

/// Generated `cyrene.plugin.runtime.v1` carrier messages.
pub mod direct_plugin_runtime_v1 {
    include!(concat!(env!("OUT_DIR"), "/cyrene.plugin.runtime.v1.rs"));
}

/// Generated `cyrene.message.connector.v1` payload messages.
pub mod message_connector_v1 {
    include!(concat!(env!("OUT_DIR"), "/cyrene.message.connector.v1.rs"));
}

/// Generated `cyrene.model.provider.v1` payload messages.
pub mod model_provider_v1 {
    include!(concat!(env!("OUT_DIR"), "/cyrene.model.provider.v1.rs"));
}

/// Generated `cyrene.agent.runtime.v1` payload messages.
pub mod agent_runtime_v1 {
    include!(concat!(env!("OUT_DIR"), "/cyrene.agent.runtime.v1.rs"));
}

/// Generated `cyrene.memory.provider.v1` payload messages.
pub mod memory_provider_v1 {
    include!(concat!(env!("OUT_DIR"), "/cyrene.memory.provider.v1.rs"));
}

/// Generated `cyrene.computer.runtime.v1` payload messages.
pub mod computer_runtime_v1 {
    include!(concat!(env!("OUT_DIR"), "/cyrene.computer.runtime.v1.rs"));
}
