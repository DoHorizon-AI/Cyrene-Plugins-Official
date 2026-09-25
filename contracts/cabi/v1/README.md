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
---

<!-- Chinese Translation / 中文翻译 -->

## 中文翻译

# Cyrene Plugin C ABI v1 规范

## 1. 概述

本目录定义 Cyrene Plugin 稳定且版本化的 C 应用程序二进制接口（ABI）。它提供非托管边界，使 Rust、C#（Native AOT）、C/C++ 与外部 host 能够进行跨语言二进制互操作。

## 2. 不变量

1. **定宽类型和不透明句柄**：跨越边界的类型仅限标准定宽类型（uint8_t、int32_t、uint32_t、uint64_t）和不透明指针（cyrene_plugin_handle_t、cyrene_cancel_handle_t）。
2. **Type URL 和 Protobuf 字节**：ABI 只传输 type_url 和序列化后的 Protobuf 字节；严禁传输语言专用对象图或集合布局。
3. **分配器所有权**：内存必须由分配它的一侧释放。Plugin 分配的 buffer 必须通过 cyrene_plugin_free_buffer_v1 释放。
4. **不允许未捕获的异常或 panic**：Rust panic、C++ exception 或 C# exception 绝不能越过 ABI 边界；所有入口点失败时都必须失败关闭，并返回类型化的 cyrene_status_code_v1_t。
5. **有序流式传输和终止事件**：stream callback 接收单调递增的序列号（0、1、2……），并且恰好以一个终止事件结束。
6. **三轴版本协商**：ABI 版本、capability ID/版本与 interface 版本通过 cyrene_plugin_get_api_v1 分别协商。

## 3. 包含的文件

| 文件 | 语言 | 职责 |
| --- | --- | --- |
| cyrene_plugin_abi_v1.h | C99/C11/C23 | 规范 C ABI v1 函数原型、状态码、结构体和内存所有权契约。 |
| cyrene_plugin_abi.h | 符号链接 | 指向 cyrene_plugin_abi_v1.h 的符号链接别名。 |
| README.md | Markdown | 双语架构概览和不变量。 |

## 4. 导出接口列表

- cyrene_plugin_get_api_v1(requested_abi_version, out_info)
- cyrene_plugin_create_v1(options, out_handle)
- cyrene_plugin_invoke_v1(handle, request, out_response)
- cyrene_plugin_invoke_stream_v1(handle, request, callback, user_data, out_cancel_handle)
- cyrene_plugin_cancel_v1(handle, cancel_handle)
- cyrene_plugin_free_buffer_v1(handle, buffer)
- cyrene_plugin_destroy_v1(handle)
