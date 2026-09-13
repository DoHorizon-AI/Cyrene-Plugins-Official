# message.connector.v1 TCK / 消息连接器契约测试

This TCK validates the Plugins-owned payload schema independently of Platform.
It checks generated Rust, C#, Java, Kotlin, and Python projections and the real
OneBot v11 connector package.

本 TCK 独立校验 Plugins 所有的 payload schema，不读取 Platform checkout；覆盖 Rust、C#、
Java、Kotlin、Python 投影及真实 OneBot v11 connector。

```bash
cargo test --manifest-path contracts/rust/cyrene-plugin-contracts/Cargo.toml
dotnet run --project contracts/tck/message-connector-v1/dotnet/MessageConnectorContractTck.csproj
bash contracts/tck/message-connector-v1/generate-bindings.sh
uv run python plugins/connectors/onebot-v11/tools/generate_message_connector_bindings.py
```
