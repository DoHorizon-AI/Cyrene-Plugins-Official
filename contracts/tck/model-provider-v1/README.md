# model.provider.v1 TCK / 模型提供方契约测试

This TCK validates the Plugins-owned payload schema independently of Platform.
It checks generated Rust, C#, Java, Kotlin, and Python projections plus the
real `model-api-connector` codec and provider tests.

本 TCK 独立校验 Plugins 所有的 payload schema，不读取 Platform checkout；覆盖 Rust、C#、
Java、Kotlin、Python 投影及真实 `model-api-connector` codec 与 provider 测试。

```bash
cargo test --manifest-path contracts/rust/cyrene-plugin-contracts/Cargo.toml
dotnet run --project contracts/tck/model-provider-v1/dotnet/ModelProviderContractTck.csproj
bash contracts/tck/model-provider-v1/generate-bindings.sh
bash contracts/tck/model-provider-v1/run-jvm-tck.sh
```
