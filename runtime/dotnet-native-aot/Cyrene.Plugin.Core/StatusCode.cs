// ┌─────────────────────────────────────────────────────────────────────────┐
// │ 📄 StatusCode.cs                                                        │
// │ Namespace: Cyrene.Plugin.Core                                           │
// │ Role: Canonical status codes matching DirectPluginRuntime & C ABI.      │
// │                                                                         │
// │ 模块职责：规范状态码定义，严格对齐 DirectPluginRuntime 与 C ABI              │
// └─────────────────────────────────────────────────────────────────────────┘

namespace Cyrene.Plugin.Core;

/// <summary>
/// Status codes representing execution outcomes across the C ABI boundary.
/// </summary>
public enum StatusCode : int
{
    Ok = 0,
    Cancelled = 1,
    Unknown = 2,
    InvalidArgument = 3,
    DeadlineExceeded = 4,
    NotFound = 5,
    AlreadyExists = 6,
    PermissionDenied = 7,
    ResourceExhausted = 8,
    FailedPrecondition = 9,
    Aborted = 10,
    OutOfRange = 11,
    Unimplemented = 12,
    Internal = 13,
    Unavailable = 14,
    DataLoss = 15,
    Unauthenticated = 16,
}
