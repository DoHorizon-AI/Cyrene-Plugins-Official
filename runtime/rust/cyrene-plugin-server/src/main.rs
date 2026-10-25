// SPDX-License-Identifier: Apache-2.0
//! Cyrene Standalone Native Plugin gRPC Server (R08)
//! Cyrene 独立原生插件 gRPC 服务（R08）
//!
//! Exposes agent.runtime.v1, memory.provider.v1, computer.runtime.v1, and tool.provider.v1
//! over standard tonic gRPC DirectPluginRuntimeService.
//! 通过标准 tonic gRPC DirectPluginRuntimeService 暴露 agent、memory、computer 和 tool provider 能力。

pub mod proto {
    // Tonic generates Result signatures whose error type is intentionally
    // fixed by the gRPC API; downstream code cannot box or shrink it.
    //     Tonic 生成的 Result 签名使用 gRPC API 固定的错误类型；下游代码不能将其装箱或缩小。
    #![allow(clippy::result_large_err)]
    tonic::include_proto!("cyrene.plugin.runtime.v1");
}

pub mod service;

use std::io::Write;
use std::net::SocketAddr;
use std::time::Duration;
use tokio::net::TcpListener;
use tokio_stream::wrappers::TcpListenerStream;
use tonic::transport::Server;

use crate::proto::direct_plugin_runtime_client::DirectPluginRuntimeClient;
use crate::proto::direct_plugin_runtime_server::DirectPluginRuntimeServer;
use crate::proto::{health_response, HealthRequest};
use crate::service::DirectPluginRuntimeServiceImpl;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut port: u16 = 50051;
    // Bind on the private container interface so the sibling mTLS proxy can
    // connect. The Compose service must never publish this port publicly.
    let mut host = "0.0.0.0".to_string();
    let mut health_check = false;

    let args: Vec<String> = std::env::args().collect();
    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--health-check" => health_check = true,
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

    if health_check {
        return run_health_check(&host, port).await;
    }

    let addr: SocketAddr = format!("{}:{}", host, port).parse()?;
    let listener = TcpListener::bind(addr).await?;
    let bound_addr = listener.local_addr()?;

    // Print announcement header to stdout for caller detection
    // 向 stdout 输出公告头，供调用方检测服务启动
    println!("[CYRENE_SERVER_STARTED] addr={}", bound_addr);
    std::io::stdout().flush().ok();

    let service = DirectPluginRuntimeServiceImpl::from_environment()
        .map_err(|error| std::io::Error::new(std::io::ErrorKind::InvalidInput, error))?;
    let incoming = TcpListenerStream::new(listener);

    Server::builder()
        .add_service(DirectPluginRuntimeServer::new(service))
        .serve_with_incoming(incoming)
        .await?;

    Ok(())
}

async fn run_health_check(host: &str, port: u16) -> Result<(), Box<dyn std::error::Error>> {
    let target_host = match host {
        "0.0.0.0" | "::" => "127.0.0.1",
        value => value,
    };
    let endpoint = tonic::transport::Endpoint::from_shared(format!("http://{target_host}:{port}"))?
        .connect_timeout(Duration::from_secs(2));
    let channel = endpoint.connect().await?;
    let mut client = DirectPluginRuntimeClient::new(channel);
    let response = client.health(HealthRequest {}).await?.into_inner();
    let expected_capabilities = ["computer.runtime.v1", "tool.provider.v1"];
    if response.status != health_response::Status::Serving as i32
        || response.plugin_id != crate::service::PLUGIN_ID
        || expected_capabilities.iter().any(|capability| {
            !response
                .capabilities
                .iter()
                .any(|value| value == capability)
        })
    {
        return Err(format!(
            "runner health mismatch: status={}, plugin_id={}, capabilities={:?}",
            response.status, response.plugin_id, response.capabilities
        )
        .into());
    }
    println!("[CYRENE_HEALTHCHECK_OK] plugin_id={}", response.plugin_id);
    Ok(())
}
