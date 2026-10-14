// SPDX-License-Identifier: Apache-2.0
//! Enterprise WeChat Official WebSocket OpenWS Bot Client in Rust.

use futures::{SinkExt, StreamExt};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::{mpsc, RwLock};
use tokio::time::{interval, sleep};
use tokio_tungstenite::connect_async;
use tokio_tungstenite::tungstenite::protocol::Message as WsMessage;
use tracing::{debug, error, info, warn};

use crate::error::WeComError;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct InboundBotMessage {
    pub msg_id: String,
    pub chat_id: String,
    pub chat_type: String, // "single" | "group"
    pub sender_id: String,
    pub sender_name: Option<String>,
    pub msg_type: String,
    pub content: String,
    pub raw: Value,
}

#[derive(Debug, Clone)]
pub struct WeComBotClient {
    bot_key: String,
    bot_secret: String,
    ws_url: String,
    connected: Arc<AtomicBool>,
    outbound_tx: Arc<RwLock<Option<mpsc::Sender<WsMessage>>>>,
    inbound_tx: Arc<RwLock<Option<mpsc::Sender<InboundBotMessage>>>>,
}

#[derive(Serialize)]
struct WsEnvelope<T> {
    cmd: &'static str,
    headers: WsHeaders,
    body: T,
}

#[derive(Serialize)]
struct WsHeaders {
    req_id: String,
}

#[derive(Serialize)]
struct SubscribeBody<'a> {
    bot_key: &'a str,
    bot_secret: &'a str,
}

#[derive(Serialize)]
struct RespondBody<'a> {
    msg_id: &'a str,
    msg_type: &'a str,
    finish: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    text: Option<TextContent<'a>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    markdown: Option<MarkdownContent<'a>>,
}

#[derive(Serialize)]
struct SendBody<'a> {
    chat_id: &'a str,
    msg_type: &'a str,
    #[serde(skip_serializing_if = "Option::is_none")]
    text: Option<TextContent<'a>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    markdown: Option<MarkdownContent<'a>>,
}

#[derive(Serialize)]
struct TextContent<'a> {
    content: &'a str,
}

#[derive(Serialize)]
struct MarkdownContent<'a> {
    content: &'a str,
}

impl WeComBotClient {
    pub fn new(bot_key: String, bot_secret: String, ws_url: String) -> Self {
        Self {
            bot_key,
            bot_secret,
            ws_url,
            connected: Arc::new(AtomicBool::new(false)),
            outbound_tx: Arc::new(RwLock::new(None)),
            inbound_tx: Arc::new(RwLock::new(None)),
        }
    }

    pub fn is_connected(&self) -> bool {
        self.connected.load(Ordering::SeqCst)
    }

    /// Set receiver channel for incoming bot messages.
    pub async fn set_inbound_receiver(&self, tx: mpsc::Sender<InboundBotMessage>) {
        let mut lock = self.inbound_tx.write().await;
        *lock = Some(tx);
    }

    /// Start background connection supervisor with automatic exponential backoff.
    pub fn spawn_connection_supervisor(self: Arc<Self>) -> tokio::task::JoinHandle<()> {
        tokio::spawn(async move {
            let mut backoff = Duration::from_millis(500);
            let max_backoff = Duration::from_secs(30);

            loop {
                info!("Connecting to WeCom OpenWS gateway at {}...", self.ws_url);
                match self.connect_and_run().await {
                    Ok(_) => {
                        info!("WeCom OpenWS session ended gracefully");
                        backoff = Duration::from_millis(500);
                    }
                    Err(err) => {
                        error!(
                            "WeCom OpenWS connection failed: {}. Retrying in {:?}",
                            err, backoff
                        );
                        self.connected.store(false, Ordering::SeqCst);
                        sleep(backoff).await;
                        backoff = (backoff * 2).min(max_backoff);
                    }
                }
            }
        })
    }

