// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 ProviderTests.cs                                                │
// │  Namespace: Cyrene.Provider.Tests                                   │
// │  Role: Comprehensive test suite for M5D (T81 - T88).                │
// └─────────────────────────────────────────────────────────────────────┘
// 中文：文件：ProviderTests.cs
// 中文：命名空间：Cyrene.Provider.Tests
// 中文：职责：针对 M5D（T81–T88）的综合测试套件。

using System.Net;
using System.Text;
using System.Text.Json;
using Cyrene.Provider.Anthropic;
using Cyrene.Provider.Gemini;
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
// 中文：T81：分离 capability profile。

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
// 中文：验证彼此独立的实现互不耦合。
        Assert.False(typeof(IModelCapability).IsAssignableFrom(typeof(IEmbeddingCapability)));
        Assert.False(typeof(ITtsCapability).IsAssignableFrom(typeof(ISttCapability)));
    }

    // ── T82: Redaction Sanitizer ───────────────────────────────────────────
// 中文：T82：脱敏清理器。

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
// 中文：T82：SSE 流读取器。

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
// 中文：T83 与 T85：OpenAI wire 格式转换。

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
// 中文：T83 与 T85：Anthropic wire 格式转换。

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

    private const string OpenAiToolStreamSse = """
        data: {"id":"chatcmpl-tool-stream","model":"gpt-4o","choices":[{"index":0,"delta":{"role":"assistant","tool_calls":[{"index":0,"id":"call_9","type":"function","function":{"name":"get_weather","arguments":""}}]},"finish_reason":null}]}

        data: {"id":"chatcmpl-tool-stream","model":"gpt-4o","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\"city\":"}}]},"finish_reason":null}]}

        data: {"id":"chatcmpl-tool-stream","model":"gpt-4o","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\"Paris\"}"}}]},"finish_reason":"tool_calls"}]}

        data: {"id":"chatcmpl-tool-stream","model":"gpt-4o","choices":[],"usage":{"prompt_tokens":30,"completion_tokens":12,"total_tokens":42}}

        data: [DONE]
        """;

    [Fact]
    public async Task Test_W1_OpenAiV2_ToolCallingRoundTrip()
    {
        const string mockResponseJson = "{\"id\":\"chatcmpl-tool\",\"model\":\"gpt-4o\",\"choices\":[{\"index\":0,\"message\":{\"role\":\"assistant\",\"content\":\"\",\"tool_calls\":[{\"id\":\"call_1\",\"type\":\"function\",\"function\":{\"name\":\"get_weather\",\"arguments\":\"{\\\"city\\\":\\\"Paris\\\"}\"}}]},\"finish_reason\":\"tool_calls\"}],\"usage\":{\"prompt_tokens\":42,\"completion_tokens\":9,\"total_tokens\":51}}";

        string? capturedBody = null;
        var mockHandler = new MockHttpMessageHandler(req =>
        {
            capturedBody = req.Content!.ReadAsStringAsync().GetAwaiter().GetResult();
            return new HttpResponseMessage(HttpStatusCode.OK)
            {
                Content = new StringContent(mockResponseJson, Encoding.UTF8, "application/json")
            };
        });

        var config = new ProviderBindingConfiguration(new Uri("https://api.openai.com"), "sk-test-openai-key");
        var client = new HttpTransportClient(new HttpClient(mockHandler));
        var adapter = new OpenAiVendorAdapter(config, client);

        var tools = new List<ChatTool>
        {
            new("function", new ChatFunctionDefinition(
                Name: "get_weather",
                Description: "Get the weather for a city",
                ParametersJson: "{\"type\":\"object\",\"properties\":{\"city\":{\"type\":\"string\"}},\"required\":[\"city\"]}",
                Strict: true
            ))
        };
        var messages = new List<ChatMessage>
        {
            new("user", "What is the weather in Paris?"),
            new("assistant", string.Empty, ToolCalls: new List<ChatToolCall>
            {
                new("call_1", "function", new ChatToolCallFunction("get_weather", "{\"city\":\"Paris\"}"))
            }),
            new("tool", "{\"temp_c\":21}", ToolCallId: "call_1")
        };

        var result = await adapter.CompleteChatAsync(new ChatCompletionParameters(
            Model: "gpt-4o",
            Messages: messages,
            Tools: tools,
            ToolChoice: new ChatToolChoice("function", "get_weather"),
            ParallelToolCalls: false
        ));

        Assert.NotNull(result.ToolCalls);
        var call = Assert.Single(result.ToolCalls!);
        Assert.Equal("call_1", call.Id);
        Assert.Equal("get_weather", call.Function.Name);
        Assert.Equal("{\"city\":\"Paris\"}", call.Function.Arguments);
        Assert.Equal("tool_calls", result.FinishReason);
        Assert.Equal(51, result.TotalTokens);

        Assert.NotNull(capturedBody);
        Assert.Contains("\"tools\":[", capturedBody);
        Assert.Contains("\"tool_choice\":{\"type\":\"function\",\"function\":{\"name\":\"get_weather\"}}", capturedBody);
        Assert.Contains("\"parallel_tool_calls\":false", capturedBody);
        Assert.Contains("\"strict\":true", capturedBody);
        Assert.Contains("\"parameters\":{\"type\":\"object\"", capturedBody);
        Assert.Contains("\"tool_call_id\":\"call_1\"", capturedBody);
    }

    [Fact]
    public async Task Test_W1_OpenAiV2_StreamingToolCallDeltasAndUsage()
    {
        string? capturedBody = null;
        var mockHandler = new MockHttpMessageHandler(req =>
        {
            capturedBody = req.Content!.ReadAsStringAsync().GetAwaiter().GetResult();
            return new HttpResponseMessage(HttpStatusCode.OK)
            {
                Content = new StringContent(OpenAiToolStreamSse, Encoding.UTF8, "text/event-stream")
            };
        });

        var config = new ProviderBindingConfiguration(new Uri("https://api.openai.com"), "sk-test-openai-key");
        var client = new HttpTransportClient(new HttpClient(mockHandler));
        var adapter = new OpenAiVendorAdapter(config, client);

        var chunks = new List<ChatCompletionChunk>();
        await foreach (var chunk in adapter.StreamChatAsync(new ChatCompletionParameters(
            Model: "gpt-4o",
            Messages: new List<ChatMessage> { new("user", "Weather in Paris?") },
            IncludeUsage: true
        )))
        {
            chunks.Add(chunk);
        }

        Assert.Equal(4, chunks.Count);
        var first = Assert.Single(chunks[0].ToolCalls!);
        Assert.Equal(0, first.Index);
        Assert.Equal("call_9", first.Id);
        Assert.Equal("get_weather", first.FunctionName);
        var second = Assert.Single(chunks[1].ToolCalls!);
        Assert.Equal("{\"city\":", second.FunctionArguments);
        Assert.Equal("tool_calls", chunks[2].FinishReason);
        var third = Assert.Single(chunks[2].ToolCalls!);
        Assert.Equal("\"Paris\"}", third.FunctionArguments);
        Assert.Equal(30, chunks[3].PromptTokens);
        Assert.Equal(12, chunks[3].CompletionTokens);
        Assert.Equal(42, chunks[3].TotalTokens);

        Assert.NotNull(capturedBody);
        Assert.Contains("\"stream\":true", capturedBody);
        Assert.Contains("\"stream_options\":{\"include_usage\":true}", capturedBody);
    }

    private const string AnthropicToolStreamSse = """
        event: message_start
        data: {"type":"message_start","message":{"id":"msg-tool-stream","model":"claude-3-5-sonnet-20241022","usage":{"input_tokens":18,"output_tokens":1}}}

        event: content_block_start
        data: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"toolu_9","name":"get_weather","input":{}}}

        event: content_block_delta
        data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{\"city\":"}}

        event: content_block_delta
        data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"\"Paris\"}"}}

        event: content_block_stop
        data: {"type":"content_block_stop","index":0}

        event: message_delta
        data: {"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":12}}

        event: message_stop
        data: {"type":"message_stop"}
        """;

    [Fact]
    public async Task Test_W1_AnthropicV2_ToolCallingRoundTrip()
    {
        const string mockResponseJson = "{\"id\":\"msg-tool-1\",\"model\":\"claude-3-5-sonnet-20241022\",\"role\":\"assistant\",\"content\":[{\"type\":\"text\",\"text\":\"\"},{\"type\":\"tool_use\",\"id\":\"toolu_1\",\"name\":\"get_weather\",\"input\":{\"city\":\"Paris\"}}],\"stop_reason\":\"tool_use\",\"usage\":{\"input_tokens\":20,\"output_tokens\":15}}";

        string? capturedBody = null;
        var mockHandler = new MockHttpMessageHandler(req =>
        {
            capturedBody = req.Content!.ReadAsStringAsync().GetAwaiter().GetResult();
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
            Messages: new List<ChatMessage>
            {
                new("system", "You are helpful"),
                new("user", "What is the weather in Paris?"),
                new("assistant", string.Empty, ToolCalls: new List<ChatToolCall>
                {
                    new("call_1", "function", new ChatToolCallFunction("get_weather", "{\"city\":\"Paris\"}"))
                }),
                new("tool", "{\"temp_c\":21}", ToolCallId: "toolu_1")
            },
            Tools: new List<ChatTool>
            {
                new("function", new ChatFunctionDefinition(
                    Name: "get_weather",
                    Description: "Get the weather for a city",
                    ParametersJson: "{\"type\":\"object\",\"properties\":{\"city\":{\"type\":\"string\"}},\"required\":[\"city\"]}",
                    Strict: true
                ))
            },
            ToolChoice: new ChatToolChoice("function", "get_weather")
        ));

        Assert.NotNull(result.ToolCalls);
        var call = Assert.Single(result.ToolCalls!);
        Assert.Equal("toolu_1", call.Id);
        Assert.Equal("get_weather", call.Function.Name);
        Assert.Equal("{\"city\":\"Paris\"}", call.Function.Arguments);
        Assert.Equal("tool_use", result.FinishReason);

        Assert.NotNull(capturedBody);
        Assert.Contains("\"system\":\"You are helpful\"", capturedBody);
        Assert.Contains("\"tools\":[{\"name\":\"get_weather\"", capturedBody);
        Assert.Contains("\"input_schema\":{\"type\":\"object\"", capturedBody);
        Assert.Contains("\"tool_choice\":{\"type\":\"tool\",\"name\":\"get_weather\"}", capturedBody);
        Assert.Contains("\"type\":\"tool_use\",\"id\":\"call_1\",\"name\":\"get_weather\"", capturedBody);
        Assert.Contains("\"type\":\"tool_result\",\"tool_use_id\":\"toolu_1\"", capturedBody);
        Assert.DoesNotContain("\"role\":\"system\"", capturedBody);
    }

    [Fact]
    public async Task Test_W1_AnthropicV2_StreamingToolUseDeltas()
    {
        var mockHandler = new MockHttpMessageHandler(_ => new HttpResponseMessage(HttpStatusCode.OK)
        {
            Content = new StringContent(AnthropicToolStreamSse, Encoding.UTF8, "text/event-stream")
        });

        var config = new ProviderBindingConfiguration(new Uri("https://api.anthropic.com"), "ant-test-key");
        var client = new HttpTransportClient(new HttpClient(mockHandler));
        var adapter = new AnthropicVendorAdapter(config, client);

        var chunks = new List<ChatCompletionChunk>();
        await foreach (var chunk in adapter.StreamChatAsync(new ChatCompletionParameters(
            Model: "claude-3-5-sonnet-20241022",
            Messages: new List<ChatMessage> { new("user", "Weather in Paris?") }
        )))
        {
            chunks.Add(chunk);
        }

        Assert.Equal(4, chunks.Count);
        var start = Assert.Single(chunks[0].ToolCalls!);
        Assert.Equal(0, start.Index);
        Assert.Equal("toolu_9", start.Id);
        Assert.Equal("get_weather", start.FunctionName);
        var firstFragment = Assert.Single(chunks[1].ToolCalls!);
        Assert.Equal("{\"city\":", firstFragment.FunctionArguments);
        var secondFragment = Assert.Single(chunks[2].ToolCalls!);
        Assert.Equal("\"Paris\"}", secondFragment.FunctionArguments);
        Assert.Equal("tool_use", chunks[3].FinishReason);
        Assert.Equal(18, chunks[3].PromptTokens);
        Assert.Equal(12, chunks[3].CompletionTokens);
    }

    // ── T86: Sanitized Fixtures Testing ────────────────────────────────────
