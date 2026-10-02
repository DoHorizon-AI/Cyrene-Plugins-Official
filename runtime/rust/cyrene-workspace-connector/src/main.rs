//! ┌─────────────────────────────────────────────────────────────────────┐
//! │  📄 main.rs                                                         │
//! │  Package: cyrene-workspace-connector                                │
//! │  Role: Entrypoint for standalone Cyrene Workspace Connector daemon.│
//! └─────────────────────────────────────────────────────────────────────┘

use std::sync::Arc;
use std::time::Duration;

use clap::Parser;
use cyrene_workspace_client_sdk::AuthorityClient;
use cyrene_workspace_connector::WorkspaceConnectorWorker;
use cyrene_workspace_product_adapters::GenericProductHttpAdapter;
use tracing::{error, info};

#[derive(Parser, Debug)]
#[command(name = "cyrene-workspace-connector")]
#[command(about = "Standalone Workspace Connector daemon for Cyrene Plugins")]
struct Args {
    #[arg(long, env = "CYRENE_CONNECTOR_ID", default_value = "connector-default")]
    connector_id: String,

    #[arg(long, env = "CYRENE_WORKSPACE_ID", default_value = "workspace-default")]
    workspace_id: String,

    #[arg(long, env = "CYRENE_AUTHORITY_ENDPOINT", default_value = "http://127.0.0.1:50051")]
    authority_endpoint: String,

    #[arg(long, env = "CYRENE_AUTHORITY_UDS")]
    authority_uds: Option<String>,

    #[arg(long, default_value = "1000")]
    poll_interval_ms: u64,
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    tracing_subscriber::fmt::init();
    let args = Args::parse();

    info!(
        connector_id = %args.connector_id,
        workspace_id = %args.workspace_id,
        "Starting Cyrene Workspace Connector daemon"
    );

    let authority_client = if let Some(uds) = args.authority_uds {
        AuthorityClient::connect_uds(uds).await?
    } else {
        AuthorityClient::connect(args.authority_endpoint).await?
    };

    let http_adapter = Arc::new(GenericProductHttpAdapter::default());
    let mut worker = WorkspaceConnectorWorker::new(
        args.connector_id,
        args.workspace_id,
        vec!["cyrene-workspace-connector".to_string()],
        authority_client,
        http_adapter,
    );

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
