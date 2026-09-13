# computer.runtime.v1 TCK / 计算机运行时契约测试

This TCK validates the Plugins-owned payload schema independently of Platform.
It checks the generated Rust and C# projections plus the real Rust host:
the contract identifiers and error codes, the bounded filesystem payloads
(including `list_dir`), and the host's fail-closed dispatch behaviour.

本 TCK 独立校验 Plugins 所有的 payload schema，不读取 Platform checkout；覆盖 Rust 与
C# 投影，以及真实 Rust host 的契约标识、错误码、有界文件系统载荷（含 `list_dir`）
与 fail-closed 分派行为。

```bash
cargo test --manifest-path contracts/rust/cyrene-plugin-contracts/Cargo.toml
dotnet run --project contracts/tck/computer-runtime-v1/dotnet/ComputerRuntimeContractTck.csproj
cargo test --manifest-path runtime/rust/cyrene-plugin-server/Cargo.toml
```

Coverage map / 覆盖说明:

- `contracts/rust/cyrene-plugin-contracts/tests/computer_runtime_contract_tck.rs`:
  stable capability ID, method identifiers, error codes, and payload round-trips
  in the Rust projection.
- `contracts/tck/computer-runtime-v1/dotnet/`: the generated C# projection
  round-trips the same payloads and pins enum numbers.
- `runtime/rust/cyrene-plugin-server/tests/server_integration_tests.rs`:
  the real host dispatches `ExecuteCommand`, artifacts, and the W2-1 `ListDir`
  method, and denies path traversal.
