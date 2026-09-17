# Cyrene Plugin C# Native AOT Shared Library & Host (`dotnet-native-aot`)

## 1. Overview / 概述

This directory contains the C# implementation of Milestone M2 tasks (T26 & T27). It provides:
1. `Cyrene.Plugin.Core`: Shared business logic, models, and interfaces.
2. `Cyrene.Plugin.NativeLib`: Native AOT unmanaged shared library (`dotnet_cyrene_plugin.so`) exposing the canonical C ABI v1 functions with explicit `[UnmanagedCallersOnly]`.
3. `Cyrene.Plugin.Host`: Standalone C# host executable demonstrating direct consumption of Core without ABI shims.

本目录包含 M2 规范中针对 C# 的实现 (T26 与 T27)，由共享 Core、Native AOT 非托管动态库 (`dotnet_cyrene_plugin.so`) 以及独立 C# Host 宿主项目构成。

## 3. OneBot v11 Native AOT slice / OneBot v11 Native AOT 阶段实现

`Cyrene.OneBot.V11.Host` exposes the canonical `DirectPluginRuntime` gRPC
service and selects one binding from the activation environment. The formal
candidate package is now the C# Native AOT runtime (`0.3.0`); the Python
`0.2.0` implementation is retained only as a separately assembled rollback
artifact and behavior reference. The supported generic profiles are
`http_api`, `forward_websocket`, and `reverse_websocket`.

The current C# slice also contains a binding-scoped `qqnt-direct` bridge for
the fixed `qq.client.v1` operation registry. It starts the explicitly selected
Linux x86_64 QQ Host over inherited stdio, negotiates protocol/version/ABI,
correlates requests by binding and generation, bounds frames, and performs
bounded shutdown. Session bootstrap, QQ canonical message/event projections,
and crash recovery gates are exercised by the Native AOT candidate package;
authorized real-QQ smoke remains a separate acceptance record.

`Cyrene.OneBot.V11.Host` 现在暴露规范的 `DirectPluginRuntime` gRPC 服务，并从
插件激活环境选择一个 binding。当前第一个可运行 profile 是 `http_api`；
`forward_websocket` 与 `reverse_websocket` 也已实现。reverse profile 在 binding
本地监听器上完成受控 RFC 6455 握手，并将单个当前对端交给同一套 action/echo
关联循环。正式候选包已切换到 C# Native AOT；Python 0.2.0 仅作为独立回滚制品保留。

当前正式候选也包含限定在 binding 内的 `qqnt-direct` 桥接和固定
`qq.client.v1` operation 注册表：通过继承 stdio 启动明确选择的 Linux x86_64 QQ Host，
协商 protocol/version/ABI，按 binding 与 generation 关联请求，有界分帧并执行有界关闭。
session bootstrap、QQ canonical message/event 投影和崩溃恢复门禁已纳入 Native AOT
候选包；真实授权 QQ smoke 仍单独记录，不由 fake Host 代替。

For a local configured-host smoke test, provide:

```text
CYRENE_CAPABILITY_BINDING_ID=qq-main
CYRENE_CAPABILITY_CONFIGURATION_JSON={"http_base_url":"http://127.0.0.1:18080","self_account_id":"10001"}
```

For a forward-WebSocket profile, use `websocket_url` and
`transport_profile=forward_websocket`. For a reverse-WebSocket profile, use
`transport_profile=reverse_websocket`, `reverse_listen_host`, and
`reverse_listen_port`; the peer connects to that binding-local listener and
must provide `Authorization: Bearer <access_token>` when a token is configured.

对于 forward WebSocket profile，配置 `websocket_url` 与
`transport_profile=forward_websocket`。对于 reverse WebSocket profile，配置
`transport_profile=reverse_websocket`、`reverse_listen_host` 与
`reverse_listen_port`；对端连接该 binding 独立监听器，配置 token 时必须提供
`Authorization: Bearer <access_token>`。

The runtime accepts the existing canonical `message.connector.v1/send_message`
protobuf request and maps private/group conversations plus reply, text,
mention, image, and file parts to OneBot v11 actions. It never logs or includes
the access token in protocol errors. `InvokeStream` also accepts the existing
subscription shape (`method=events`, the canonical `Filter` type URL, and
`DIRECT_STREAM_MODE_SUBSCRIPTION`) and emits bounded `DirectPayload` items for
normalized `inbound_message` and `inbound_request` events.

The unary `message.connector.v1/respond_request` operation is also mapped to
OneBot v11 `set_friend_add_request` and `set_group_add_request` actions. The
mapping preserves opaque request flags, normalized friend/group-invite kinds,
approve/reject decisions, bounded comments, and optional canonical vendor
request facts. The formal plugin manifest points to the Native AOT executable;
the Python manifest snapshot is kept under the rollback artifact directory and
is not a dual-runtime fallback.

运行时接受现有的 `message.connector.v1/send_message` protobuf 请求，并将私聊/群聊、
回复、文本、提及、图片和文件片段映射到 OneBot v11 action；协议错误不会记录或包含
access token。`InvokeStream` 也接受现有订阅形态（`method=events`、规范 `Filter`
type URL 与 `DIRECT_STREAM_MODE_SUBSCRIPTION`），并以有界 `DirectPayload` 流输出规范化的
`inbound_message` 与 `inbound_request` 事件。

一元 `message.connector.v1/respond_request` 操作也已映射到 OneBot v11 的
`set_friend_add_request` 与 `set_group_add_request` action。该映射保留不透明请求 flag、
规范化的好友/群邀请类型、同意/拒绝决策、有界评论以及可选的规范 vendor 请求事实。
正式插件清单已指向 Native AOT 可执行文件；Python 清单快照仅保留在回滚制品目录中，
不构成双运行时 fallback。

## 2. Invariants & Implementation Details / 核心不变量与实现细节

1. **Three-Tier Architecture (T27)**: Separates C# Host and Native Library into distinct projects referencing the shared `Cyrene.Plugin.Core`.
2. **Explicit `UnmanagedCallersOnly` (T26)**: All 7 ABI entrypoints (`cyrene_plugin_*_v1`) are marked with explicit entrypoint names and Cdecl calling conventions.
3. **Exception Containment (T22)**: Every unmanaged entrypoint wraps all operations in `try / catch (Exception)`. Zero managed exceptions escape across the unmanaged boundary.
4. **Non-managed Memory Tracking (T21, T29)**: Uses `NativeMemory.Alloc` and `NativeMemory.Free` tracked by an instance allocation set to guard against double-frees and foreign pointer frees.
