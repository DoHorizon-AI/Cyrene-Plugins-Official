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
