# Cyrene Plugin C ABI v1 Specification / Cyrene 插件 C ABI v1 规范

## 1. Overview / 概述

This directory defines the stable, versioned C Application Binary Interface (ABI) for Cyrene plugins. It provides an unmanaged boundary enabling cross-language binary interoperability among Rust, C# (Native AOT), C/C++, and foreign hosts.

本目录定义了 Cyrene 插件的稳定版本化 C 应用程序二进制接口（ABI）。通过提供非托管二进制边界，支持 Rust、C#（Native AOT）、C/C++ 以及外部宿主环境之间的高效互操作。

## 2. Invariants / 核心不变量

1. **Fixed-Width Types & Opaque Handles**: Only standard fixed-width types (`uint8_t`, `int32_t`, `uint32_t`, `uint64_t`) and opaque pointers (`cyrene_plugin_handle_t`, `cyrene_cancel_handle_t`) cross the boundary.
2. **Type URL + Protobuf Bytes**: Only `type_url` and serialized Protobuf bytes are transferred across the ABI; language-specific object graphs and collection layouts are strictly forbidden.
3. **Allocator Ownership**: Memory must be released by the side that allocated it. Plugin-allocated buffers must be freed via `cyrene_plugin_free_buffer_v1`.
4. **Zero Uncaught Exceptions/Panics**: Rust panics, C++ exceptions, or C# exceptions must never cross the ABI boundary; all entrypoints fail-closed returning typed `cyrene_status_code_v1_t`.
5. **Ordered Streaming & Terminal Events**: Stream callbacks receive monotonic sequence numbers (`0, 1, 2...`) terminating with exactly one terminal event.
6. **Triple-Axis Version Negotiation**: ABI version, capability IDs/versions, and interface versions are negotiated independently via `cyrene_plugin_get_api_v1`.

## 3. Contained Files / 包含文件

| File | Language | Responsibility / 职责 |
| --- | --- | --- |
| `cyrene_plugin_abi_v1.h` | C99/C11/C23 | Canonical C ABI v1 function prototypes, status codes, structs, and memory ownership contracts. |
| `cyrene_plugin_abi.h` | Symlink | Symlink alias pointing to `cyrene_plugin_abi_v1.h`. |
| `README.md` | Markdown | Bilingual architectural overview and invariants. |

## 4. Function Surface / 接口导出列表

- `cyrene_plugin_get_api_v1(requested_abi_version, out_info)`
- `cyrene_plugin_create_v1(options, out_handle)`
- `cyrene_plugin_invoke_v1(handle, request, out_response)`
- `cyrene_plugin_invoke_stream_v1(handle, request, callback, user_data, out_cancel_handle)`
- `cyrene_plugin_cancel_v1(handle, cancel_handle)`
- `cyrene_plugin_free_buffer_v1(handle, buffer)`
- `cyrene_plugin_destroy_v1(handle)`
