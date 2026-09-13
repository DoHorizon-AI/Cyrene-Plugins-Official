# Cyrene Plugin C ABI Rust Shared Library (`cyrene-plugin-cabi`)

## 1. Overview / 概述

This crate publishes the native C ABI shared library (`librust_cyrene_plugin.so` / `cdylib`) implementing Milestone M2 tasks (T19–T25). It bridges native Rust capability implementations with unmanaged C ABI callers.

本 Crate 发布符合 M2 规范 (T19–T25) 的原生 C ABI 共享库 (`librust_cyrene_plugin.so` / `cdylib`)，连接 Rust 原生能力实现与非托管 C ABI 调用方。

## 2. Invariants & Implementation Details / 核心不变量与实现细节

1. **Explicit C ABI Symbols**: Exports the canonical 7 symbols:
   - `cyrene_plugin_get_api_v1`
   - `cyrene_plugin_create_v1`
   - `cyrene_plugin_invoke_v1`
   - `cyrene_plugin_invoke_stream_v1`
   - `cyrene_plugin_cancel_v1`
   - `cyrene_plugin_free_buffer_v1`
   - `cyrene_plugin_destroy_v1`
2. **Panic Containment**: Every exported entrypoint is wrapped in `std::panic::catch_unwind(AssertUnwindSafe(...))`. No Rust panic ever unwinds across the ABI.
3. **Buffer Tracking & Memory Safety**: Allocations returned to callers prepend an internal `AllocHeader` with magic verification and are tracked in an instance ledger. Double-free attempts and foreign pointer frees return `CYRENE_STATUS_INVALID_ARGUMENT` gracefully.
4. **Streaming, Cancellation & Ordering**: Streams deliver monotonically ordered chunks (`0, 1, 2...`) terminating with an explicit terminal event. Cancellation tokens can be triggered asynchronously from any thread.
