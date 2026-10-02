//! ┌─────────────────────────────────────────────────────────────────────┐
//! │  📄 main.rs                                                         │
//! │  Binary: cyrene-workspace-sidecar                                  │
//! │  Role: Standalone daemon for loopback Workspace API bridge.        │
//! └─────────────────────────────────────────────────────────────────────┘

#![forbid(unsafe_code)]

use clap::Parser;
use cyrene_workspace_sidecar::{
    SidecarConfig, WorkspaceSidecarServiceImpl, WorkspaceSidecarServiceServer,
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

    #[arg(long, default_value = "http://127.0.0.1:50051")]
    authority_addr: String,

    #[arg(long)]
    authority_uds: Option<PathBuf>,

    #[arg(long, default_value = "http://127.0.0.1:50053")]
    bridge_addr: String,

    #[arg(long, default_value = "/tmp/cyrene-frontend-bridge.sock")]
    bridge_uds: Option<PathBuf>,
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    tracing_subscriber::fmt::init();
    let args = Args::parse();

    let config = SidecarConfig {
        authority_endpoint: args.authority_addr,
        authority_uds_path: args.authority_uds,
        bridge_endpoint: args.bridge_addr,
        bridge_uds_path: args.bridge_uds,
    };

    let service = WorkspaceSidecarServiceImpl::new(config);

    if let Some(uds_path) = args.listen_uds {
        #[cfg(unix)]
        {
            if uds_path.exists() {
                let _ = std::fs::remove_file(&uds_path);
            }
            if let Some(parent) = uds_path.parent() {
                let _ = std::fs::create_dir_all(parent);
            }

            info!(
                uds_path = %uds_path.display(),
                "Starting cyrene-workspace-sidecar on UDS"
            );

            let uds = tokio::net::UnixListener::bind(&uds_path)?;
            let uds_stream = tokio_stream::wrappers::UnixListenerStream::new(uds);

            Server::builder()
                .add_service(WorkspaceSidecarServiceServer::new(service))
                .serve_with_incoming(uds_stream)
                .await?;
        }
    } else {
        info!(
            listen_addr = %args.listen_addr,
            "Starting cyrene-workspace-sidecar on TCP"
        );

        Server::builder()
            .add_service(WorkspaceSidecarServiceServer::new(service))
            .serve(args.listen_addr)
            .await?;
    }

    Ok(())
}
