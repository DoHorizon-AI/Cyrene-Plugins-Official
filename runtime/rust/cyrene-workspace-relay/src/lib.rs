//! ┌─────────────────────────────────────────────────────────────────────┐
//! │  📄 lib.rs                                                          │
//! │  Package: cyrene-workspace-relay                                    │
//! │  Role: Decoupled Workspace Relay Service.                          │
//! │                                                                     │
//! │  模块职责：独立连接进程 Workspace Relay 运行时，提供双向流中继，无    │
//! │  Platform 控制面或数据库依赖。                                       │
//! └─────────────────────────────────────────────────────────────────────┘

#![forbid(unsafe_code)]

use std::collections::HashMap;
use std::pin::Pin;
use std::sync::{Arc, Mutex};
use std::time::{SystemTime, UNIX_EPOCH};

pub use cyrene_plugin_contracts::workspace_relay_v1::{
    workspace_relay_service_server::{WorkspaceRelayService, WorkspaceRelayServiceServer},
    RelayFrame, RelayPing, RelayPong,
};
use futures_util::StreamExt;
use tokio::sync::mpsc;
use tokio_stream::wrappers::ReceiverStream;
use tonic::{Request, Response, Status, Streaming};
use tracing::{debug, error, info};

pub type ResponseStream =
    Pin<Box<dyn tokio_stream::Stream<Item = Result<RelayFrame, Status>> + Send + 'static>>;

#[derive(Clone, Default)]
pub struct WorkspaceRelay {
    sessions: Arc<Mutex<HashMap<String, mpsc::Sender<Result<RelayFrame, Status>>>>>,
}

impl WorkspaceRelay {
    pub fn new() -> Self {
        Self {
            sessions: Arc::new(Mutex::new(HashMap::new())),
        }
    }
}

#[tonic::async_trait]
impl WorkspaceRelayService for WorkspaceRelay {
    async fn health_check(
        &self,
        request: Request<RelayPing>,
    ) -> Result<Response<RelayPong>, Status> {
        let req = request.into_inner();
        let now_ms = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_millis() as u64;

        Ok(Response::new(RelayPong {
            timestamp_ms: if req.timestamp_ms > 0 {
                req.timestamp_ms
            } else {
                now_ms
            },
        }))
    }

    type RelayStreamStream = ResponseStream;

    async fn relay_stream(
        &self,
        request: Request<Streaming<RelayFrame>>,
    ) -> Result<Response<Self::RelayStreamStream>, Status> {
        let mut in_stream = request.into_inner();
        let (out_tx, out_rx) = mpsc::channel(32);
        let sessions = Arc::clone(&self.sessions);

        tokio::spawn(async move {
            let mut active_session_id: Option<String> = None;

            while let Some(result) = in_stream.next().await {
                match result {
                    Ok(frame) => {
                        let session_id = frame.session_id.clone();
                        if !session_id.is_empty() && active_session_id.is_none() {
                            active_session_id = Some(session_id.clone());
                            sessions
                                .lock()
                                .unwrap()
                                .insert(session_id.clone(), out_tx.clone());
                            info!(session_id = %session_id, "Relay session registered");
                        }

                        if let Some(payload) = frame.payload {
                            match payload {
                                cyrene_plugin_contracts::workspace_relay_v1::relay_frame::Payload::Ping(ping) => {
                                    let pong_frame = RelayFrame {
                                        frame_id: format!("pong-{}", frame.frame_id),
                                        session_id: session_id.clone(),
                                        workspace_id: frame.workspace_id.clone(),
                                        payload: Some(
                                            cyrene_plugin_contracts::workspace_relay_v1::relay_frame::Payload::Pong(
                                                RelayPong {
                                                    timestamp_ms: ping.timestamp_ms,
                                                },
                                            ),
                                        ),
                                    };
                                    if out_tx.send(Ok(pong_frame)).await.is_err() {
                                        break;
                                    }
                                }
                                cyrene_plugin_contracts::workspace_relay_v1::relay_frame::Payload::Pong(_) => {
                                    debug!("Received pong on relay session");
                                }
                                other => {
                                    let echo_frame = RelayFrame {
                                        frame_id: format!("ack-{}", frame.frame_id),
                                        session_id: session_id.clone(),
                                        workspace_id: frame.workspace_id.clone(),
                                        payload: Some(other),
                                    };
                                    if out_tx.send(Ok(echo_frame)).await.is_err() {
                                        break;
                                    }
                                }
                            }
                        }
                    }
                    Err(e) => {
                        error!("Error in relay incoming stream: {e}");
                        break;
                    }
                }
            }

            if let Some(session_id) = active_session_id {
                sessions.lock().unwrap().remove(&session_id);
                info!(session_id = %session_id, "Relay session deregistered");
            }
        });

        let stream = ReceiverStream::new(out_rx);
        Ok(Response::new(Box::pin(stream) as ResponseStream))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn test_relay_health_check() {
        let relay = WorkspaceRelay::new();
        let ping = RelayPing {
            timestamp_ms: 12345,
        };
        let resp = relay.health_check(Request::new(ping)).await.unwrap();
        assert_eq!(resp.into_inner().timestamp_ms, 12345);
    }
}
