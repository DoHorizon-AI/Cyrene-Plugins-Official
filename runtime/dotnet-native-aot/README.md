# Cyrene Plugin C# Native AOT Shared Library & Host (`dotnet-native-aot`)

## 1. Overview / 概述

This directory contains the C# implementation of Milestone M2 tasks (T26 & T27). It provides:
1. `Cyrene.Plugin.Core`: Shared business logic, models, and interfaces.
2. `Cyrene.Plugin.NativeLib`: Native AOT unmanaged shared library (`dotnet_cyrene_plugin.so`) exposing the canonical C ABI v1 functions with explicit `[UnmanagedCallersOnly]`.
3. `Cyrene.Plugin.Host`: Standalone C# host executable demonstrating direct consumption of Core without ABI shims.

本目录包含 M2 规范中针对 C# 的实现 (T26 与 T27)，由共享 Core、Native AOT 非托管动态库 (`dotnet_cyrene_plugin.so`) 以及独立 C# Host 宿主项目构成。

## 2. Invariants & Implementation Details / 核心不变量与实现细节

1. **Three-Tier Architecture (T27)**: Separates C# Host and Native Library into distinct projects referencing the shared `Cyrene.Plugin.Core`.
2. **Explicit `UnmanagedCallersOnly` (T26)**: All 7 ABI entrypoints (`cyrene_plugin_*_v1`) are marked with explicit entrypoint names and Cdecl calling conventions.
3. **Exception Containment (T22)**: Every unmanaged entrypoint wraps all operations in `try / catch (Exception)`. Zero managed exceptions escape across the unmanaged boundary.
4. **Non-managed Memory Tracking (T21, T29)**: Uses `NativeMemory.Alloc` and `NativeMemory.Free` tracked by an instance allocation set to guard against double-frees and foreign pointer frees.
