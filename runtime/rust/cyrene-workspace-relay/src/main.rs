//! ┌─────────────────────────────────────────────────────────────────────┐
//! │  📄 main.rs                                                         │
//! │  Binary: cy-workspace-relay                                         │
//! │  Role: mTLS-only standalone Workspace byte relay.                   │
//! └─────────────────────────────────────────────────────────────────────┘

#![forbid(unsafe_code)]

use clap::Parser;
use cyrene_workspace_relay::{PeerIdentityAllowlist, WorkspaceRelay, WorkspaceTunnelServiceServer};
use std::net::SocketAddr;
use std::path::PathBuf;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;
use tonic::transport::{Certificate, Identity, Server, ServerTlsConfig};
use tracing::info;

const MAX_TUNNEL_MESSAGE_BYTES: usize = 64 * 1024 + 1024;

#[derive(Parser, Debug)]
#[command(author, version, about = "Cyrene Workspace Authority byte relay")]
struct Args {
    #[arg(
        long,
        env = "CYRENE_RELAY_LISTEN_ADDR",
        default_value = "0.0.0.0:50054"
    )]
    listen_addr: SocketAddr,

    #[arg(
        long,
        env = "CYRENE_RELAY_HEALTH_ADDR",
        default_value = "127.0.0.1:18080"
    )]
    health_addr: SocketAddr,

    #[arg(long, env = "CYRENE_RELAY_TLS_CA")]
    tls_ca: PathBuf,

    #[arg(long, env = "CYRENE_RELAY_TLS_CERT")]
    tls_certificate: PathBuf,

    #[arg(long, env = "CYRENE_RELAY_TLS_KEY")]
    tls_private_key: PathBuf,

    #[arg(long, env = "CYRENE_RELAY_AUTHORITY_BRIDGE_SANS")]
    authority_bridge_sans: String,

    #[arg(long, env = "CYRENE_RELAY_CONNECTOR_SANS")]
    connector_sans: String,
}

fn parse_sans(value: &str) -> Vec<String> {
    value
        .split(',')
        .map(str::trim)
        .filter(|identity| !identity.is_empty())
        .map(str::to_owned)
        .collect()
}

async fn serve_health(
    addr: SocketAddr,
    relay: WorkspaceRelay,
) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let listener = TcpListener::bind(addr).await?;
    loop {
        let (mut stream, _) = listener.accept().await?;
        let relay = relay.clone();
        tokio::spawn(async move {
            let mut request = [0_u8; 1024];
            let read = match stream.read(&mut request).await {
                Ok(read) => read,
                Err(_) => return,
            };
            let request = String::from_utf8_lossy(&request[..read]);
            let path = request
                .lines()
                .next()
                .and_then(|line| line.split_whitespace().nth(1));
            let (status, body) = match path {
                Some("/livez") => ("200 OK", "{\"live\":true}\n".to_string()),
                Some("/readyz") => {
                    let bridge_ready = relay.authority_bridge_registered().await;
                    let status = if bridge_ready {
                        "200 OK"
                    } else {
                        "503 Service Unavailable"
                    };
                    (
                        status,
                        format!("{{\"ready\":{bridge_ready},\"authority_bridge_registered\":{bridge_ready}}}\n"),
                    )
                }
                _ => ("404 Not Found", "{\"error\":\"not_found\"}\n".to_string()),
            };
            let response = format!(
                "HTTP/1.1 {status}\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{body}",
                body.len()
            );
            let _ = stream.write_all(response.as_bytes()).await;
            let _ = stream.shutdown().await;
        });
    }
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    tracing_subscriber::fmt::init();
    let args = Args::parse();
    if !args.health_addr.ip().is_loopback() {
        return Err("health endpoint must bind to loopback only".into());
    }
    let allowlist = PeerIdentityAllowlist::new(
        parse_sans(&args.authority_bridge_sans),
        parse_sans(&args.connector_sans),
    )?;
    let server_tls = ServerTlsConfig::new()
        .identity(Identity::from_pem(
            std::fs::read(args.tls_certificate)?,
            std::fs::read(args.tls_private_key)?,
        ))
        .client_ca_root(Certificate::from_pem(std::fs::read(args.tls_ca)?));
    let relay = WorkspaceRelay::new(allowlist);
    let health_relay = relay.clone();
    tokio::spawn(async move {
        if let Err(error) = serve_health(args.health_addr, health_relay).await {
            tracing::error!(%error, "Relay health endpoint failed");
        }
    });
    info!(
        addr = %args.listen_addr,
        "Starting mTLS-only Cyrene Workspace Tunnel Relay"
    );
    Server::builder()
        .http2_keepalive_interval(Some(std::time::Duration::from_secs(15)))
        .http2_keepalive_timeout(Some(std::time::Duration::from_secs(5)))
        .tls_config(server_tls)?
        .add_service(
            WorkspaceTunnelServiceServer::new(relay)
                .max_decoding_message_size(MAX_TUNNEL_MESSAGE_BYTES)
                .max_encoding_message_size(MAX_TUNNEL_MESSAGE_BYTES),
        )
        .serve(args.listen_addr)
        .await?;
    Ok(())
}
