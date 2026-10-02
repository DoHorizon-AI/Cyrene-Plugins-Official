//! ┌─────────────────────────────────────────────────────────────────────┐
//! │  📄 main.rs                                                         │
//! │  Package: cyrene-workspace-frontend-bridge                          │
//! │  Role: UDS-only local facade and Authority Tunnel Bridge.           │
//! └─────────────────────────────────────────────────────────────────────┘

use std::fs;
use std::os::unix::fs::{DirBuilderExt, FileTypeExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use clap::Parser;
use cyrene_workspace_client_sdk::MutualTlsClientConfig;
use cyrene_workspace_frontend_bridge::{
    serve_authority_tunnel_once, WorkspaceFrontendBridgeServiceImpl,
    WorkspaceFrontendBridgeServiceServer,
};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;
use tokio::net::UnixListener;
use tokio_stream::wrappers::UnixListenerStream;
use tonic::transport::Server;
use tracing::{error, info};

#[derive(Parser, Debug)]
#[command(name = "cyrene-workspace-frontend-bridge")]
#[command(about = "Authority-side fixed-target Relay byte bridge")]
struct Args {
    #[arg(
        long,
        env = "CYRENE_BRIDGE_UDS",
        default_value = "/run/cyrene-frontend-bridge/frontend.sock"
    )]
    uds_path: PathBuf,

    #[arg(long, env = "CYRENE_BRIDGE_ALLOWED_UID")]
    allowed_uid: u32,

    #[arg(long, env = "CYRENE_BRIDGE_RELAY_ENDPOINT")]
    relay_endpoint: String,

    #[arg(long, env = "CYRENE_BRIDGE_RELAY_CA")]
    relay_ca: PathBuf,

    #[arg(long, env = "CYRENE_BRIDGE_RELAY_SERVER_NAME")]
    relay_server_name: String,

    #[arg(long, env = "CYRENE_BRIDGE_RELAY_CLIENT_CERT")]
    relay_client_certificate: PathBuf,

    #[arg(long, env = "CYRENE_BRIDGE_RELAY_CLIENT_KEY")]
    relay_client_private_key: PathBuf,

    #[arg(long, env = "CYRENE_BRIDGE_AUTHORITY_UPSTREAM")]
    authority_upstream: String,

    #[arg(
        long,
        env = "CYRENE_BRIDGE_HEALTH_ADDR",
        default_value = "127.0.0.1:18082"
    )]
    health_addr: std::net::SocketAddr,
}

async fn serve_health(
    addr: std::net::SocketAddr,
    readiness: Arc<AtomicBool>,
) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let listener = TcpListener::bind(addr).await?;
    loop {
        let (mut stream, _) = listener.accept().await?;
        let readiness = Arc::clone(&readiness);
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
                    let ready = readiness.load(Ordering::Acquire);
                    let status = if ready {
                        "200 OK"
                    } else {
                        "503 Service Unavailable"
                    };
                    (
                        status,
                        format!("{{\"ready\":{ready},\"relay_registered\":{ready}}}\n"),
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

fn prepare_uds(path: &std::path::Path) -> Result<UnixListener, Box<dyn std::error::Error>> {
    let parent = path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
        .ok_or("UDS path must have a private parent directory")?;
    std::fs::DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(parent)?;
    verify_no_symlink_directories(parent)?;
    let parent_metadata = fs::symlink_metadata(parent)?;
    if parent_metadata.permissions().mode() & 0o077 != 0 {
        return Err("UDS parent directory must not be accessible to group or other users".into());
    }

    match fs::symlink_metadata(path) {
        Ok(metadata) if metadata.file_type().is_socket() => fs::remove_file(path)?,
        Ok(_) => return Err("refusing to replace a non-socket UDS path".into()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Err(error) => return Err(error.into()),
    }

    let listener = UnixListener::bind(path)?;
    fs::set_permissions(path, fs::Permissions::from_mode(0o600))?;
    Ok(listener)
}

fn verify_no_symlink_directories(path: &Path) -> Result<(), Box<dyn std::error::Error>> {
    let mut current = PathBuf::new();
    for component in path.components() {
        current.push(component.as_os_str());
        let metadata = fs::symlink_metadata(&current)?;
        if metadata.file_type().is_symlink() || !metadata.is_dir() {
            return Err("UDS path ancestors must be real directories, not symlinks".into());
        }
    }
    Ok(())
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    tracing_subscriber::fmt::init();
    let args = Args::parse();
    if !args.health_addr.ip().is_loopback() {
        return Err("health endpoint must bind to loopback only".into());
    }
    let relay_tls = MutualTlsClientConfig::from_pem_files(
        args.relay_ca,
        args.relay_server_name,
        args.relay_client_certificate,
        args.relay_client_private_key,
    )?;
    let relay_endpoint = args.relay_endpoint;
    let authority_upstream = args.authority_upstream;
    let readiness = Arc::new(AtomicBool::new(false));
    let tunnel_readiness = Arc::clone(&readiness);
    tokio::spawn(async move {
        let mut retry_delay = Duration::from_secs(1);
        loop {
            match serve_authority_tunnel_once(
                relay_endpoint.clone(),
                &relay_tls,
                authority_upstream.clone(),
                Arc::clone(&tunnel_readiness),
            )
            .await
            {
                Ok(()) => error!("Relay tunnel session ended; retrying without replay"),
                Err(error) => {
                    error!(%error, "Relay tunnel session failed; retrying without replay")
                }
            }
            tokio::time::sleep(retry_delay).await;
            retry_delay = (retry_delay * 2).min(Duration::from_secs(30));
        }
    });
    let health_readiness = Arc::clone(&readiness);
    tokio::spawn(async move {
        if let Err(error) = serve_health(args.health_addr, health_readiness).await {
            tracing::error!(%error, "Bridge health endpoint failed");
        }
    });

    let listener = prepare_uds(&args.uds_path)?;
    info!(path = %args.uds_path.display(), "Serving local Bridge UDS with peer UID authorization");
    let service = WorkspaceFrontendBridgeServiceImpl::new(args.allowed_uid);
    Server::builder()
        .add_service(WorkspaceFrontendBridgeServiceServer::new(service))
        .serve_with_incoming(UnixListenerStream::new(listener))
        .await?;
    Ok(())
}