    async fn connect_and_run(&self) -> Result<(), WeComError> {
        let (ws_stream, response) = connect_async(&self.ws_url)
            .await
            .map_err(|e| WeComError::WebSocket(format!("Connect error: {e}")))?;

        info!(
            "Connected to WeCom WebSocket, HTTP status: {}",
            response.status()
        );

        let (mut write_half, mut read_half) = ws_stream.split();
        let (tx, mut rx) = mpsc::channel::<WsMessage>(64);

        {
            let mut lock = self.outbound_tx.write().await;
            *lock = Some(tx.clone());
        }

        // 1. Handshake: aibot_subscribe
        let subscribe_req = WsEnvelope {
            cmd: "aibot_subscribe",
            headers: WsHeaders {
                req_id: format!(
                    "sub-{}",
                    std::time::SystemTime::now()
                        .elapsed()
                        .unwrap_or_default()
                        .as_millis()
                ),
            },
            body: SubscribeBody {
                bot_key: &self.bot_key,
                bot_secret: &self.bot_secret,
            },
        };
        let sub_json = serde_json::to_string(&subscribe_req)?;
        write_half
            .send(WsMessage::Text(sub_json))
            .await
            .map_err(|e| WeComError::WebSocket(format!("Send subscribe error: {e}")))?;

        self.connected.store(true, Ordering::SeqCst);

        // 2. Spawn writer loop
        let writer_task = tokio::spawn(async move {
            while let Some(msg) = rx.recv().await {
                if let Err(e) = write_half.send(msg).await {
                    error!("Error sending WebSocket frame: {e}");
                    break;
                }
            }
        });

        // 3. Heartbeat loop (Ping every 30s)
        let ping_tx = tx.clone();
        let ping_task = tokio::spawn(async move {
            let mut ticker = interval(Duration::from_secs(30));
            loop {
                ticker.tick().await;
                if ping_tx.send(WsMessage::Ping(vec![])).await.is_err() {
                    break;
                }
                debug!("WeCom WebSocket ping sent");
            }
        });

        // 4. Inbound read loop
        while let Some(msg_result) = read_half.next().await {
            match msg_result {
                Ok(WsMessage::Text(text)) => {
                    self.handle_incoming_text(&text).await?;
                }
                Ok(WsMessage::Ping(data)) => {
                    let _ = tx.send(WsMessage::Pong(data)).await;
                }
                Ok(WsMessage::Pong(_)) => {
                    debug!("WeCom WebSocket pong received");
                }
                Ok(WsMessage::Close(frame)) => {
                    warn!("WeCom WebSocket close frame received: {:?}", frame);
                    break;
                }
                Err(e) => {
                    error!("WeCom WebSocket stream read error: {e}");
                    break;
                }
                _ => {}
            }
        }

        self.connected.store(false, Ordering::SeqCst);
        ping_task.abort();
        writer_task.abort();
        Ok(())
    }

