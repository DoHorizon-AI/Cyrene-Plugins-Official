# tool.provider.v1 TCK / 工具提供方契约测试

This TCK validates the Plugins-owned payload schema independently of Platform.
v1 covers the tool catalog and tool execution payloads for providers that
expose `tools/list` and `tools/call` (MCP is the first planned implementation).

本 TCK 独立校验 Plugins 所有的 payload schema，不读取 Platform checkout；v1 覆盖
`tools/list` 与 `tools/call` 的目录与执行载荷（首个计划实现为 MCP）。

```bash
cargo test --manifest-path contracts/rust/cyrene-plugin-contracts/Cargo.toml
```

Coverage map / 覆盖说明:

- Identifiers: capability ID, interface version, method ids, type URLs, and
  error codes (`contracts/rust/cyrene-plugin-contracts/tests/tool_provider_contract_tck.rs`).
- Tool catalog: `(binding_id, provider_tool_id)` identity, presentation facts,
  input/output schema JSON, opaque `catalog_version`, and the per-AgentRun
  snapshot semantics it enables.
- Tool execution: identity pair, opaque JSON arguments, optional catalog-version
  echo, text/JSON content parts, and typed `ToolProviderError` outcomes.

Provider-level conformance (stdio MCP against this contract) lives in
`runtime/rust/cyrene-mcp-provider/tests/mcp_provider_tests.rs` and the plugin-server
dispatch tests; both run real child processes speaking newline-delimited JSON-RPC.
---

<!-- Chinese Translation / 中文翻译 -->

## 中文翻译

# tool.provider.v1 TCK

本 TCK 独立验证 Plugins 所有的 payload schema，不依赖 Platform。v1 覆盖公开 tools/list 和 tools/call 的 provider 的工具目录和工具执行 payload；MCP 是首个计划实现。

```bash
cargo test --manifest-path contracts/rust/cyrene-plugin-contracts/Cargo.toml
```

## 覆盖说明

- **标识符**：capability ID、interface version、method ID、type URL 和错误码（contracts/rust/cyrene-plugin-contracts/tests/tool_provider_contract_tck.rs）。
- **工具目录**：(binding_id, provider_tool_id) identity、展示事实、输入/输出 schema JSON、不透明 catalog_version，以及其支持的 per-AgentRun 快照语义。
- **工具执行**：identity pair、不透明 JSON 参数、可选 catalog-version 回显、文本/JSON 内容部分，以及类型化 ToolProviderError 结果。

Provider 级的一致性验证（针对本契约的 stdio MCP）位于 runtime/rust/cyrene-mcp-provider/tests/mcp_provider_tests.rs 和 plugin-server 分派测试中；两者都会运行通过换行符分隔的 JSON-RPC 通信的真实子进程。
