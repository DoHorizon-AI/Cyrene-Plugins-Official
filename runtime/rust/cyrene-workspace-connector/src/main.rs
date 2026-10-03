//! ┌─────────────────────────────────────────────────────────────────────┐
//! │  📄 main.rs                                                         │
//! │  Package: cyrene-workspace-connector                                │
//! │  Role: Entrypoint for standalone Cyrene Workspace Connector daemon.│
//! └─────────────────────────────────────────────────────────────────────┘

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use clap::Parser;
use cyrene_workspace_client_sdk::{AuthorityClient, ClientSdkError, MutualTlsClientConfig};
use cyrene_workspace_connector::WorkspaceConnectorWorker;
use cyrene_workspace_product_adapters::GenericProductHttpAdapter;
use tracing::{error, info};

const MAX_STARTUP_CONNECTION_ATTEMPTS: u32 = 8;

#[derive(Parser, Debug)]
#[command(name = "cyrene-workspace-connector")]
#[command(about = "Standalone Workspace Connector daemon for Cyrene Plugins")]
struct Args {
    #[arg(long, env = "CYRENE_WORKSPACE_ID")]
    workspace_id: String,

    #[arg(long, env = "CYRENE_CONNECTOR_RELAY_ENDPOINT")]
    relay_endpoint: String,

    #[arg(long, env = "CYRENE_CONNECTOR_RELAY_CA")]
    relay_ca: String,

    #[arg(long, env = "CYRENE_CONNECTOR_RELAY_SERVER_NAME")]
    relay_server_name: String,

    #[arg(long, env = "CYRENE_CONNECTOR_RELAY_CLIENT_CERT")]
    relay_client_cert: String,

    #[arg(long, env = "CYRENE_CONNECTOR_RELAY_CLIENT_KEY")]
    relay_client_key: String,

    #[arg(long, env = "CYRENE_CONNECTOR_AUTHORITY_CA")]
    authority_ca: String,

    #[arg(long, env = "CYRENE_CONNECTOR_AUTHORITY_SERVER_NAME")]
    authority_server_name: String,

    #[arg(long, env = "CYRENE_CONNECTOR_AUTHORITY_CLIENT_CERT")]
    authority_client_cert: String,

    #[arg(long, env = "CYRENE_CONNECTOR_AUTHORITY_CLIENT_KEY")]
    authority_client_key: String,