    async fn handle_incoming_text(&self, text: &str) -> Result<(), WeComError> {
        let val: Value = match serde_json::from_str(text) {
            Ok(v) => v,
            Err(e) => {
                warn!("Ignoring non-JSON frame: {e}");
                return Ok(());
            }
        };

        let cmd = val.get("cmd").and_then(Value::as_str).unwrap_or("");
        if cmd == "aibot_subscribe_resp" {
            let errcode = val.get("errcode").and_then(Value::as_i64).unwrap_or(-1);
            if errcode != 0 {
                let errmsg = val
                    .get("errmsg")
                    .and_then(Value::as_str)
                    .unwrap_or("unknown");
                return Err(WeComError::Auth(format!(
                    "Subscribe failed ({errcode}): {errmsg}"
                )));
            }
            info!("Successfully subscribed to WeCom Bot OpenWS stream!");
            return Ok(());
        }

        if cmd == "aibot_msg_callback" {
            if let Some(body) = val.get("body") {
                let msg_id = body
                    .get("msg_id")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string();
                let chat_id = body
                    .get("chat_id")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string();
                let chat_type = body
                    .get("chat_type")
                    .and_then(Value::as_str)
                    .unwrap_or("single")
                    .to_string();
                let msg_type = body
                    .get("msg_type")
                    .and_then(Value::as_str)
                    .unwrap_or("text")
                    .to_string();

                let mut sender_id = String::new();
                let mut sender_name = None;
                if let Some(from) = body.get("from") {
                    if let Some(uid) = from.get("user_id").and_then(Value::as_str) {
                        sender_id = uid.to_string();
                    }
                    if let Some(name) = from.get("name").and_then(Value::as_str) {
                        sender_name = Some(name.to_string());
                    }
                }

                let content = if msg_type == "text" {
                    body.get("text")
                        .and_then(|t| t.get("content"))
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .to_string()
                } else {
                    format!("[{msg_type} message]")
                };

                let inbound = InboundBotMessage {
                    msg_id,
                    chat_id,
                    chat_type,
                    sender_id,
                    sender_name,
                    msg_type,
                    content,
                    raw: body.clone(),
                };

                let lock = self.inbound_tx.read().await;
                if let Some(ref tx) = *lock {
                    let _ = tx.send(inbound).await;
                }
            }
        }

        Ok(())
    }

    /// Reply directly to an inbound message (supporting streaming finish=false/true).
    pub async fn respond(
        &self,
        msg_id: &str,
        content: &str,
        msg_type: &str,
        finish: bool,
    ) -> Result<(), WeComError> {
        let envelope = WsEnvelope {
            cmd: "aibot_respond",
            headers: WsHeaders {
                req_id: format!(
                    "rsp-{}",
                    std::time::SystemTime::now()
                        .elapsed()
                        .unwrap_or_default()
                        .as_millis()
                ),
            },
            body: RespondBody {
                msg_id,
                msg_type,
                finish,
                text: if msg_type == "text" {
                    Some(TextContent { content })
                } else {
                    None
                },
                markdown: if msg_type == "markdown" {
                    Some(MarkdownContent { content })
                } else {
                    None
                },
            },
        };

        let json = serde_json::to_string(&envelope)?;
        let lock = self.outbound_tx.read().await;
        if let Some(ref tx) = *lock {
            tx.send(WsMessage::Text(json))
                .await
                .map_err(|e| WeComError::WebSocket(format!("Outbound send failed: {e}")))?;
            Ok(())
        } else {
            Err(WeComError::WebSocket(
                "Not connected to WeCom OpenWS gateway".to_string(),
            ))
        }
    }

    /// Proactively send a message to a chat session.
    pub async fn send_message(
        &self,
        chat_id: &str,
        content: &str,
        is_markdown: bool,
    ) -> Result<(), WeComError> {
        let msg_type = if is_markdown { "markdown" } else { "text" };
        let envelope = WsEnvelope {
            cmd: "aibot_send",
            headers: WsHeaders {
                req_id: format!(
                    "snd-{}",
                    std::time::SystemTime::now()
                        .elapsed()
                        .unwrap_or_default()
                        .as_millis()
                ),
            },
            body: SendBody {
                chat_id,
                msg_type,
                text: if !is_markdown {
                    Some(TextContent { content })
                } else {
                    None
                },
                markdown: if is_markdown {
                    Some(MarkdownContent { content })
                } else {
                    None
                },
            },
        };

        let json = serde_json::to_string(&envelope)?;
        let lock = self.outbound_tx.read().await;
        if let Some(ref tx) = *lock {
            tx.send(WsMessage::Text(json))
                .await
                .map_err(|e| WeComError::WebSocket(format!("Outbound send failed: {e}")))?;
            Ok(())
        } else {
            Err(WeComError::WebSocket(
                "Not connected to WeCom OpenWS gateway".to_string(),
            ))
        }
    }
}
