//! ┌─────────────────────────────────────────────────────────────────────┐
//! │  📄 main.rs                                                         │
//! │  Binary: cyrene-workspace-sidecar                                  │
//! │  Role: Standalone daemon for loopback Workspace API bridge.        │
//! └─────────────────────────────────────────────────────────────────────┘

#![forbid(unsafe_code)]

use clap::Parser;
use cyrene_workspace_sidecar::{
    local_v1::workspace_sidecar_service_server::WorkspaceSidecarServiceServer as SidecarV1Server,
    local_v2::workspace_sidecar_service_server::WorkspaceSidecarServiceServer as SidecarV2Server,
    LocalBearerInterceptor, SidecarConfig, WorkspaceSidecarServiceImpl,
};
use std::net::SocketAddr;
use std::path::PathBuf;
use tonic::transport::Server;
use tracing::info;

#[derive(Parser, Debug)]
#[command(author, version, about = "Cyrene Workspace Sidecar Daemon")]
struct Args {
    #[arg(long, default_value = "127.0.0.1:50052")]
    listen_addr: SocketAddr,

    #[arg(long)]
    listen_uds: Option<PathBuf>,

    #[arg(long, env = "CYRENE_SIDECAR_RELAY_ENDPOINT")]
    relay_endpoint: String,

    #[arg(long, env = "CYRENE_SIDECAR_RELAY_CA")]
    relay_ca: PathBuf,

    #[arg(long, env = "CYRENE_SIDECAR_RELAY_SERVER_NAME")]
    relay_server_name: String,

    #[arg(long, env = "CYRENE_SIDECAR_RELAY_CLIENT_CERT")]
    relay_client_cert: PathBuf,

    #[arg(long, env = "CYRENE_SIDECAR_RELAY_CLIENT_KEY")]
    relay_client_key: PathBuf,

    #[arg(long, env = "CYRENE_SIDECAR_AUTHORITY_CA")]
    authority_ca: PathBuf,

    #[arg(long, env = "CYRENE_SIDECAR_AUTHORITY_SERVER_NAME")]
    authority_server_name: String,

    #[arg(long, env = "CYRENE_SIDECAR_AUTHORITY_CLIENT_CERT")]
    authority_client_cert: PathBuf,

    #[arg(long, env = "CYRENE_SIDECAR_AUTHORITY_CLIENT_KEY")]
    authority_client_key: PathBuf,

    #[arg(
        long,
        env = "CYRENE_SIDECAR_RESULT_WAIT_TIMEOUT_MS",
        default_value = "30000"
    )]
    result_wait_timeout_ms: u32,

    #[arg(
        long,
        env = "CYRENE_SIDECAR_HEALTH_ADDR",
        default_value = "127.0.0.1:18083"
    )]
    health_addr: SocketAddr,

    #[arg(
        long,
        env = "CYRENE_SIDECAR_LOCAL_TOKEN_FILE",
        default_value = "/etc/cyrene/workspace-sidecar.local-token"
    )]
    local_token_file: PathBuf,
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    tracing_subscriber::fmt::init();
    let args = Args::parse();
    let local_auth = LocalBearerInterceptor::from_file(&args.local_token_file)?;

    let relay_tls = cyrene_workspace_client_sdk::MutualTlsClientConfig::from_pem_files(
        args.relay_ca,
        args.relay_server_name,
        args.relay_client_cert,
        args.relay_client_key,
    )?;
    let authority_tls = cyrene_workspace_client_sdk::MutualTlsClientConfig::from_pem_files(
        args.authority_ca,
        args.authority_server_name,
        args.authority_client_cert,
        args.authority_client_key,
    )?;
    let config = SidecarConfig {
        relay_endpoint: args.relay_endpoint,
        relay_tls,
        authority_tls,
        result_wait_timeout_ms: args.result_wait_timeout_ms,
    };

    let service = WorkspaceSidecarServiceImpl::new(config);
    if !args.health_addr.ip().is_loopback() {
        return Err("Sidecar health listener must use a loopback address".into());
    }
    let health_service = service.clone();
    let health_listener = tokio::net::TcpListener::bind(args.health_addr).await?;
    tokio::spawn(async move {
        if let Err(error) = serve_health(health_listener, health_service).await {
            tracing::error!("Sidecar health listener failed: {error}");
        }
    });

    if let Some(uds_path) = args.listen_uds {
        #[cfg(unix)]
        {
            use std::os::unix::fs::{DirBuilderExt, FileTypeExt, PermissionsExt};

            if std::fs::symlink_metadata(&uds_path).is_ok() {
                let metadata = std::fs::symlink_metadata(&uds_path)?;
                if !metadata.file_type().is_socket() {
                    return Err(format!(
                        "refusing to replace non-socket path {}",
                        uds_path.display()
                    )
                    .into());
                }
                if tokio::net::UnixStream::connect(&uds_path).await.is_ok() {
                    return Err(format!(
                        "sidecar socket is already accepting connections: {}",
                        uds_path.display()
                    )
                    .into());
                }
                std::fs::remove_file(&uds_path)?;
            }
            if let Some(parent) = uds_path.parent() {
                let mut builder = std::fs::DirBuilder::new();
                builder.recursive(true).mode(0o700).create(parent)?;
                let metadata = std::fs::metadata(parent)?;
                if metadata.permissions().mode() & 0o077 != 0 {
                    return Err(format!(
                        "sidecar socket directory must be private: {}",
                        parent.display()
                    )
                    .into());
                }
            }

            info!(
                uds_path = %uds_path.display(),
                "Starting cyrene-workspace-sidecar on UDS"
            );

            let uds = tokio::net::UnixListener::bind(&uds_path)?;
            std::fs::set_permissions(&uds_path, std::fs::Permissions::from_mode(0o600))?;
            let uds_stream = tokio_stream::wrappers::UnixListenerStream::new(uds);

            Server::builder()
                .add_service(SidecarV1Server::with_interceptor(
                    service.clone(),
                    local_auth.clone(),
                ))
                .add_service(SidecarV2Server::with_interceptor(service, local_auth))
                .serve_with_incoming(uds_stream)
                .await?;
        }
    } else {
        if !args.listen_addr.ip().is_loopback() {
            return Err("sidecar TCP listener must use a loopback address".into());
        }
        info!(
            listen_addr = %args.listen_addr,
            "Starting cyrene-workspace-sidecar on TCP"
        );

        Server::builder()
            .add_service(SidecarV1Server::with_interceptor(
                service.clone(),
                local_auth.clone(),
            ))
            .add_service(SidecarV2Server::with_interceptor(service, local_auth))
            .serve(args.listen_addr)
            .await?;
    }

    Ok(())
}

async fn serve_health(
    listener: tokio::net::TcpListener,
    service: WorkspaceSidecarServiceImpl,
) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    use cyrene_workspace_sidecar::local_v2::workspace_sidecar_service_server::WorkspaceSidecarService;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    tracing::info!("Sidecar loopback health endpoint is listening");
    loop {
        let (mut stream, peer) = listener.accept().await?;
        if !peer.ip().is_loopback() {
            continue;
        }
        let service = service.clone();
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
            } else if request_line.starts_with("GET /readyz ") {
                match service
                    .health(tonic::Request::new(
                        cyrene_workspace_sidecar::local_v2::HealthRequest {},
                    ))
                    .await
                {
                    Ok(response) => {
                        if response.into_inner().local_ready {
                            ("200 OK", "ready")
                        } else {
                            ("503 Service Unavailable", "not ready")
                        }
                    }
                    _ => ("503 Service Unavailable", "not ready"),
                }
            } else {
                ("404 Not Found", "not found")
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
