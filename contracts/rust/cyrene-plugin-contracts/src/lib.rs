//! ┌─────────────────────────────────────────────────────────────────────┐
//! │  📄 lib.rs                                                          │
//! │  Package: cyrene_plugin_contracts                                  │
//! │  Role: Generated Rust projections for Cyrene Plugin & Authority RPC.│
//! │                                                                     │
//! │  模块职责：Cyrene 插件能力载荷与解耦 Workspace Authority RPC 协议。   │
//! └─────────────────────────────────────────────────────────────────────┘
//!
//! Products use these types when they exchange business payloads directly with
//! a Plugin endpoint. Platform control-plane types are intentionally absent.

#![allow(clippy::large_enum_variant)]

pub mod agent_runtime;
pub mod computer_runtime;
pub mod direct_plugin_runtime;
pub mod memory_provider;
pub mod message_connector;
pub mod model_provider;
pub mod tool_provider;

pub mod google {
    pub mod rpc {
        tonic::include_proto!("google.rpc");
    }
}

pub mod cyrene {
    pub mod semantic {
        pub mod v1 {
            tonic::include_proto!("cyrene.semantic.v1");
        }
    }
    pub mod plugin {
        pub mod runtime {
            // Tonic-generated RPC methods return `tonic::Status`; keep this lint exception
            // scoped to the generated runtime v1 module.
            // 中文：tonic 生成的 RPC 使用 `tonic::Status`，此例外仅作用于 runtime v1。
            #[allow(clippy::result_large_err)]
            pub mod v1 {
                tonic::include_proto!("cyrene.plugin.runtime.v1");
            }
        }
    }
    pub mod message {
        pub mod connector {
            pub mod v1 {
                tonic::include_proto!("cyrene.message.connector.v1");
            }
        }
    }
    pub mod model {
        pub mod provider {
            pub mod v1 {
                tonic::include_proto!("cyrene.model.provider.v1");
            }
        }
    }
    pub mod agent {
        pub mod runtime {
            pub mod v1 {
                tonic::include_proto!("cyrene.agent.runtime.v1");
            }
        }
    }
    pub mod memory {
        pub mod provider {
            pub mod v1 {
                tonic::include_proto!("cyrene.memory.provider.v1");
            }
        }
    }
    pub mod computer {
        pub mod runtime {
            pub mod v1 {
                tonic::include_proto!("cyrene.computer.runtime.v1");
            }
        }
    }
    pub mod tool {
        pub mod provider {
            pub mod v1 {
                tonic::include_proto!("cyrene.tool.provider.v1");
            }
        }
    }
    pub mod workspace {
        pub mod v1 {
            tonic::include_proto!("cyrene.workspace.v1");
        }
        pub mod product {
            pub mod v2 {
                tonic::include_proto!("cyrene.workspace.product.v2");
            }
        }
        pub mod authority {
            #[allow(clippy::result_large_err)]
            pub mod v1 {
                tonic::include_proto!("cyrene.workspace.authority.v1");
            }
            #[allow(clippy::result_large_err)]
            pub mod v2 {
                tonic::include_proto!("cyrene.workspace.authority.v2");
            }
        }
        pub mod bridge {
            #[allow(clippy::result_large_err)]
            pub mod v1 {
                tonic::include_proto!("cyrene.workspace.bridge.v1");
            }
        }
        pub mod local {
            #[allow(clippy::result_large_err)]
            pub mod v1 {
                tonic::include_proto!("cyrene.workspace.local.v1");
            }
            #[allow(clippy::result_large_err)]
            pub mod v2 {
                tonic::include_proto!("cyrene.workspace.local.v2");
            }
        }
        pub mod relay {
            #[allow(clippy::result_large_err)]
            pub mod v1 {
                tonic::include_proto!("cyrene.workspace.relay.v1");
            }
        }
        pub mod tunnel {
            #[allow(clippy::result_large_err)]
            pub mod v1 {
                tonic::include_proto!("cyrene.workspace.tunnel.v1");
            }
        }
    }
}

// Convenient top-level re-exports preserving existing consumers:
pub use cyrene::agent::runtime::v1 as agent_runtime_v1;
pub use cyrene::computer::runtime::v1 as computer_runtime_v1;
pub use cyrene::memory::provider::v1 as memory_provider_v1;
pub use cyrene::message::connector::v1 as message_connector_v1;
pub use cyrene::model::provider::v1 as model_provider_v1;
pub use cyrene::plugin::runtime::v1 as direct_plugin_runtime_v1;
pub use cyrene::tool::provider::v1 as tool_provider_v1;
pub use cyrene::workspace::authority::v1 as workspace_authority_v1;
pub use cyrene::workspace::authority::v2 as workspace_authority_v2;
pub use cyrene::workspace::bridge::v1 as workspace_bridge_v1;
pub use cyrene::workspace::local::v1 as workspace_local_v1;
pub use cyrene::workspace::local::v2 as workspace_local_v2;
pub use cyrene::workspace::product::v2 as workspace_product_v2;
pub use cyrene::workspace::relay::v1 as workspace_relay_v1;
/// Bounded end-to-end Authority byte tunnel messages and service.
pub use cyrene::workspace::tunnel::v1 as workspace_tunnel_v1;
pub use cyrene::workspace::v1 as workspace_v1;
pub use google::rpc as google_rpc;
