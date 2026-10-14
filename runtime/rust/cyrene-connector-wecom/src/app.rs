// SPDX-License-Identifier: Apache-2.0
//! Enterprise WeChat Application (Corp App) REST API client.

use reqwest::Client;
use serde::{Deserialize, Serialize};
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::sync::RwLock;
use tracing::{debug, info};

use crate::error::WeComError;

#[derive(Debug, Clone)]
pub struct WeComAppClient {
    client: Client,
    corp_id: String,
    corp_secret: String,
    agent_id: i64,
    base_url: String,
    token_cache: Arc<RwLock<Option<CachedToken>>>,
}

#[derive(Debug, Clone)]
struct CachedToken {
    token: String,
    expires_at: Instant,
}

#[derive(Deserialize)]
struct TokenResponse {
    errcode: i64,
    errmsg: String,
    access_token: Option<String>,
    expires_in: Option<u64>,
}

#[derive(Serialize)]
struct SendMessagePayload<'a> {
    #[serde(skip_serializing_if = "Option::is_none")]
    touser: Option<&'a str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    toparty: Option<&'a str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    totag: Option<&'a str>,
    msgtype: &'a str,
    agentid: i64,
    #[serde(skip_serializing_if = "Option::is_none")]
    text: Option<TextMessage<'a>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    markdown: Option<MarkdownMessage<'a>>,
}

#[derive(Serialize)]
struct TextMessage<'a> {
    content: &'a str,
}

#[derive(Serialize)]
struct MarkdownMessage<'a> {
    content: &'a str,
}

#[derive(Deserialize)]
struct ApiResponse {
    errcode: i64,
    errmsg: String,
    msgid: Option<String>,
}

impl WeComAppClient {
    pub fn new(corp_id: String, corp_secret: String, agent_id: i64, base_url: String) -> Self {
        Self {
            client: Client::builder()
                .timeout(Duration::from_secs(10))
                .build()
                .unwrap_or_default(),
            corp_id,
            corp_secret,
            agent_id,
            base_url: base_url.trim_end_matches('/').to_string(),
            token_cache: Arc::new(RwLock::new(None)),
        }
    }

    /// Retrieve or refresh the application AccessToken.
    pub async fn get_access_token(&self) -> Result<String, WeComError> {
        {
            let cache = self.token_cache.read().await;
            if let Some(ref entry) = *cache {
                if Instant::now() < entry.expires_at {
                    return Ok(entry.token.clone());
                }
            }
        }

        let mut cache = self.token_cache.write().await;
        // Double-check after acquiring write lock
        if let Some(ref entry) = *cache {
            if Instant::now() < entry.expires_at {
                return Ok(entry.token.clone());
            }
        }

        debug!("Fetching fresh AccessToken for corp_id={}", self.corp_id);
        let url = format!(
            "{}/cgi-bin/gettoken?corpid={}&corpsecret={}",
            self.base_url, self.corp_id, self.corp_secret
        );

        let res = self.client.get(&url).send().await?;
        let data: TokenResponse = res.json().await?;

        if data.errcode != 0 {
            return Err(WeComError::Api {
                code: data.errcode,
                message: data.errmsg,
            });
        }

        let token = data.access_token.ok_or_else(|| WeComError::Api {
            code: -1,
            message: "Missing access_token in WeCom response".to_string(),
        })?;

        let ttl = data.expires_in.unwrap_or(7200).saturating_sub(60);
        let expires_at = Instant::now() + Duration::from_secs(ttl);

        *cache = Some(CachedToken {
            token: token.clone(),
            expires_at,
        });

        info!(
            "Successfully refreshed WeCom AccessToken (valid for {}s)",
            ttl
        );
        Ok(token)
    }

    /// Send text or markdown message via standard WeCom enterprise message API.
    pub async fn send_text_message(
        &self,
        to_user: Option<&str>,
        to_party: Option<&str>,
        to_tag: Option<&str>,
        content: &str,
        is_markdown: bool,
    ) -> Result<String, WeComError> {
        let token = self.get_access_token().await?;
        let url = format!(
            "{}/cgi-bin/message/send?access_token={}",
            self.base_url, token
        );

        let payload = if is_markdown {
            SendMessagePayload {
                touser: to_user,
                toparty: to_party,
                totag: to_tag,
                msgtype: "markdown",
                agentid: self.agent_id,
                text: None,
                markdown: Some(MarkdownMessage { content }),
            }
        } else {
            SendMessagePayload {
                touser: to_user,
                toparty: to_party,
                totag: to_tag,
                msgtype: "text",
                agentid: self.agent_id,
                text: Some(TextMessage { content }),
                markdown: None,
            }
        };

        let res = self.client.post(&url).json(&payload).send().await?;
        let data: ApiResponse = res.json().await?;

        if data.errcode != 0 {
            return Err(WeComError::Api {
                code: data.errcode,
                message: data.errmsg,
            });
        }

        Ok(data.msgid.unwrap_or_else(|| "OK".to_string()))
    }
}