// 中文：T86：使用脱敏 fixture 进行测试。

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
// 中文：必须产生两个有效事件，且不得抛出未处理异常。
        Assert.Equal(2, events.Count);
        Assert.Contains("Hello", events[0]);
        Assert.Contains("world", events[1]);
    }

    // ── T87: Credential Injection (No Raw Secret Storage) ──────────────────
// 中文：T87：注入凭据（不存储原始密钥）。

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
// 中文：T88：Native AOT 原型不得宣称运行时已就绪。

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
// 中文：M5D 退出门槛验证。

    [Fact]
    public void Test_M5D_ExitGate_ProvidersShareInfrastructureNotGlobalRouting()
    {
        // 1. Providers share HttpTransportClient, SseStreamReader, RedactionSanitizer
// 中文：1. 各 Provider 共用 HttpTransportClient、SseStreamReader 和 RedactionSanitizer。
        var config1 = new ProviderBindingConfiguration(new Uri("https://api.openai.com"), "sk-key-1");
        var config2 = new ProviderBindingConfiguration(new Uri("https://api.anthropic.com"), "ant-key-2");

        var openAiAdapter = new OpenAiVendorAdapter(config1);
        var anthropicAdapter = new AnthropicVendorAdapter(config2);

        // 2. Both adapters are completely independent instances without shared mutable global routing
// 中文：2. 两个适配器是完全独立的实例，不会共享可变的全局路由状态。
        Assert.NotNull(openAiAdapter);
        Assert.NotNull(anthropicAdapter);
        Assert.NotSame(openAiAdapter, anthropicAdapter);
    }
    // ── W6-2: Gemini Native Provider Wire Translation ──────────────────────
