// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 agent_bench.rs                                                  │
// │  Package: cyrene-agent-runtime::benches                             │
// │  Role: Benchmark against Rig, Native Rust loop & candidates (T57).  │
// │                                                                     │
// │  模块职责：对比测量原生 Rust 循环、Rig 适配器及宿主候选的性能指标    │
// └─────────────────────────────────────────────────────────────────────┘

use std::time::Instant;
use tokio::runtime::Runtime;

use cyrene_agent_runtime::adapter::rig_adapter::{AgentDriver, RigAgentAdapter};
use cyrene_agent_runtime::engine::cancel::CancellationToken;
use cyrene_agent_runtime::engine::turn_loop::CyreneNativeAgentLoop;
use cyrene_agent_runtime::tck::{MockModelProvider, MockToolProvider};
use cyrene_plugin_contracts::agent_runtime_v1::AgentRunRequest;

fn benchmark_driver(name: &str, driver: &(dyn AgentDriver + 'static), iterations: usize) {
    let rt = Runtime::new().unwrap();
    let model = MockModelProvider::with_tool_turns(1);
    let tools = MockToolProvider::new();

    let start = Instant::now();
    for i in 0..iterations {
        let req = AgentRunRequest {
            run_id: format!("bench-{}-{}", name, i),
            session_id: "bench-session".to_string(),
            prompt: "Benchmark query".to_string(),
            messages: Vec::new(),
            available_tools: Vec::new(),
            config: None,
        };

        let result = rt.block_on(async {
            driver
                .execute_run(req, &model, Some(&tools), CancellationToken::new())
                .await
        });

        assert!(result.is_ok(), "Benchmark run failed");
    }

    let elapsed = start.elapsed();
    let avg_us = elapsed.as_micros() as f64 / iterations as f64;
    let ops_sec = (iterations as f64 / elapsed.as_secs_f64()) as u64;

    println!(
        "[BENCHMARK] Target: {:<22} | Iterations: {:<5} | Total: {:<7.2?} | Latency: {:<7.2} µs/op | Throughput: {} ops/sec",
        name, iterations, elapsed, avg_us, ops_sec
    );
}

fn main() {
    println!("═════════════════════════════════════════════════════════════════════════");
    println!("  Cyrene Agent Runtime Benchmark Suite (Milestone M5A Task T57)");
    println!("═════════════════════════════════════════════════════════════════════════");

    let iterations = 2000;

    let native_loop = CyreneNativeAgentLoop::new();
    benchmark_driver("CyreneNativeAgentLoop", &native_loop, iterations);

    let rig_adapter = RigAgentAdapter::new();
    benchmark_driver("RigAgentAdapter", &rig_adapter, iterations);

    println!("═════════════════════════════════════════════════════════════════════════");
}
