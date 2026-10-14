// SPDX-License-Identifier: Apache-2.0
//! Error types for the unified Cyrene WeCom Connector.

use thiserror::Error;
use tonic::Status;

#[derive(Debug, Error)]
pub enum WeComError {
    #[error("Configuration error: {0}")]
    Config(String),

    #[error("Authentication error: {0}")]
    Auth(String),

    #[error("REST API error: code={code}, msg={message}")]
    Api { code: i64, message: String },

    #[error("WebSocket connection error: {0}")]
    WebSocket(String),

    #[error("CLI execution error: {0}")]
    Cli(String),

    #[error("Serialization error: {0}")]
    Serialization(String),

    #[error("Invalid request: {0}")]
    InvalidRequest(String),

    #[error("Internal connector error: {0}")]
    Internal(String),
}

impl From<serde_json::Error> for WeComError {
    fn from(err: serde_json::Error) -> Self {
        WeComError::Serialization(err.to_string())
    }
}

impl From<reqwest::Error> for WeComError {
    fn from(err: reqwest::Error) -> Self {
        WeComError::Api {
            code: -1,
            message: err.to_string(),
        }
    }
}

impl From<WeComError> for Status {
    fn from(err: WeComError) -> Self {
        match err {
            WeComError::Config(msg) | WeComError::InvalidRequest(msg) => {
                Status::invalid_argument(msg)
            }
            WeComError::Auth(msg) => Status::unauthenticated(msg),
            WeComError::Api { code, message } => {
                Status::unavailable(format!("WeCom API error ({code}): {message}"))
            }
            WeComError::WebSocket(msg) => Status::unavailable(format!("WebSocket error: {msg}")),
            WeComError::Cli(msg) => Status::internal(format!("CLI error: {msg}")),
            WeComError::Serialization(msg) => Status::invalid_argument(msg),
            WeComError::Internal(msg) => Status::internal(msg),
        }
    }
}
