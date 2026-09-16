# Cyrene Plugin C# Native AOT Shared Library & Host (`dotnet-native-aot`)

## 1. Overview / 概述

This directory contains the C# implementation of Milestone M2 tasks (T26 & T27). It provides:
1. `Cyrene.Plugin.Core`: Shared business logic, models, and interfaces.
2. `Cyrene.Plugin.NativeLib`: Native AOT unmanaged shared library (`dotnet_cyrene_plugin.so`) exposing the canonical C ABI v1 functions with explicit `[UnmanagedCallersOnly]`.
3. `Cyrene.Plugin.Host`: Standalone C# host executable demonstrating direct consumption of Core without ABI shims.

本目录包含 M2 规范中针对 C# 的实现 (T26 与 T27)，由共享 Core、Native AOT 非托管动态库 (`dotnet_cyrene_plugin.so`) 以及独立 C# Host 宿主项目构成。

## 3. OneBot v11 Native AOT slice / OneBot v11 Native AOT 阶段实现

`Cyrene.OneBot.V11.Host` now exposes the canonical `DirectPluginRuntime` gRPC
service and selects one binding from the activation environment. The first
functional profile is `http_api`; `forward_websocket` and `reverse_websocket`
remain explicit registry entries for the next transport slice. The production
Python manifest is intentionally unchanged until cross-transport behavior and
package assembly reach parity.

`Cyrene.OneBot.V11.Host` 现在暴露规范的 `DirectPluginRuntime` gRPC 服务，并从
插件激活环境选择一个 binding。当前第一个可运行 profile 是 `http_api`；
`forward_websocket` 与 `reverse_websocket` 已保留为显式注册项，待下一传输阶段实现。
在跨传输行为和包组装达到等价前，正式 Python 清单保持不变。

For a local configured-host smoke test, provide:

```text
CYRENE_CAPABILITY_BINDING_ID=qq-main
CYRENE_CAPABILITY_CONFIGURATION_JSON={"http_base_url":"http://127.0.0.1:18080","self_account_id":"10001"}
```

The runtime accepts the existing canonical `message.connector.v1/send_message`
protobuf request and maps private/group conversations plus reply, text,
mention, image, and file parts to OneBot v11 actions. It never logs or includes
the access token in protocol errors.

运行时接受现有的 `message.connector.v1/send_message` protobuf 请求，并将私聊/群聊、
回复、文本、提及、图片和文件片段映射到 OneBot v11 action；协议错误不会记录或包含
access token。

## 2. Invariants & Implementation Details / 核心不变量与实现细节

1. **Three-Tier Architecture (T27)**: Separates C# Host and Native Library into distinct projects referencing the shared `Cyrene.Plugin.Core`.
2. **Explicit `UnmanagedCallersOnly` (T26)**: All 7 ABI entrypoints (`cyrene_plugin_*_v1`) are marked with explicit entrypoint names and Cdecl calling conventions.
3. **Exception Containment (T22)**: Every unmanaged entrypoint wraps all operations in `try / catch (Exception)`. Zero managed exceptions escape across the unmanaged boundary.
4. **Non-managed Memory Tracking (T21, T29)**: Uses `NativeMemory.Alloc` and `NativeMemory.Free` tracked by an instance allocation set to guard against double-frees and foreign pointer frees.
