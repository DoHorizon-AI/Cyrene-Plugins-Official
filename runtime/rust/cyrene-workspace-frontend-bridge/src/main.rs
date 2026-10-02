//! ┌─────────────────────────────────────────────────────────────────────┐
//! │  📄 main.rs                                                         │
//! │  Package: cyrene-workspace-frontend-bridge                          │
//! │  Role: Entrypoint for standalone Cyrene Workspace Frontend Bridge.  │
//! └─────────────────────────────────────────────────────────────────────┘

use std::fs;
use std::path::PathBuf;
use std::sync::Arc;

use clap::Parser;
use cyrene_workspace_frontend_bridge::{
    WorkspaceFrontendBridgeServiceImpl, WorkspaceFrontendBridgeServiceServer,
};
use cyrene_workspace_product_adapters::GenericProductHttpAdapter;
use tokio::net::UnixListener;
use tokio_stream::wrappers::UnixListenerStream;
use tonic::transport::Server;
use tracing::info;

#[derive(Parser, Debug)]
#[command(name = "cyrene-workspace-frontend-bridge")]
#[command(about = "Standalone Workspace Frontend Bridge daemon for Platform BFF")]
struct Args {
    #[arg(long, env = "CYRENE_BRIDGE_UDS", default_value = "/tmp/cyrene-frontend-bridge.sock")]
    uds_path: PathBuf,

    #[arg(long, env = "CYRENE_BRIDGE_TCP")]
    tcp_addr: Option<String>,
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    tracing_subscriber::fmt::init();
    let args = Args::parse();

    let http_adapter = Arc::new(GenericProductHttpAdapter::default());
    let service = WorkspaceFrontendBridgeServiceImpl::new(http_adapter);
    let server = WorkspaceFrontendBridgeServiceServer::new(service);

    if let Some(tcp_addr) = args.tcp_addr {
        let addr = tcp_addr.parse()?;
        info!(addr = %addr, "Starting Frontend Bridge on TCP");
        Server::builder()
            .add_service(server)
            .serve(addr)
            .await?;
    } else {
        if args.uds_path.exists() {
            let _ = fs::remove_file(&args.uds_path);
        }
        if let Some(parent) = args.uds_path.parent() {
            fs::create_dir_all(parent)?;
        }
        let listener = UnixListener::bind(&args.uds_path)?;
        info!(path = %args.uds_path.display(), "Starting Frontend Bridge on UDS");
        Server::builder()
            .add_service(server)
            .serve_with_incoming(UnixListenerStream::new(listener))
            .await?;
    }

    Ok(())
}
