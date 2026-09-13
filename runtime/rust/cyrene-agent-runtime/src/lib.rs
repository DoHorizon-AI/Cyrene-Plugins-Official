// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 lib.rs                                                          │
// │  Package: cyrene-agent-runtime                                      │
// │  Role: Canonical Stateless Agent Runtime for Cyrene (M5A).          │
// │                                                                     │
// │  模块职责：生产级 Rust 智能体运行时库：                              │
// │           1. 无状态单次调用与有序流式事件 (T59)                     │
// │           2. 仅通过 model.provider.v1 消费模型 (T60)                │
// │           3. 仅通过 tool.provider.v1 / computer.runtime.v1 调工具   │
// │           4. 显式注入 Memory，无内置持久化 session (T62)           │
// │           5. 业务策略与 Prompt 完全留在 Product 侧 (T63)            │
// │           6. 严格执行轮次/字节/并发/超时限制 (T64)                  │
// │           7. 严禁框架内部类型泄露至公共契约表面 (T65)                │
// └─────────────────────────────────────────────────────────────────────┘

pub mod adapter;
pub mod engine;
pub mod limits;
pub mod tck;

// Re-export canonical Protobuf types
pub use cyrene_plugin_contracts::agent_runtime_v1;

// Re-export runtime core components
pub use adapter::{InjectedMemoryProvider, ModelProvider, RigAgentAdapter, ToolProvider};
pub use engine::{CancellationToken, CyreneNativeAgentLoop};
pub use limits::RuntimeLimits;
pub use tck::AgentTckSuite;
