// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 SecurityComponents.cs                                           │
// │  Namespace: Cyrene.Provider.Infrastructure.Security                 │
// │  Role: Redaction sanitizer and injected binding config (T82, T87).  │
// └─────────────────────────────────────────────────────────────────────┘

using System.Text.RegularExpressions;

namespace Cyrene.Provider.Infrastructure.Security;

public static class RedactionSanitizer
{
    private static readonly Regex BearerRegex = new(@"Bearer\s+[a-zA-Z0-9_\-\.]+", RegexOptions.Compiled);
    private static readonly Regex ApiKeyRegex = new(@"(sk-[a-zA-Z0-9_\-]{8,})", RegexOptions.Compiled);
    private static readonly Regex QueryKeyRegex = new(@"([?&](?:api[_-]?key|token|secret)=)[^&]+", RegexOptions.Compiled | RegexOptions.IgnoreCase);

    public static string Redact(string? input)
    {
        if (string.IsNullOrEmpty(input))
        {
            return string.Empty;
        }

        var result = BearerRegex.Replace(input, "Bearer [REDACTED]");
        result = ApiKeyRegex.Replace(result, "sk-[REDACTED]");
        result = QueryKeyRegex.Replace(result, "$1[REDACTED]");
        return result;
    }
}

public sealed class ProviderBindingConfiguration
{
    public Uri EndpointUri { get; }
    public string ApiKey { get; }
    public string? OrganizationId { get; }
    public TimeSpan Timeout { get; }

    public ProviderBindingConfiguration(
        Uri endpointUri,
        string apiKey,
        string? organizationId = null,
        TimeSpan? timeout = null
    )
    {
        EndpointUri = endpointUri ?? throw new ArgumentNullException(nameof(endpointUri));
        ApiKey = string.IsNullOrWhiteSpace(apiKey) ? throw new ArgumentException("ApiKey cannot be empty", nameof(apiKey)) : apiKey;
        OrganizationId = organizationId;
        Timeout = timeout ?? TimeSpan.FromSeconds(60);
    }

    // Explicitly prohibit printing raw credentials in string representation or logs (T87)
    public override string ToString()
    {
        return $"ProviderBindingConfiguration(Endpoint={EndpointUri}, OrganizationId={OrganizationId ?? "none"}, ApiKey=REDACTED, Timeout={Timeout.TotalSeconds}s)";
    }
}
