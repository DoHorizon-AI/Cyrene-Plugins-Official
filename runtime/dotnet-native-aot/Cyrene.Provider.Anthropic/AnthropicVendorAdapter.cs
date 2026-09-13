// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 AnthropicVendorAdapter.cs                                       │
// │  Namespace: Cyrene.Provider.Anthropic                               │
// │  Role: Strict Anthropic Messages API wire translation (T83, T85).  │
// └─────────────────────────────────────────────────────────────────────┘

using System.Runtime.CompilerServices;
using System.Text;
using System.Text.Json;
using Cyrene.Provider.Infrastructure.Capability;
using Cyrene.Provider.Infrastructure.Diagnostics;
using Cyrene.Provider.Infrastructure.Security;
using Cyrene.Provider.Infrastructure.Serialization;
using Cyrene.Provider.Infrastructure.Transport;

namespace Cyrene.Provider.Anthropic;

public sealed class AnthropicVendorAdapter : IModelCapability
{
    private readonly ProviderBindingConfiguration _config;
    private readonly HttpTransportClient _transport;

    public AnthropicVendorAdapter(
        ProviderBindingConfiguration config,
        HttpTransportClient? transport = null
    )
    {
        _config = config ?? throw new ArgumentNullException(nameof(config));
        _transport = transport ?? new HttpTransportClient();
    }

    public async Task<ChatCompletionResult> CompleteChatAsync(
        ChatCompletionParameters parameters,
        CancellationToken cancellationToken = default
    )
    {
        var wireReq = BuildRequest(parameters, stream: false);

        var json = JsonSerializer.Serialize(wireReq, ProviderJsonSerializerContext.Default.AnthropicMessagesRequest);
        using var httpRequest = new HttpRequestMessage(HttpMethod.Post, new Uri(_config.EndpointUri, "/v1/messages"))
        {
            Content = new StringContent(json, Encoding.UTF8, "application/json")
        };
        httpRequest.Headers.Add("x-api-key", _config.ApiKey);
        httpRequest.Headers.Add("anthropic-version", "2023-06-01");

        using var response = await _transport.SendRequestAsync(httpRequest, _config, cancellationToken).ConfigureAwait(false);
        var body = await response.Content.ReadAsStringAsync(cancellationToken).ConfigureAwait(false);

        var parsed = JsonSerializer.Deserialize(body, ProviderJsonSerializerContext.Default.AnthropicMessagesResponse);
        if (parsed == null || parsed.Content == null || parsed.Content.Count == 0)
        {
            throw new ProviderException(ProviderErrorCode.MalformedResponse, "Anthropic response contained no content blocks.");
        }

        var fullText = string.Join("\n", parsed.Content.Where(c => c.Type == "text" && c.Text != null).Select(c => c.Text));
        var toolCalls = parsed.Content
            .Where(block => block.Type == "tool_use" && block.Id is not null && block.Name is not null)
            .Select(block => new ChatToolCall(
                Id: block.Id!,
                Type: "function",
                Function: new ChatToolCallFunction(block.Name!, block.Input?.GetRawText() ?? "{}")
            ))
            .ToList();
        return new ChatCompletionResult(
            Id: parsed.Id,
            Model: parsed.Model,
            Content: fullText,
            FinishReason: parsed.StopReason,
            PromptTokens: parsed.Usage?.InputTokens,
            CompletionTokens: parsed.Usage?.OutputTokens,
            ToolCalls: toolCalls.Count > 0 ? toolCalls : null
        );
    }

    public async IAsyncEnumerable<ChatCompletionChunk> StreamChatAsync(
        ChatCompletionParameters parameters,
        [EnumeratorCancellation] CancellationToken cancellationToken = default
    )
    {
        var wireReq = BuildRequest(parameters, stream: true);

        var json = JsonSerializer.Serialize(wireReq, ProviderJsonSerializerContext.Default.AnthropicMessagesRequest);
        using var httpRequest = new HttpRequestMessage(HttpMethod.Post, new Uri(_config.EndpointUri, "/v1/messages"))
        {
            Content = new StringContent(json, Encoding.UTF8, "application/json")
        };
        httpRequest.Headers.Add("x-api-key", _config.ApiKey);
        httpRequest.Headers.Add("anthropic-version", "2023-06-01");

        using var response = await _transport.SendRequestAsync(httpRequest, _config, cancellationToken).ConfigureAwait(false);
        var stream = await response.Content.ReadAsStreamAsync(cancellationToken).ConfigureAwait(false);

        var messageId = string.Empty;
        int? promptTokens = null;

        await foreach (var sseEvent in SseStreamReader.ReadEventsAsync(stream, cancellationToken).ConfigureAwait(false))
        {
            AnthropicStreamEvent? parsed;
            try
            {
                parsed = JsonSerializer.Deserialize(sseEvent, ProviderJsonSerializerContext.Default.AnthropicStreamEvent);
            }
            catch (JsonException)
            {
                // Ignore malformed or vendor comment frames, matching the OpenAI stream path.
                continue;
            }

            switch (parsed?.Type)
            {
                case "message_start":
                    messageId = parsed.Message?.Id ?? messageId;
                    promptTokens = parsed.Message?.Usage?.InputTokens ?? promptTokens;
                    break;

                case "content_block_start" when parsed.ContentBlock?.Type == "tool_use":
                    yield return new ChatCompletionChunk(
                        Id: messageId,
                        Delta: string.Empty,
                        FinishReason: null,
                        ToolCalls: new List<ChatToolCallDelta>
                        {
                            new(
                                Index: parsed.Index ?? 0,
                                Id: parsed.ContentBlock.Id,
                                Type: "function",
                                FunctionName: parsed.ContentBlock.Name,
                                FunctionArguments: string.Empty
                            )
                        }
                    );
                    break;

                case "content_block_delta" when parsed.Delta?.Type == "input_json_delta":
                    yield return new ChatCompletionChunk(
                        Id: messageId,
                        Delta: string.Empty,
                        FinishReason: null,
                        ToolCalls: new List<ChatToolCallDelta>
                        {
                            new(
                                Index: parsed.Index ?? 0,
                                FunctionArguments: parsed.Delta.PartialJson ?? string.Empty
                            )
                        }
                    );
                    break;

                case "content_block_delta" when parsed.Delta?.Text is { Length: > 0 } text:
                    yield return new ChatCompletionChunk(messageId, text, null);
                    break;

                case "message_delta":
                    yield return new ChatCompletionChunk(
                        Id: messageId,
                        Delta: string.Empty,
                        FinishReason: parsed.Delta?.StopReason,
                        PromptTokens: promptTokens,
                        CompletionTokens: parsed.Usage?.OutputTokens
                    );
                    break;

                case "error":
                    throw MapStreamError(parsed.Error);

                default:
                    // message_stop, content_block_start/stop, and ping carry no delta.
                    break;
            }
        }
    }

