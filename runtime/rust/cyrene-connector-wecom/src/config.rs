// SPDX-License-Identifier: Apache-2.0
//! Configuration types and validation for the WeCom connector.

use serde::{Deserialize, Serialize};
use std::env;

use crate::error::WeComError;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "lowercase")]
pub enum ConnectorMode {
    #[default]
    App,
    Bot,
    Hybrid,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WeComConfig {
    #[serde(default)]
    pub mode: ConnectorMode,

    // App mode fields
    pub corp_id: Option<String>,
    pub corp_secret: Option<String>,
    pub agent_id: Option<i64>,
    #[serde(default = "default_base_url")]
    pub base_url: String,

    // Bot mode fields
    pub bot_key: Option<String>,
    pub bot_secret: Option<String>,
    #[serde(default = "default_ws_url")]
    pub ws_url: String,

    // CLI tools fields
    pub cli_path: Option<String>,
    #[serde(default = "default_true")]
    pub enable_tools: bool,
}

fn default_base_url() -> String {
    "https://qyapi.weixin.qq.com".to_string()
}

fn default_ws_url() -> String {
    "wss://openws.work.weixin.qq.com".to_string()
}

fn default_true() -> bool {
    true
}

impl Default for WeComConfig {
    fn default() -> Self {
        Self {
            mode: ConnectorMode::App,
            corp_id: None,
            corp_secret: None,
            agent_id: None,
            base_url: default_base_url(),
            bot_key: None,
            bot_secret: None,
            ws_url: default_ws_url(),
            cli_path: None,
            enable_tools: true,
        }
    }
}

impl WeComConfig {
    pub fn validate(&self) -> Result<(), WeComError> {
        match self.mode {
            ConnectorMode::App => {
                if self.corp_id.is_none() || self.corp_secret.is_none() || self.agent_id.is_none() {
                    return Err(WeComError::Config(
                        "App mode requires corp_id, corp_secret, and agent_id".to_string(),
                    ));
                }
            }
            ConnectorMode::Bot => {
                if self.bot_key.is_none() || self.bot_secret.is_none() {
                    return Err(WeComError::Config(
                        "Bot mode requires bot_key and bot_secret".to_string(),
                    ));
                }
            }
            ConnectorMode::Hybrid => {
                if self.corp_id.is_none() || self.corp_secret.is_none() || self.agent_id.is_none() {
                    return Err(WeComError::Config(
                        "Hybrid mode requires corp_id, corp_secret, and agent_id for app messaging"
                            .to_string(),
                    ));
                }
                if self.bot_key.is_none() || self.bot_secret.is_none() {
                    return Err(WeComError::Config(
                        "Hybrid mode requires bot_key and bot_secret for bot WebSocket connection"
                            .to_string(),
                    ));
                }
            }
        }
        Ok(())
    }

    /// Read configuration from standard Cyrene environment or fallback env vars.
    pub fn from_env() -> Result<Self, WeComError> {
        if let Ok(config_json) = env::var("CYRENE_CAPABILITY_CONFIGURATION_JSON") {
            if !config_json.trim().is_empty() {
                let cfg: WeComConfig = serde_json::from_str(&config_json)?;
                cfg.validate()?;
                return Ok(cfg);
            }
        }

        // Fallback to individual env vars
        let mut cfg = WeComConfig::default();
        if let Ok(mode_str) = env::var("WECOM_MODE") {
            cfg.mode = match mode_str.to_lowercase().as_str() {
                "bot" => ConnectorMode::Bot,
                "hybrid" => ConnectorMode::Hybrid,
                _ => ConnectorMode::App,
            };
        }
        cfg.corp_id = env::var("WECOM_CORP_ID").ok();
        cfg.corp_secret = env::var("WECOM_CORP_SECRET").ok();
        cfg.agent_id = env::var("WECOM_AGENT_ID").ok().and_then(|s| s.parse().ok());
        cfg.bot_key = env::var("WECOM_BOT_KEY").ok();
        cfg.bot_secret = env::var("WECOM_BOT_SECRET").ok();
        if let Ok(url) = env::var("WECOM_BASE_URL") {
            cfg.base_url = url;
        }
        if let Ok(ws) = env::var("WECOM_WS_URL") {
            cfg.ws_url = ws;
        }
        cfg.cli_path = env::var("WECOM_CLI_PATH").ok();

        cfg.validate()?;
        Ok(cfg)
    }
}
