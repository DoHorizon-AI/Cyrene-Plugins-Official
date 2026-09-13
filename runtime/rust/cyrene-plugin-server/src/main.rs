// SPDX-License-Identifier: Apache-2.0
//! Cyrene Standalone Native Plugin gRPC Server (R08)
//!
//! Exposes agent.runtime.v1, memory.provider.v1, and computer.runtime.v1
//! over standard tonic gRPC DirectPluginRuntimeService.

pub mod proto {
    tonic::include_proto!("cyrene.plugin.runtime.v1");
}

pub mod service;

use std::io::Write;
use std::net::SocketAddr;
use tokio::net::TcpListener;
use tokio_stream::wrappers::TcpListenerStream;
use tonic::transport::Server;

use crate::proto::direct_plugin_runtime_server::DirectPluginRuntimeServer;
use crate::service::DirectPluginRuntimeServiceImpl;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut port: u16 = 50051;
    let mut host = "127.0.0.1".to_string();

    let args: Vec<String> = std::env::args().collect();
    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--port" | "-p" if i + 1 < args.len() => {
                port = args[i + 1].parse().unwrap_or(50051);
                i += 1;
            }
            "--host" | "-h" | "--addr" if i + 1 < args.len() => {
                host = args[i + 1].clone();
                i += 1;
            }
            _ => {}
        }
        i += 1;
    }

    let addr: SocketAddr = format!("{}:{}", host, port).parse()?;
    let listener = TcpListener::bind(addr).await?;
    let bound_addr = listener.local_addr()?;

    // Print announcement header to stdout for caller detection
    println!("[CYRENE_SERVER_STARTED] addr={}", bound_addr);
    std::io::stdout().flush().ok();

    let service = DirectPluginRuntimeServiceImpl::new();
    let incoming = TcpListenerStream::new(listener);

    Server::builder()
        .add_service(DirectPluginRuntimeServer::new(service))
        .serve_with_incoming(incoming)
        .await?;

    Ok(())
}
