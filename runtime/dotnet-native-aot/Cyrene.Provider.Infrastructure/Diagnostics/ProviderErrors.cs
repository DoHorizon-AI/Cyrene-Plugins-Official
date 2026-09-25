// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 ProviderErrors.cs                                               │
// │  Namespace: Cyrene.Provider.Infrastructure.Diagnostics              │
// │  Role: Error mapping, status codes, and exceptions (T82).           │
// └─────────────────────────────────────────────────────────────────────┘
// 中文：文件：ProviderErrors.cs
// 中文：命名空间：Cyrene.Provider.Infrastructure.Diagnostics
// 中文：职责：错误映射、状态码与异常处理（T82）。

namespace Cyrene.Provider.Infrastructure.Diagnostics;

public enum ProviderErrorCode
{
    None = 0,
    AuthenticationFailed = 1,
    RateLimitExceeded = 2,
    DeadlineExceeded = 3,
    MalformedResponse = 4,
    ServiceUnavailable = 5,
    ModelNotFound = 6,
    WireProtocolViolation = 7
}

public class ProviderException : Exception
{
    public ProviderErrorCode ErrorCode { get; }
    public int? HttpStatusCode { get; }
    public int? RetryAfterSeconds { get; }
    public bool Retryable => ErrorCode is ProviderErrorCode.RateLimitExceeded or ProviderErrorCode.ServiceUnavailable;

    public ProviderException(
        ProviderErrorCode errorCode,
        string message,
        int? httpStatusCode = null,
        int? retryAfterSeconds = null,
        Exception? innerException = null
    ) : base(message, innerException)
    {
        ErrorCode = errorCode;
        HttpStatusCode = httpStatusCode;
        RetryAfterSeconds = retryAfterSeconds;
    }
}

public static class ErrorMapper
{
    public static ProviderException MapHttpError(
        int statusCode,
        string rawBody,
        int? retryAfterSeconds = null
    )
    {
        var errorCode = statusCode switch
        {
            401 or 403 => ProviderErrorCode.AuthenticationFailed,
            429 => ProviderErrorCode.RateLimitExceeded,
            404 => ProviderErrorCode.ModelNotFound,
            504 or 408 => ProviderErrorCode.DeadlineExceeded,
            >= 500 => ProviderErrorCode.ServiceUnavailable,
            _ => ProviderErrorCode.WireProtocolViolation
        };

        var message = $"Vendor HTTP {statusCode}: {rawBody}";
        return new ProviderException(errorCode, message, statusCode, retryAfterSeconds);
    }
}
