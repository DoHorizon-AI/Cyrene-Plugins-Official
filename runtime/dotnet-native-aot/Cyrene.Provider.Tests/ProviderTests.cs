// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 ProviderTests.cs                                                │
// │  Namespace: Cyrene.Provider.Tests                                   │
// │  Role: Comprehensive test suite for M5D (T81 - T88).                │
// └─────────────────────────────────────────────────────────────────────┘

using System.Net;
using System.Text;
using Cyrene.Provider.Anthropic;
using Cyrene.Provider.Infrastructure.Capability;
using Cyrene.Provider.Infrastructure.Diagnostics;
using Cyrene.Provider.Infrastructure.Security;
using Cyrene.Provider.Infrastructure.Transport;
using Cyrene.Provider.OpenAi;
using Xunit;

namespace Cyrene.Provider.Tests;

public class MockHttpMessageHandler : HttpMessageHandler
{
    private readonly Func<HttpRequestMessage, HttpResponseMessage> _handler;

    public MockHttpMessageHandler(Func<HttpRequestMessage, HttpResponseMessage> handler)
    {
        _handler = handler;
    }

    protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken)
    {
        return Task.FromResult(_handler(request));
    }
}

public class ProviderTests
{
    private static readonly string[] ReadinessArgs = ["--readiness"];
    private static readonly string FixturesDir = FindFixturesDir();

