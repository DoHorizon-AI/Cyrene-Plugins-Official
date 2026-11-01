# memory.provider.v1 TCK

The Python TCK compares all seven method codecs and the paginated export fields
against independently generated protobuf messages.

```bash
cargo test --locked --manifest-path contracts/rust/cyrene-plugin-contracts/Cargo.toml memory_provider
uv run --isolated --no-project --with grpcio-tools==1.62.3 --with protobuf==4.25.9 python contracts/tck/memory-provider-v1/python/generate_bindings.py
```

本 Python TCK 将七个方法的编解码及分页导出字段与独立生成的 protobuf 消息逐项对照。

```bash
cargo test --locked --manifest-path contracts/rust/cyrene-plugin-contracts/Cargo.toml memory_provider
uv run --isolated --no-project --with grpcio-tools==1.62.3 --with protobuf==4.25.9 python contracts/tck/memory-provider-v1/python/generate_bindings.py
```