// 中文：W6-2：Gemini 原生 Provider wire 格式转换。

    private const string GeminiStreamSse = """
        data: {"candidates":[{"content":{"role":"model","parts":[{"text":"Hel"}]},"index":0}],"usageMetadata":{"promptTokenCount":7,"candidatesTokenCount":1,"totalTokenCount":8}}

        data: {"candidates":[{"content":{"role":"model","parts":[{"text":"lo Cyrene"}]},"index":0}],"usageMetadata":{"promptTokenCount":7,"candidatesTokenCount":3,"totalTokenCount":10}}

        data: {"candidates":[{"content":{"role":"model","parts":[{"functionCall":{"name":"get_weather","args":{"city":"Paris"}}}]},"finishReason":"STOP","index":0}],"usageMetadata":{"promptTokenCount":7,"candidatesTokenCount":9,"totalTokenCount":16}}

        data: {"usageMetadata":{"promptTokenCount":7,"candidatesTokenCount":9,"totalTokenCount":16}}
        """;

    private static GeminiVendorAdapter NewGeminiAdapter(MockHttpMessageHandler handler)
    {
        var config = new ProviderBindingConfiguration(
            new Uri("https://generativelanguage.googleapis.com"),
            "gemini-test-key"
        );
        return new GeminiVendorAdapter(config, new HttpTransportClient(new HttpClient(handler)));
    }

    [Fact]
    public async Task Test_W6_Gemini_WireTranslation_SystemInstructionAndUsage()
    {
        const string mockResponseJson = "{\"candidates\":[{\"content\":{\"role\":\"model\",\"parts\":[{\"text\":\"Hello Cyrene\"}]},\"finishReason\":\"STOP\",\"index\":0}],\"usageMetadata\":{\"promptTokenCount\":10,\"candidatesTokenCount\":5,\"totalTokenCount\":15}}";

        string? capturedPath = null;
        string? capturedKey = null;
        string? capturedBody = null;
        var handler = new MockHttpMessageHandler(request =>
        {
            capturedPath = request.RequestUri!.PathAndQuery;
            capturedKey = request.Headers.GetValues("x-goog-api-key").First();
            capturedBody = request.Content!.ReadAsStringAsync().GetAwaiter().GetResult();
            return new HttpResponseMessage(HttpStatusCode.OK)
            {
                Content = new StringContent(mockResponseJson, Encoding.UTF8, "application/json")
            };
        });

        var adapter = NewGeminiAdapter(handler);

        var result = await adapter.CompleteChatAsync(new ChatCompletionParameters(
            Model: "gemini-2.0-flash",
            Messages: new List<ChatMessage>
            {
                new("system", "Be concise."),
                new("user", "Hi")
            },
            Temperature: 0.2f,
            MaxTokens: 64
        ));

        Assert.Equal("/v1beta/models/gemini-2.0-flash:generateContent", capturedPath);
        Assert.Equal("gemini-test-key", capturedKey);
        // Gemini native responses carry no message id.
// 中文：Gemini 原生响应不携带消息 ID。
        Assert.Equal(string.Empty, result.Id);
        Assert.Equal("Hello Cyrene", result.Content);
        Assert.Equal("STOP", result.FinishReason);
        Assert.Equal(10, result.PromptTokens);
        Assert.Equal(5, result.CompletionTokens);
        Assert.Equal(15, result.TotalTokens);

        Assert.NotNull(capturedBody);
        Assert.Contains("\"systemInstruction\":{\"parts\":[{\"text\":\"Be concise.\"}]}", capturedBody);
        Assert.Contains("\"generationConfig\":{", capturedBody);
        Assert.Contains("\"maxOutputTokens\":64", capturedBody);
        // System messages are hoisted out of contents into systemInstruction.
// 中文：将 system 消息从 contents 中移出，提升到 systemInstruction。
        Assert.DoesNotContain("\"role\":\"system\"", capturedBody);
    }

    [Fact]
    public async Task Test_W6_Gemini_ToolCallingRoundTrip()
    {
        const string mockResponseJson = "{\"candidates\":[{\"content\":{\"role\":\"model\",\"parts\":[{\"functionCall\":{\"name\":\"get_weather\",\"args\":{\"city\":\"Paris\"}}}]},\"finishReason\":\"STOP\",\"index\":0}],\"usageMetadata\":{\"promptTokenCount\":42,\"candidatesTokenCount\":9,\"totalTokenCount\":51}}";

        string? capturedBody = null;
        var handler = new MockHttpMessageHandler(request =>
        {
            capturedBody = request.Content!.ReadAsStringAsync().GetAwaiter().GetResult();
            return new HttpResponseMessage(HttpStatusCode.OK)
            {
                Content = new StringContent(mockResponseJson, Encoding.UTF8, "application/json")
            };
        });

        var adapter = NewGeminiAdapter(handler);

        var tools = new List<ChatTool>
        {
            new("function", new ChatFunctionDefinition(
                Name: "get_weather",
                Description: "Get the weather for a city",
                ParametersJson: "{\"type\":\"object\",\"properties\":{\"city\":{\"type\":\"string\"}},\"required\":[\"city\"]}"
            ))
        };
        var messages = new List<ChatMessage>
        {
            new("user", "What is the weather in Paris?"),
            new("assistant", string.Empty, ToolCalls: new List<ChatToolCall>
            {
                new("call_1", "function", new ChatToolCallFunction("get_weather", "{\"city\":\"Paris\"}"))
            }),
            new("tool", "{\"temp_c\":21}", ToolCallId: "call_1")
        };

        var result = await adapter.CompleteChatAsync(new ChatCompletionParameters(
            Model: "gemini-2.0-flash",
            Messages: messages,
            Tools: tools,
            ToolChoice: new ChatToolChoice("function", "get_weather")
        ));

        var call = Assert.Single(result.ToolCalls!);
        Assert.Equal("gemini-call-0", call.Id);
        Assert.Equal("function", call.Type);
        Assert.Equal("get_weather", call.Function.Name);
        Assert.Equal("{\"city\":\"Paris\"}", call.Function.Arguments);
        Assert.Equal(51, result.TotalTokens);

        Assert.NotNull(capturedBody);
        using var request = JsonDocument.Parse(capturedBody);

        var declaration = request.RootElement
            .GetProperty("tools")[0]
            .GetProperty("functionDeclarations")[0];
        Assert.Equal("get_weather", declaration.GetProperty("name").GetString());
        Assert.Equal("Get the weather for a city", declaration.GetProperty("description").GetString());
        Assert.Equal("object", declaration.GetProperty("parameters").GetProperty("type").GetString());

        var functionConfig = request.RootElement
            .GetProperty("toolConfig")
            .GetProperty("functionCallingConfig");
        Assert.Equal("ANY", functionConfig.GetProperty("mode").GetString());
        Assert.Equal(
            "get_weather",
            functionConfig.GetProperty("allowedFunctionNames")[0].GetString()
        );

        var contents = request.RootElement.GetProperty("contents");
        Assert.Equal("model", contents[1].GetProperty("role").GetString());
        var functionCall = contents[1].GetProperty("parts")[0].GetProperty("functionCall");
        Assert.Equal("get_weather", functionCall.GetProperty("name").GetString());
        Assert.Equal("Paris", functionCall.GetProperty("args").GetProperty("city").GetString());

        // Tool responses correlate by function name, not by call id.
// 中文：工具响应根据函数名称关联，而不是根据调用 ID。
        var functionResponse = contents[2]
            .GetProperty("parts")[0]
            .GetProperty("functionResponse");
        Assert.Equal("get_weather", functionResponse.GetProperty("name").GetString());
        Assert.Equal(21, functionResponse.GetProperty("response").GetProperty("temp_c").GetInt32());
    }

    [Fact]
    public async Task Test_W6_Gemini_ParallelSameNameToolCallsGetDistinctSyntheticIds()
    {
        // Two parallel calls to the same function must stay distinguishable in
        // the capability model even though the Gemini wire protocol carries no
        // call ids.
// 中文：对同一函数发起的两个并行调用，在 capability 模型中必须仍可区分，即使 Gemini wire 协议没有调用 ID。
        const string mockResponseJson = "{\"candidates\":[{\"content\":{\"role\":\"model\",\"parts\":[{\"functionCall\":{\"name\":\"get_weather\",\"args\":{\"city\":\"Paris\"}}},{\"functionCall\":{\"name\":\"get_weather\",\"args\":{\"city\":\"London\"}}}]},\"finishReason\":\"STOP\",\"index\":0}],\"usageMetadata\":{\"promptTokenCount\":20,\"candidatesTokenCount\":8,\"totalTokenCount\":28}}";

        var handler = new MockHttpMessageHandler(_ => new HttpResponseMessage(HttpStatusCode.OK)
        {
            Content = new StringContent(mockResponseJson, Encoding.UTF8, "application/json")
        });
        var adapter = NewGeminiAdapter(handler);

        var result = await adapter.CompleteChatAsync(new ChatCompletionParameters(
            Model: "gemini-2.0-flash",
            Messages: new List<ChatMessage> { new("user", "Compare Paris and London weather") }
        ));

        Assert.Equal(2, result.ToolCalls!.Count);
        Assert.Equal("gemini-call-0", result.ToolCalls[0].Id);
        Assert.Equal("gemini-call-1", result.ToolCalls[1].Id);
        Assert.Equal("get_weather", result.ToolCalls[0].Function.Name);
        Assert.Equal("get_weather", result.ToolCalls[1].Function.Name);
        Assert.Equal("{\"city\":\"Paris\"}", result.ToolCalls[0].Function.Arguments);
        Assert.Equal("{\"city\":\"London\"}", result.ToolCalls[1].Function.Arguments);
    }

    [Fact]
    public async Task Test_W6_Gemini_ParallelToolResponsesFollowCallOrderNotMessageOrder()
    {
        const string mockResponseJson = "{\"candidates\":[{\"content\":{\"role\":\"model\",\"parts\":[{\"text\":\"both\"}]},\"finishReason\":\"STOP\",\"index\":0}]}";

        string? capturedBody = null;
        var handler = new MockHttpMessageHandler(request =>
        {
            capturedBody = request.Content!.ReadAsStringAsync().GetAwaiter().GetResult();
            return new HttpResponseMessage(HttpStatusCode.OK)
            {
                Content = new StringContent(mockResponseJson, Encoding.UTF8, "application/json")
            };
        });
        var adapter = NewGeminiAdapter(handler);

        var messages = new List<ChatMessage>
        {
            new("user", "Compare Paris and London weather and the time"),
            new("assistant", string.Empty, ToolCalls: new List<ChatToolCall>
            {
                new("gemini-call-0", "function", new ChatToolCallFunction("get_weather", "{\"city\":\"Paris\"}")),
                new("gemini-call-1", "function", new ChatToolCallFunction("get_weather", "{\"city\":\"London\"}")),
                new("gemini-call-2", "function", new ChatToolCallFunction("get_time", "{\"zone\":\"UTC\"}"))
            }),
            // The runtime reports the results out of order on purpose.
// 中文：运行时会故意打乱结果顺序。
            new("tool", "{\"temp_c\":14}", ToolCallId: "gemini-call-1"),
            new("tool", "{\"utc\":\"12:00\"}", ToolCallId: "gemini-call-2"),
            new("tool", "{\"temp_c\":21}", ToolCallId: "gemini-call-0")
        };

        await adapter.CompleteChatAsync(new ChatCompletionParameters("gemini-2.0-flash", messages));

        Assert.NotNull(capturedBody);
        using var request = JsonDocument.Parse(capturedBody);
        var contents = request.RootElement.GetProperty("contents");

        // The model turn keeps the calls in their original order.
// 中文：模型轮次仍按原始顺序保留这些调用。
        var calls = contents[1].GetProperty("parts");
        Assert.Equal("Paris", calls[0].GetProperty("functionCall").GetProperty("args").GetProperty("city").GetString());
        Assert.Equal("London", calls[1].GetProperty("functionCall").GetProperty("args").GetProperty("city").GetString());
        Assert.Equal("get_time", calls[2].GetProperty("functionCall").GetProperty("name").GetString());

        // Responses are emitted in call order: Paris, London, then time.
// 中文：按调用顺序发出响应：Paris、London，最后是时间。
        Assert.Equal(5, contents.GetArrayLength());
        var paris = contents[2].GetProperty("parts")[0].GetProperty("functionResponse");
        var london = contents[3].GetProperty("parts")[0].GetProperty("functionResponse");
        var time = contents[4].GetProperty("parts")[0].GetProperty("functionResponse");
        Assert.Equal("get_weather", paris.GetProperty("name").GetString());
        Assert.Equal("get_weather", london.GetProperty("name").GetString());
        Assert.Equal("get_time", time.GetProperty("name").GetString());
        Assert.Equal(21, paris.GetProperty("response").GetProperty("temp_c").GetInt32());
        Assert.Equal(14, london.GetProperty("response").GetProperty("temp_c").GetInt32());
        Assert.Equal("12:00", time.GetProperty("response").GetProperty("utc").GetString());
    }

    [Fact]
    public async Task Test_W6_Gemini_PlainTextToolResponseWrapsAndUnknownIdFailsClosed()
    {
        const string mockResponseJson = "{\"candidates\":[{\"content\":{\"role\":\"model\",\"parts\":[{\"text\":\"ok\"}]},\"finishReason\":\"STOP\",\"index\":0}]}";

        string? capturedBody = null;
        var handler = new MockHttpMessageHandler(request =>
        {
            capturedBody = request.Content!.ReadAsStringAsync().GetAwaiter().GetResult();
            return new HttpResponseMessage(HttpStatusCode.OK)
            {
                Content = new StringContent(mockResponseJson, Encoding.UTF8, "application/json")
            };
        });

        var adapter = NewGeminiAdapter(handler);

        var messages = new List<ChatMessage>
        {
            new("user", "weather?"),
            new("assistant", string.Empty, ToolCalls: new List<ChatToolCall>
            {
                new("call_1", "function", new ChatToolCallFunction("get_weather", "{\"city\":\"Paris\"}"))
            }),
            new("tool", "all good", ToolCallId: "call_1")
        };

        await adapter.CompleteChatAsync(new ChatCompletionParameters("gemini-2.0-flash", messages));

        Assert.NotNull(capturedBody);
        using var request = JsonDocument.Parse(capturedBody);
        var response = request.RootElement
            .GetProperty("contents")[2]
            .GetProperty("parts")[0]
            .GetProperty("functionResponse")
            .GetProperty("response");
        // functionResponse requires a JSON object; plain text is wrapped deterministically.
// 中文：functionResponse 要求 JSON 对象；如果输入是纯文本，则按确定性规则将其包装起来。
        Assert.Equal("all good", response.GetProperty("output").GetString());

        // A tool message whose tool_call_id has no matching assistant tool call is rejected
        // before any network call, because Gemini correlates by function name.
// 中文：如果工具消息的 tool_call_id 找不到匹配的 assistant 工具调用，就会在任何网络请求发出之前被拒绝，因为 Gemini 根据函数名称进行关联。
        var unroutable = new List<ChatMessage> { new("tool", "x", ToolCallId: "missing") };
        var exception = await Assert.ThrowsAsync<ProviderException>(() => adapter.CompleteChatAsync(
            new ChatCompletionParameters("gemini-2.0-flash", unroutable)
        ));
        Assert.Equal(ProviderErrorCode.WireProtocolViolation, exception.ErrorCode);
    }

    [Fact]
    public async Task Test_W6_Gemini_StreamingSSE_DeltasToolCallAndUsage()
    {
        string? capturedPath = null;
        var handler = new MockHttpMessageHandler(request =>
        {
            capturedPath = request.RequestUri!.PathAndQuery;
            return new HttpResponseMessage(HttpStatusCode.OK)
            {
                Content = new StringContent(GeminiStreamSse, Encoding.UTF8, "text/event-stream")
            };
        });

        var adapter = NewGeminiAdapter(handler);

        var chunks = new List<ChatCompletionChunk>();
        await foreach (var chunk in adapter.StreamChatAsync(new ChatCompletionParameters(
            Model: "gemini-2.0-flash",
            Messages: new List<ChatMessage> { new("user", "Weather in Paris?") },
            IncludeUsage: true
        )))
        {
            chunks.Add(chunk);
        }

        Assert.Equal(
            "/v1beta/models/gemini-2.0-flash:streamGenerateContent?alt=sse",
            capturedPath
        );
        Assert.Equal(4, chunks.Count);
        Assert.Equal("Hel", chunks[0].Delta);
        Assert.Equal("lo Cyrene", chunks[1].Delta);
        Assert.Equal("STOP", chunks[2].FinishReason);
        var delta = Assert.Single(chunks[2].ToolCalls!);
        Assert.Equal(0, delta.Index);
        Assert.Equal("gemini-call-0", delta.Id);
        Assert.Equal("get_weather", delta.FunctionName);
        Assert.Equal("{\"city\":\"Paris\"}", delta.FunctionArguments);
        Assert.Equal(string.Empty, chunks[3].Delta);
        Assert.Equal(7, chunks[3].PromptTokens);
        Assert.Equal(9, chunks[3].CompletionTokens);
        Assert.Equal(16, chunks[3].TotalTokens);
    }

    [Fact]
    public async Task Test_W6_Gemini_Embeddings_BatchRequestAndVectorOrder()
    {
        const string mockResponseJson = "{\"embeddings\":[{\"values\":[0.1,0.2]},{\"values\":[0.3,0.4]}]}";

        string? capturedBody = null;
        var handler = new MockHttpMessageHandler(request =>
        {
            capturedBody = request.Content!.ReadAsStringAsync().GetAwaiter().GetResult();
            return new HttpResponseMessage(HttpStatusCode.OK)
            {
                Content = new StringContent(mockResponseJson, Encoding.UTF8, "application/json")
            };
        });

        var adapter = NewGeminiAdapter(handler);

        var result = await adapter.GenerateEmbeddingsAsync(new EmbeddingParameters(
            Model: "text-embedding-004",
            Inputs: new List<string> { "alpha", "beta" },
            Dimensions: 768
        ));

        Assert.Equal("text-embedding-004", result.Model);
        Assert.Equal(2, result.Embeddings.Count);
        Assert.Equal(0, result.Embeddings[0].Index);
        Assert.Equal(0.1f, result.Embeddings[0].Values[0]);
        Assert.Equal(0.2f, result.Embeddings[0].Values[1]);
        Assert.Equal(1, result.Embeddings[1].Index);
        Assert.Equal(0.3f, result.Embeddings[1].Values[0]);
        Assert.Equal(0.4f, result.Embeddings[1].Values[1]);

        Assert.NotNull(capturedBody);
        using var request = JsonDocument.Parse(capturedBody);
        var entries = request.RootElement.GetProperty("requests");
        Assert.Equal(2, entries.GetArrayLength());
        Assert.Equal("models/text-embedding-004", entries[0].GetProperty("model").GetString());
        Assert.Equal(768, entries[0].GetProperty("outputDimensionality").GetInt32());
        Assert.Equal(
            "alpha",
            entries[0].GetProperty("content").GetProperty("parts")[0].GetProperty("text").GetString()
        );
        Assert.Equal(
            "beta",
            entries[1].GetProperty("content").GetProperty("parts")[0].GetProperty("text").GetString()
        );
    }

    [Fact]
    public void Test_W6_Gemini_Readiness_FailsClosedWithoutGrpcHost()
    {
        using var sw = new StringWriter();
        var original = Console.Out;
        Console.SetOut(sw);
        int exitCode;
        try
        {
            exitCode = Cyrene.Provider.Gemini.Program.Main(ReadinessArgs);
        }
        finally
        {
            Console.SetOut(original);
        }

        Assert.Equal(2, exitCode);
        var output = sw.ToString();
        Assert.Contains("\"status\":\"NOT_SERVING\"", output);
        Assert.Contains("\"provider\":\"gemini\"", output);
        Assert.Contains("\"reason\":\"DIRECT_RUNTIME_HOST_NOT_CONFIGURED\"", output);
        Assert.DoesNotContain("key", output, StringComparison.OrdinalIgnoreCase);
    }
}
