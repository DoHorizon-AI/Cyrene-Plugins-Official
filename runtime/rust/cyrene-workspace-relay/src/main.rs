//! ┌─────────────────────────────────────────────────────────────────────┐
//! │  📄 main.rs                                                         │
//! │  Binary: cy-workspace-relay                                         │
//! │  Role: High-performance standalone Workspace Relay server.          │
//! └─────────────────────────────────────────────────────────────────────┘

#![forbid(unsafe_code)]

use clap::Parser;
use cyrene_workspace_relay::{WorkspaceRelay, WorkspaceRelayServiceServer};
use std::net::SocketAddr;
use std::path::PathBuf;
use tonic::transport::Server;
use tracing::info;

#[derive(Parser, Debug)]
#[command(author, version, about = "Cyrene Workspace Relay Server")]
struct Args {
    #[arg(long, default_value = "0.0.0.0:50054")]
    listen_addr: SocketAddr,

    #[arg(long)]
    listen_uds: Option<PathBuf>,
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    tracing_subscriber::fmt::init();
    let args = Args::parse();

    let relay = WorkspaceRelay::new();

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
                "Starting cy-workspace-relay on UDS"
            );

            let uds = tokio::net::UnixListener::bind(&uds_path)?;
            let uds_stream = tokio_stream::wrappers::UnixListenerStream::new(uds);

            Server::builder()
                .add_service(WorkspaceRelayServiceServer::new(relay))
                .serve_with_incoming(uds_stream)
                .await?;
        }
    } else {
        info!(
            listen_addr = %args.listen_addr,
            "Starting cy-workspace-relay on TCP"
        );

        Server::builder()
            .add_service(WorkspaceRelayServiceServer::new(relay))
            .serve(args.listen_addr)
            .await?;
    }

    Ok(())
}
