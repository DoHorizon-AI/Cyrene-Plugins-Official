// SPDX-License-Identifier: Apache-2.0
//! ╔══════════════════════════════════════════════════════════════════════╗
//! ║ 📄 File: runtime/rust/cyrene-connector-wecom/src/lib.rs             ║
//! ║ Module: Cyrene Enterprise WeChat (WeCom) Unified Connector in Rust   ║
//! ║ Role: Pure Rust implementation uniting Bot WS, App REST, and CLI.    ║
//! ╚══════════════════════════════════════════════════════════════════════╝

pub mod app;
pub mod bot;
pub mod cli;
pub mod config;
pub mod error;
pub mod service;
pub mod tool_provider;

pub use app::WeComAppClient;
pub use bot::{InboundBotMessage, WeComBotClient};
pub use cli::WeComCliClient;
pub use config::{ConnectorMode, WeComConfig};
pub use error::WeComError;
pub use service::WeComService;
pub use tool_provider::WeComToolProvider;