    private static AnthropicMessagesRequest BuildRequest(ChatCompletionParameters parameters, bool stream)
    {
        // Anthropic carries system prompts as a top-level field, never as a
        // message role; tool results arrive as user-role tool_result blocks.
        var systemParts = parameters.Messages
            .Where(message => message.Role == "system")
            .Select(message => message.Content)
            .Where(content => !string.IsNullOrEmpty(content))
            .ToList();
        var wireMessages = parameters.Messages
            .Where(message => message.Role != "system")
            .Select(ToWireMessage)
            .ToList();
        return new AnthropicMessagesRequest(
            Model: parameters.Model,
            Messages: wireMessages,
            MaxTokens: parameters.MaxTokens ?? 1024,
            System: systemParts.Count > 0 ? string.Join("\n", systemParts) : null,
            Temperature: parameters.Temperature,
            Stream: stream,
            Tools: parameters.Tools?.Select(ToWireTool).ToList(),
            ToolChoice: ToWireToolChoice(parameters.ToolChoice)
        );
    }

    private static AnthropicWireMessage ToWireMessage(ChatMessage message)
    {
        if (message.Role == "tool")
        {
            return new AnthropicWireMessage(
                Role: "user",
                Content: new List<AnthropicContentBlock>
                {
                    new(
                        Type: "tool_result",
                        ToolUseId: message.ToolCallId ?? string.Empty,
                        Content: message.Content
                    )
                }
            );
        }

        var blocks = new List<AnthropicContentBlock>();
        if (!string.IsNullOrEmpty(message.Content))
        {
            blocks.Add(new AnthropicContentBlock(Type: "text", Text: message.Content));
        }
        if (message.ToolCalls is not null)
        {
            blocks.AddRange(message.ToolCalls.Select(call => new AnthropicContentBlock(
                Type: "tool_use",
                Id: call.Id,
                Name: call.Function.Name,
                Input: ParseJson(call.Function.Arguments)
            )));
        }
        if (blocks.Count == 0)
        {
            blocks.Add(new AnthropicContentBlock(Type: "text", Text: string.Empty));
        }
        return new AnthropicWireMessage(Role: message.Role, Content: blocks);
    }

    private static AnthropicWireTool ToWireTool(ChatTool tool) =>
        new(
            Name: tool.Function.Name,
            Description: tool.Function.Description,
            InputSchema: ParseJson(tool.Function.ParametersJson)
        );

    private static AnthropicWireToolChoice? ToWireToolChoice(ChatToolChoice? choice)
    {
        if (choice is null)
        {
            return null;
        }

        return choice.Mode switch
        {
            "function" when !string.IsNullOrEmpty(choice.FunctionName) =>
                new AnthropicWireToolChoice("tool", choice.FunctionName),
            "required" => new AnthropicWireToolChoice("any"),
            "auto" => new AnthropicWireToolChoice("auto"),
            // Anthropic has no explicit disable mode; omit the field.
            _ => null,
        };
    }

    private static JsonElement? ParseJson(string json)
    {
        if (string.IsNullOrWhiteSpace(json))
        {
            return null;
        }

        using var document = JsonDocument.Parse(json);
        return document.RootElement.Clone();
    }

    private static ProviderException MapStreamError(AnthropicStreamError? error)
    {
        var errorCode = error?.Type == "overloaded_error"
            ? ProviderErrorCode.ServiceUnavailable
            : ProviderErrorCode.WireProtocolViolation;
        return new ProviderException(
            errorCode,
            error?.Message ?? "Anthropic stream reported an error event."
        );
    }
}
