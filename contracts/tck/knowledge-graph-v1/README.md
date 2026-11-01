# knowledge.graph.v1 TCK

The TCK checks the generated Rust projection, the Python SDK, and wire
interoperability with a separately generated protobuf binding.

```bash
cargo test --locked --manifest-path contracts/rust/cyrene-plugin-contracts/Cargo.toml knowledge_graph
python3 contracts/tck/knowledge-graph-v1/python/generate_bindings.py
```

本 TCK 检查生成的 Rust projection、Python SDK，以及与独立 protobuf binding 的 wire 互通。

```bash
cargo test --locked --manifest-path contracts/rust/cyrene-plugin-contracts/Cargo.toml knowledge_graph
python3 contracts/tck/knowledge-graph-v1/python/generate_bindings.py
```