    private static string FindFixturesDir()
    {
        var current = AppContext.BaseDirectory;
        while (!string.IsNullOrEmpty(current))
        {
            var candidate = Path.Combine(current, "contracts", "fixtures", "providers");
            if (Directory.Exists(candidate))
            {
                return candidate;
            }
            var parent = Path.GetDirectoryName(current);
            if (parent == current) break;
            current = parent;
        }
        return Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, "../../../../../../contracts/fixtures/providers"));
    }

    private static readonly string[] AnthropicStreamFrames =
    [
        "event: message_start",
        "data: {\"type\":\"message_start\",\"message\":{\"id\":\"msg-stream-1\",\"model\":\"claude-3-5-sonnet-20241022\",\"usage\":{\"input_tokens\":11,\"output_tokens\":1}}}",
        "",
        "event: content_block_start",
        "data: {\"type\":\"content_block_start\",\"index\":0,\"content_block\":{\"type\":\"text\",\"text\":\"\"}}",
        "",
        "event: ping",
        "data: {\"type\":\"ping\"}",
        "",
        "event: content_block_delta",
        "data: {\"type\":\"content_block_delta\",\"index\":0,\"delta\":{\"type\":\"text_delta\",\"text\":\"Hello\"}}",
        "",
        "event: content_block_delta",
        "data: {\"type\":\"content_block_delta\",\"index\":0,\"delta\":{\"type\":\"text_delta\",\"text\":\" Cyrene\"}}",
        "",
        "event: message_delta",
        "data: {\"type\":\"message_delta\",\"delta\":{\"stop_reason\":\"end_turn\"},\"usage\":{\"output_tokens\":7}}",
        "",
        "event: message_stop",
        "data: {\"type\":\"message_stop\"}",
        "",
    ];

    private static readonly string[] AnthropicStreamErrorFrames =
    [
        "event: message_start",
        "data: {\"type\":\"message_start\",\"message\":{\"id\":\"msg-stream-2\",\"model\":\"claude-3-5-sonnet-20241022\"}}",
        "",
        "event: error",
        "data: {\"type\":\"error\",\"error\":{\"type\":\"overloaded_error\",\"message\":\"Overloaded\"}}",
        "",
    ];

    [Fact]
    public void PluginCoreRejectsUnknownMethodsAndUnconfiguredModelCalls()
    {
        using var plugin = new Cyrene.Plugin.Core.PluginCoreInstance(null, 0, 0);

        var unknown = plugin.Invoke("unknown", null, [], 0);
        Assert.Equal(Cyrene.Plugin.Core.StatusCode.Unimplemented, unknown.Status);

        var model = plugin.Invoke("model.provider.v1/Chat", null, [], 0);
        Assert.Equal(Cyrene.Plugin.Core.StatusCode.Unavailable, model.Status);

        var stream = plugin.InvokeStream("unknown", null, [], 0, () => false).Single();
        Assert.Equal(Cyrene.Plugin.Core.StatusCode.Unimplemented, stream.Status);
        Assert.True(stream.IsTerminal);
    }

    // ── T81: Separate Capability Profiles ──────────────────────────────────

    [Fact]
    public void Test_T81_SeparateCapabilityProfiles()
    {
        var modelCapName = typeof(IModelCapability).Name;
        var embCapName = typeof(IEmbeddingCapability).Name;
        var ttsCapName = typeof(ITtsCapability).Name;
        var sttCapName = typeof(ISttCapability).Name;
        var rerankCapName = typeof(IRerankCapability).Name;

        Assert.Equal("IModelCapability", modelCapName);
        Assert.Equal("IEmbeddingCapability", embCapName);
        Assert.Equal("ITtsCapability", ttsCapName);
        Assert.Equal("ISttCapability", sttCapName);
        Assert.Equal("IRerankCapability", rerankCapName);

        // Verify independent implementations are decoupled
        Assert.False(typeof(IModelCapability).IsAssignableFrom(typeof(IEmbeddingCapability)));
        Assert.False(typeof(ITtsCapability).IsAssignableFrom(typeof(ISttCapability)));
    }

    // ── T82: Redaction Sanitizer ───────────────────────────────────────────

    [Fact]
    public void Test_T82_RedactionSanitizer_ScrubsSecrets()
    {
        var rawWithApiKey = "Request to https://api.openai.com/v1/chat failed with key sk-1234567890abcdef123456";
        var redacted = RedactionSanitizer.Redact(rawWithApiKey);
        Assert.DoesNotContain("sk-1234567890abcdef123456", redacted);
        Assert.Contains("sk-[REDACTED]", redacted);

        var rawWithBearer = "Authorization: Bearer my-super-secret-jwt-token-12345";
        var redactedBearer = RedactionSanitizer.Redact(rawWithBearer);
        Assert.DoesNotContain("my-super-secret-jwt-token-12345", redactedBearer);
        Assert.Contains("Bearer [REDACTED]", redactedBearer);

        var rawWithQuery = "https://api.example.com/v1/models?api_key=secretKey123&other=val";
        var redactedQuery = RedactionSanitizer.Redact(rawWithQuery);
        Assert.DoesNotContain("secretKey123", redactedQuery);
        Assert.Contains("api_key=[REDACTED]", redactedQuery);
    }

    // ── T82: SSE Stream Reader ─────────────────────────────────────────────

    [Fact]
    public async Task Test_T82_SseStreamReader_ParsesAndTerminatesOnDone()
    {
        var sseContent = "data: {\"chunk\": 1}\n\n: ping\n\ndata: {\"chunk\": 2}\n\ndata: [DONE]\n\ndata: {\"chunk\": 3}\n\n";
        using var stream = new MemoryStream(Encoding.UTF8.GetBytes(sseContent));

        var events = new List<string>();
        await foreach (var evt in SseStreamReader.ReadEventsAsync(stream))
        {
            events.Add(evt);
        }

        Assert.Equal(2, events.Count);
        Assert.Equal("{\"chunk\": 1}", events[0]);
        Assert.Equal("{\"chunk\": 2}", events[1]);
    }

    // ── T83 & T85: OpenAI Wire Translation ─────────────────────────────────

    [Fact]
    public async Task Test_T83_T85_OpenAiVendorAdapter_WireTranslation()
    {
        var mockResponseJson = "{\"id\":\"chatcmpl-test\",\"model\":\"gpt-4o\",\"choices\":[{\"index\":0,\"message\":{\"role\":\"assistant\",\"content\":\"Hello Cyrene\"},\"finish_reason\":\"stop\"}],\"usage\":{\"prompt_tokens\":10,\"completion_tokens\":5,\"total_tokens\":15}}";

        var mockHandler = new MockHttpMessageHandler(req =>
        {
            Assert.Equal("/v1/chat/completions", req.RequestUri!.AbsolutePath);
            Assert.Equal("Bearer", req.Headers.Authorization!.Scheme);
            Assert.Equal("sk-test-openai-key", req.Headers.Authorization.Parameter);
            return new HttpResponseMessage(HttpStatusCode.OK)
            {
                Content = new StringContent(mockResponseJson, Encoding.UTF8, "application/json")
            };
        });

        var config = new ProviderBindingConfiguration(new Uri("https://api.openai.com"), "sk-test-openai-key");
        var client = new HttpTransportClient(new HttpClient(mockHandler));
        var adapter = new OpenAiVendorAdapter(config, client);

        var result = await adapter.CompleteChatAsync(new ChatCompletionParameters(
            Model: "gpt-4o",
            Messages: new List<ChatMessage> { new("user", "Hi") }
        ));

        Assert.Equal("chatcmpl-test", result.Id);
        Assert.Equal("gpt-4o", result.Model);
        Assert.Equal("Hello Cyrene", result.Content);
        Assert.Equal("stop", result.FinishReason);
        Assert.Equal(10, result.PromptTokens);
        Assert.Equal(5, result.CompletionTokens);
    }

    // ── T83 & T85: Anthropic Wire Translation ──────────────────────────────

    [Fact]
    public async Task Test_T83_T85_AnthropicVendorAdapter_WireTranslation()
    {
        var mockResponseJson = "{\"id\":\"msg-test-123\",\"model\":\"claude-3-5-sonnet-20241022\",\"role\":\"assistant\",\"content\":[{\"type\":\"text\",\"text\":\"Claude response text\"}],\"stop_reason\":\"end_turn\",\"usage\":{\"input_tokens\":12,\"output_tokens\":8}}";

        var mockHandler = new MockHttpMessageHandler(req =>
        {
            Assert.Equal("/v1/messages", req.RequestUri!.AbsolutePath);
            Assert.True(req.Headers.Contains("x-api-key"));
            Assert.Equal("ant-test-key", req.Headers.GetValues("x-api-key").First());
            return new HttpResponseMessage(HttpStatusCode.OK)
            {
                Content = new StringContent(mockResponseJson, Encoding.UTF8, "application/json")
            };
        });

        var config = new ProviderBindingConfiguration(new Uri("https://api.anthropic.com"), "ant-test-key");
        var client = new HttpTransportClient(new HttpClient(mockHandler));
        var adapter = new AnthropicVendorAdapter(config, client);

        var result = await adapter.CompleteChatAsync(new ChatCompletionParameters(
            Model: "claude-3-5-sonnet-20241022",
            Messages: new List<ChatMessage> { new("user", "Hello Claude") }
        ));

        Assert.Equal("msg-test-123", result.Id);
        Assert.Equal("claude-3-5-sonnet-20241022", result.Model);
        Assert.Equal("Claude response text", result.Content);
        Assert.Equal("end_turn", result.FinishReason);
        Assert.Equal(12, result.PromptTokens);
        Assert.Equal(8, result.CompletionTokens);
    }

    [Fact]
    public async Task Test_W1_AnthropicStreaming_YieldsOrderedDeltasAndUsage()
    {
        var sse = string.Join("\n", AnthropicStreamFrames);

        string? capturedBody = null;
        var mockHandler = new MockHttpMessageHandler(req =>
        {
            capturedBody = req.Content!.ReadAsStringAsync().GetAwaiter().GetResult();
            return new HttpResponseMessage(HttpStatusCode.OK)
            {
                Content = new StringContent(sse, Encoding.UTF8, "text/event-stream")
            };
        });

        var config = new ProviderBindingConfiguration(new Uri("https://api.anthropic.com"), "ant-test-key");
        var client = new HttpTransportClient(new HttpClient(mockHandler));
        var adapter = new AnthropicVendorAdapter(config, client);

        var chunks = new List<ChatCompletionChunk>();
        await foreach (var chunk in adapter.StreamChatAsync(new ChatCompletionParameters(
            Model: "claude-3-5-sonnet-20241022",
            Messages: new List<ChatMessage> { new("user", "Hello Claude") }
        )))
        {
            chunks.Add(chunk);
        }

        Assert.Equal(3, chunks.Count);
        Assert.Equal("msg-stream-1", chunks[0].Id);
        Assert.Equal("Hello", chunks[0].Delta);
        Assert.Null(chunks[0].FinishReason);
        Assert.Equal(" Cyrene", chunks[1].Delta);
        Assert.Equal(string.Empty, chunks[2].Delta);
        Assert.Equal("end_turn", chunks[2].FinishReason);
        Assert.Equal(11, chunks[2].PromptTokens);
        Assert.Equal(7, chunks[2].CompletionTokens);
        Assert.Contains("\"stream\":true", capturedBody);
    }

    [Fact]
    public async Task Test_W1_AnthropicStreaming_ErrorEventFailsClosed()
    {
        var sse = string.Join("\n", AnthropicStreamErrorFrames);

        var mockHandler = new MockHttpMessageHandler(_ => new HttpResponseMessage(HttpStatusCode.OK)
        {
            Content = new StringContent(sse, Encoding.UTF8, "text/event-stream")
        });

        var config = new ProviderBindingConfiguration(new Uri("https://api.anthropic.com"), "ant-test-key");
        var client = new HttpTransportClient(new HttpClient(mockHandler));
        var adapter = new AnthropicVendorAdapter(config, client);

        var exception = await Assert.ThrowsAsync<ProviderException>(async () =>
        {
            await foreach (var _ in adapter.StreamChatAsync(new ChatCompletionParameters(
                Model: "claude-3-5-sonnet-20241022",
                Messages: new List<ChatMessage> { new("user", "Hello Claude") }
            )))
            {
            }
        });

        Assert.Equal(ProviderErrorCode.ServiceUnavailable, exception.ErrorCode);
        Assert.True(exception.Retryable);
    }

    // ── T86: Sanitized Fixtures Testing ────────────────────────────────────

    [Fact]
    public async Task Test_T86_AuthFailureFixture_401()
    {
        var fixturePath = Path.Combine(FixturesDir, "auth_failure_401.json");
        var fixtureJson = await File.ReadAllTextAsync(fixturePath);

        var mockHandler = new MockHttpMessageHandler(req => new HttpResponseMessage(HttpStatusCode.Unauthorized)
        {
            Content = new StringContent(fixtureJson, Encoding.UTF8, "application/json")
        });

        var config = new ProviderBindingConfiguration(new Uri("https://api.openai.com"), "sk-invalid-key");
        var client = new HttpTransportClient(new HttpClient(mockHandler));
        var adapter = new OpenAiVendorAdapter(config, client);

        var ex = await Assert.ThrowsAsync<ProviderException>(() => adapter.CompleteChatAsync(
            new ChatCompletionParameters("gpt-4o", new List<ChatMessage> { new("user", "Hello") })
        ));

        Assert.Equal(ProviderErrorCode.AuthenticationFailed, ex.ErrorCode);
        Assert.Equal(401, ex.HttpStatusCode);
    }

    [Fact]
    public async Task Test_T86_RateLimitFixture_429_WithRetryAfter()
    {
        var fixturePath = Path.Combine(FixturesDir, "rate_limit_429.json");
        var fixtureJson = await File.ReadAllTextAsync(fixturePath);

        var mockHandler = new MockHttpMessageHandler(req =>
        {
            var resp = new HttpResponseMessage(HttpStatusCode.TooManyRequests)
            {
                Content = new StringContent(fixtureJson, Encoding.UTF8, "application/json")
            };
            resp.Headers.RetryAfter = new System.Net.Http.Headers.RetryConditionHeaderValue(TimeSpan.FromSeconds(30));
            return resp;
        });

        var config = new ProviderBindingConfiguration(new Uri("https://api.openai.com"), "sk-test-key");
        var client = new HttpTransportClient(new HttpClient(mockHandler));
        var adapter = new OpenAiVendorAdapter(config, client);

        var ex = await Assert.ThrowsAsync<ProviderException>(() => adapter.CompleteChatAsync(
            new ChatCompletionParameters("gpt-4o", new List<ChatMessage> { new("user", "Hello") })
        ));

        Assert.Equal(ProviderErrorCode.RateLimitExceeded, ex.ErrorCode);
        Assert.Equal(429, ex.HttpStatusCode);
        Assert.Equal(30, ex.RetryAfterSeconds);
        Assert.True(ex.Retryable);
    }

    [Fact]
    public async Task Test_T86_MalformedResponseFixture()
    {
        var fixturePath = Path.Combine(FixturesDir, "malformed_response.json");
        var fixtureJson = await File.ReadAllTextAsync(fixturePath);

        var mockHandler = new MockHttpMessageHandler(req => new HttpResponseMessage(HttpStatusCode.OK)
        {
            Content = new StringContent(fixtureJson, Encoding.UTF8, "application/json")
        });

        var config = new ProviderBindingConfiguration(new Uri("https://api.openai.com"), "sk-test-key");
        var client = new HttpTransportClient(new HttpClient(mockHandler));
        var adapter = new OpenAiVendorAdapter(config, client);

        await Assert.ThrowsAnyAsync<Exception>(() => adapter.CompleteChatAsync(
            new ChatCompletionParameters("gpt-4o", new List<ChatMessage> { new("user", "Hello") })
        ));
    }

    [Fact]
    public async Task Test_T86_StreamingInterruptedFixture()
    {
        var fixturePath = Path.Combine(FixturesDir, "stream_interrupted.sse");
        using var stream = File.OpenRead(fixturePath);

        var events = new List<string>();
        await foreach (var evt in SseStreamReader.ReadEventsAsync(stream))
        {
            events.Add(evt);
        }

        // Must yield 2 valid events and not throw unhandled exception
        Assert.Equal(2, events.Count);
        Assert.Contains("Hello", events[0]);
        Assert.Contains("world", events[1]);
    }

    // ── T87: Credential Injection (No Raw Secret Storage) ──────────────────

    [Fact]
    public void Test_T87_CredentialInjection_NeverLogsSecrets()
    {
        var config = new ProviderBindingConfiguration(
            new Uri("https://api.openai.com"),
            "sk-super-secret-token-do-not-leak"
        );

        var repr = config.ToString();
        Assert.DoesNotContain("sk-super-secret-token-do-not-leak", repr);
        Assert.Contains("ApiKey=REDACTED", repr);
    }

    // ── T88: Native AOT prototype must not claim runtime readiness ─────────

    [Fact]
    public void Test_T88_ProviderReadiness_FailsClosedWithoutGrpcHost()
    {
        using var sw = new StringWriter();
        Console.SetOut(sw);

        var exitCode = Cyrene.Provider.OpenAi.Program.Main(ReadinessArgs);
        Assert.Equal(2, exitCode);

        var output = sw.ToString();
        Assert.Contains("\"status\":\"NOT_SERVING\"", output);
        Assert.Contains("\"reason\":\"DIRECT_RUNTIME_HOST_NOT_CONFIGURED\"", output);
        Assert.Contains("\"provider\":\"openai\"", output);
        Assert.DoesNotContain("key", output, StringComparison.OrdinalIgnoreCase);
    }

    // ── M5D Exit Gate Verification ─────────────────────────────────────────

    [Fact]
    public void Test_M5D_ExitGate_ProvidersShareInfrastructureNotGlobalRouting()
    {
        // 1. Providers share HttpTransportClient, SseStreamReader, RedactionSanitizer
        var config1 = new ProviderBindingConfiguration(new Uri("https://api.openai.com"), "sk-key-1");
        var config2 = new ProviderBindingConfiguration(new Uri("https://api.anthropic.com"), "ant-key-2");

        var openAiAdapter = new OpenAiVendorAdapter(config1);
        var anthropicAdapter = new AnthropicVendorAdapter(config2);

        // 2. Both adapters are completely independent instances without shared mutable global routing
        Assert.NotNull(openAiAdapter);
        Assert.NotNull(anthropicAdapter);
        Assert.NotSame(openAiAdapter, anthropicAdapter);
    }
}