    #[arg(
        long,
        env = "CYRENE_CONNECTOR_JOURNAL",
        default_value = "/var/lib/cyrene/workspace-connector/journal.bin"
    )]
    journal_path: String,

    #[arg(long, env = "CYRENE_CONNECTOR_COMPONENTS", value_delimiter = ',')]
    supported_components: Vec<String>,

    #[arg(
        long,
        env = "CYRENE_CONNECTOR_ALLOWED_PRIVATE_ORIGINS",
        value_delimiter = ','
    )]
    allowed_private_origins: Vec<String>,

    #[arg(
        long,
        env = "CYRENE_CONNECTOR_HEALTH_ADDR",
        default_value = "127.0.0.1:18081"
    )]
    health_addr: std::net::SocketAddr,

    #[arg(long, default_value = "1000")]
    poll_interval_ms: u64,
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    tracing_subscriber::fmt::init();
    let args = Args::parse();

    if args.workspace_id.trim().is_empty()
        || args.supported_components.is_empty()
        || args
            .supported_components
            .iter()
            .any(|component| component.trim().is_empty())
    {
        return Err("workspace id and at least one supported component are required".into());
    }
    if !args.health_addr.ip().is_loopback() {
        return Err("Connector health listener must use a loopback address".into());
    }

    let readiness = Arc::new(AtomicBool::new(false));
    let health_readiness = Arc::clone(&readiness);
    let health_listener = tokio::net::TcpListener::bind(args.health_addr).await?;
    tokio::spawn(async move {
        if let Err(error) = serve_health(health_listener, health_readiness).await {
            error!("Connector health listener failed: {error}");
        }
    });

    info!(
        workspace_id = %args.workspace_id,
        "Starting Cyrene Workspace Connector daemon"
    );

    let relay_tls = MutualTlsClientConfig::from_pem_files(
        args.relay_ca,
        args.relay_server_name,
        args.relay_client_cert,
        args.relay_client_key,
    )?;
    let authority_tls = MutualTlsClientConfig::from_pem_files(
        args.authority_ca,
        args.authority_server_name,
        args.authority_client_cert,
        args.authority_client_key,
    )?;
    let mut retry_delay = Duration::from_secs(1);
    let mut retry_attempt = 0;
    let authority_client = loop {
        match AuthorityClient::connect_via_relay(
            args.relay_endpoint.clone(),
            &relay_tls,
            &authority_tls,
        )
        .await
        {
            Ok(client) => break client,
            Err(error) if is_retryable_connection_error(&error) => {
                retry_attempt += 1;
                if retry_attempt >= MAX_STARTUP_CONNECTION_ATTEMPTS {
                    return Err(error.into());
                }
                error!(
                    "Authority Relay tunnel unavailable; attempt {retry_attempt}/{MAX_STARTUP_CONNECTION_ATTEMPTS}, retrying in {}s: {error}",
                    retry_delay.as_secs()
                );
                tokio::time::sleep(retry_delay).await;
                retry_delay = (retry_delay * 2).min(Duration::from_secs(30));
            }
            Err(error) => return Err(error.into()),
        }
    };

    let http_adapter = Arc::new(GenericProductHttpAdapter::new(
        Duration::from_secs(30),
        args.allowed_private_origins,
    ));
    let mut worker = WorkspaceConnectorWorker::new_with_readiness(
        args.workspace_id,
        args.supported_components,
        authority_client,
        http_adapter,
        args.journal_path,
        readiness,
    )?;

    let poll_interval = Duration::from_millis(args.poll_interval_ms);
    loop {
        match worker.process_batch(10).await {
            Ok(count) => {
                if count > 0 {
                    info!("Processed {count} invocations");
                }
            }
            Err(e) => {
                error!("Error in batch processing: {e}");
            }
        }
        tokio::time::sleep(poll_interval).await;
    }
}

fn is_retryable_connection_error(error: &ClientSdkError) -> bool {
    match error {
        ClientSdkError::Transport(_) => true,
        ClientSdkError::Rpc(status) => matches!(
            status.code(),
            tonic::Code::Unavailable
                | tonic::Code::DeadlineExceeded
                | tonic::Code::ResourceExhausted
        ),
        ClientSdkError::InvalidEndpoint(_)
        | ClientSdkError::MutualTlsRequired
        | ClientSdkError::InvalidCredential(_)
        | ClientSdkError::ProtocolVersionMismatch { .. } => false,
    }
}

async fn serve_health(
    listener: tokio::net::TcpListener,
    readiness: Arc<AtomicBool>,
) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    info!("Connector loopback health endpoint is listening");
    loop {
        let (mut stream, peer) = listener.accept().await?;
        if !peer.ip().is_loopback() {
            continue;
        }
        let readiness = Arc::clone(&readiness);
        tokio::spawn(async move {
            let mut request = [0_u8; 1024];
            let Ok(count) = stream.read(&mut request).await else {
                return;
            };
            let request_line = std::str::from_utf8(&request[..count])
                .ok()
                .and_then(|request| request.lines().next())
                .unwrap_or_default();
            let (status, body) = if request_line.starts_with("GET /livez ") {
                ("200 OK", "live")
            } else if request_line.starts_with("GET /readyz ") && readiness.load(Ordering::Acquire)
            {
                ("200 OK", "ready")
            } else {
                ("503 Service Unavailable", "not ready")
            };
            let response = format!(
                "HTTP/1.1 {status}\r\ncontent-type: text/plain\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{body}",
                body.len()
            );
            let _ = stream.write_all(response.as_bytes()).await;
            let _ = stream.shutdown().await;
        });
    }
}
