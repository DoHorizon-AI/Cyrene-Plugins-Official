# Cyrene Plugins

This repository publishes Cyrene-owned capability contracts, language SDKs,
native runtimes, and connector packages from a reviewed empty-history source
export. `source-manifest.json` records the exact private audit commit and the
SHA-256 of every published file.

本仓库通过经过审查的空历史源码导出，发布 Cyrene 自有的能力契约、多语言 SDK、原生
运行时与 Connector 包。`source-manifest.json` 记录精确的私有审计提交，以及每个公开
文件的 SHA-256。

## Publication status / 发布状态

The repository is designed for public, credential-free CI. A build or test
pass does not promote an unconfigured runtime to production readiness. Native
providers and optional capabilities continue to fail closed until an operator
supplies a valid binding and endpoint.

本仓库面向无需私有凭据的公开 CI。构建或测试通过不代表未配置的 runtime 已达到生产
就绪；在 operator 提供有效 binding 与 endpoint 前，原生 provider 与可选能力保持
fail-closed。

## Published surface / 发布范围

- `contracts/`: versioned Protobuf, JSON, C ABI, TCK, and generated contract projections.
- `sdk/`: Java/Spring and Python client/runtime SDKs.
- `runtime/`: Rust runtimes and .NET Native AOT hosts/providers.
- `plugins/`: official capability Plugins across connectors, evaluation, models,
  policy, and tools, including the Plugins-owned OneBot v11 connector.
- `contracts/capabilities.yaml`: the generated capability index covering contracts,
  implementations, TCK coverage, and maturity.
- `docs/plans/`: reviewed implementation plans and their evidence.
- Protected QQ-side interface research documents, not a bundled QQ runtime.
- `manifests/`: the canonical Plugin manifest schema.

The repository does not own Product semantics or Platform lifecycle authority.
Products obtain an authorized opaque connection reference through Platform and
then call the selected Plugin directly through the versioned contract.

本仓库不拥有 Product 语义或 Platform 生命周期权威。Product 通过 Platform 获得授权的
opaque connection reference，再按版本化契约直连所选 Plugin。

## Verification / 验证

The authoritative gate definition is
[`.github/workflows/public-ci.yml`](.github/workflows/public-ci.yml). It covers
source-manifest verification, secret scanning, SPDX/CycloneDX SBOMs, Python,
Buf, Rust, pure C ABI, .NET Native AOT, Java 17/Spring Boot 3 and 4, and an
immutable release handoff.

公开验收权威定义位于 [`.github/workflows/public-ci.yml`](.github/workflows/public-ci.yml)，
覆盖源码清单、凭据扫描、SPDX/CycloneDX SBOM、Python、Buf、Rust、纯 C ABI、.NET
Native AOT、Java 17/Spring Boot 3 与 4，以及不可变 release handoff。

Local entry points include:

```bash
python3 tools/ci/verify_source_manifest.py --root . --allow-git-metadata
cargo test --manifest-path contracts/rust/cyrene-plugin-contracts/Cargo.toml
dotnet test runtime/dotnet-native-aot/Cyrene.Provider.Tests/Cyrene.Provider.Tests.csproj --configuration Release
mvn --batch-mode --no-transfer-progress clean verify -f sdk/java/pom.xml
```

## Policy / 策略

- [Licensing map / 授权映射](LICENSING.md)
- [Security policy / 安全策略](SECURITY.md)
- [Contribution guide / 贡献指南](CONTRIBUTING.md)
- [Repository policy / 仓库策略](repository-policy.yaml)
