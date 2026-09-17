// ┌─────────────────────────────────────────────────────────────────────────┐
// │  📄 NativeEquivalenceTests.cs                                            │
// │  Namespace: Cyrene.OneBot.V11.Tests                                      │
// │  Role: Cross-language golden behavior tests for the native migration.    │
// │                                                                         │
// │  模块职责：Native 迁移的跨语言 golden 行为测试                            │
// └─────────────────────────────────────────────────────────────────────────┘

using System.Text;
using System.Text.Json;
using Cyrene.Message.Connector.V1;
using Cyrene.OneBot.V11.Core;
using Xunit;

namespace Cyrene.OneBot.V11.Tests;

public sealed class NativeEquivalenceTests
{
    [Fact]
    public void OutboundAndRequestMappingsMatchThePythonGoldenFixture()
    {
        using JsonDocument fixture = LoadFixture();
        OneBotProfile profile = CreateProfile();

        foreach (JsonElement item in fixture.RootElement.GetProperty("cases").EnumerateArray())
        {
            string kind = item.GetProperty("kind").GetString()!;
            if (kind is not ("send_message" or "respond_request"))
            {
                continue;
            }

            JsonElement input = item.GetProperty("input");
            JsonElement expected = item.GetProperty("expected");
            if (kind == "send_message")
            {
                OneBotSendOperation operation = OneBotMessageMapper.MapSendMessage(
                    ParseSendMessage(input),
                    profile);
                Assert.Equal(expected.GetProperty("action").GetString(), operation.Action);
                AssertJsonEqual(
                    expected.GetProperty("params"),
                    JsonSerializer.SerializeToElement(
                        operation.Request,
                        OneBotJsonContext.Default.OneBotActionRequest));
                Assert.Equal(
                    "fixture-message-1",
                    expected.GetProperty("result").GetProperty("vendor_message_id").GetString());
            }
            else
            {
                OneBotRequestOperation operation = OneBotRequestMapper.Map(
                    Encoding.UTF8.GetBytes(input.GetRawText()));
                Assert.Equal(expected.GetProperty("action").GetString(), operation.Action);
                AssertJsonEqual(
                    expected.GetProperty("params"),
                    JsonSerializer.SerializeToElement(
                        operation.Request,
                        OneBotJsonContext.Default.OneBotActionRequest));
                Assert.Equal(
                    expected.GetProperty("result").GetProperty("request_id").GetString(),
                    operation.RequestId);
            }
        }
    }

    [Fact]
    public void InboundMappingsMatchThePythonGoldenFixture()
    {
        using JsonDocument fixture = LoadFixture();
        OneBotProfile profile = CreateProfile();

        foreach (JsonElement item in fixture.RootElement.GetProperty("cases").EnumerateArray())
        {
            string kind = item.GetProperty("kind").GetString()!;
            if (kind == "inbound_request")
            {
                OneBotNormalizedEvent normalized = OneBotEventNormalizer.Normalize(
                    item.GetProperty("input"),
                    profile)!;
                Assert.Equal("inbound_request", normalized.EventType);
                AssertJsonEqual(
                    item.GetProperty("expected"),
                    JsonDocument.Parse(normalized.Payload).RootElement);
            }
            else if (kind == "inbound_message")
            {
                OneBotNormalizedEvent normalized = OneBotEventNormalizer.Normalize(
                    item.GetProperty("input"),
                    profile)!;
                InboundMessagePayload actual = InboundMessagePayload.Parser.ParseFrom(
                    normalized.Payload);
                AssertInboundMessage(item.GetProperty("expected"), actual);
            }
        }
    }

    private static JsonDocument LoadFixture()
    {
        string path = Path.Combine(
            RepositoryPath(),
            "plugins/connectors/onebot-v11/tests/fixtures/native-equivalence.json");
        return JsonDocument.Parse(File.ReadAllText(path));
    }

    private static OneBotProfile CreateProfile() => OneBotProfileLoader.FromJson(
        "{\"binding_id\":\"fixture\",\"http_base_url\":\"http://fixture.invalid\","
        + "\"self_account_id\":\"10001\",\"timeout_seconds\":2}");

    private static SendMessageRequest ParseSendMessage(JsonElement source)
    {
        JsonElement conversation = source.GetProperty("conversation");
        SendMessageRequest request = new()
        {
            Conversation = new ConversationScope
            {
                Vendor = conversation.GetProperty("vendor").GetString()!,
                AccountId = conversation.GetProperty("account_id").GetString()!,
                ConversationId = conversation.GetProperty("conversation_id").GetString()!,
                Kind = conversation.GetProperty("kind").GetString() switch
                {
                    "private" => ConversationKind.Private,
                    "group" => ConversationKind.Group,
                    _ => ConversationKind.Unspecified
                }
            }
        };

        if (source.TryGetProperty("reply", out JsonElement reply))
        {
            request.Reply = new ReplyReference
            {
                MessageId = reply.GetProperty("message_id").GetString()!
            };
        }

        foreach (JsonElement rawPart in source.GetProperty("content").EnumerateArray())
        {
            JsonProperty part = rawPart.EnumerateObject().Single();
            JsonElement value = part.Value;
            MessageContentPart target = new();
            switch (part.Name)
            {
                case "text":
                    target.Text = new TextContent { Text = value.GetProperty("text").GetString()! };
                    break;
                case "mention":
                    target.Mention = new MentionContent
                    {
                        Target = value.GetProperty("target").GetString() == "everyone"
                            ? MentionTarget.Everyone
                            : MentionTarget.User,
                        TargetId = value.TryGetProperty("target_id", out JsonElement targetId)
                            ? targetId.GetString() ?? string.Empty
                            : string.Empty
                    };
                    break;
                case "image":
                    target.Image = new ImageContent
                    {
                        Reference = ParseAttachment(value.GetProperty("reference"))
                    };
                    break;
                case "file":
                    target.File = new FileContent
                    {
                        Reference = ParseAttachment(value.GetProperty("reference")),
                        FileName = value.TryGetProperty("file_name", out JsonElement fileName)
                            ? fileName.GetString() ?? string.Empty
                            : string.Empty
                    };
                    break;
                default:
                    throw new InvalidOperationException($"Unknown fixture content kind: {part.Name}");
            }

            request.Content.Add(target);
        }

        return request;
    }

    private static AttachmentReference ParseAttachment(JsonElement source)
    {
        if (source.TryGetProperty("remote_uri", out JsonElement uri))
        {
            return new AttachmentReference { RemoteUri = uri.GetString()! };
        }

        JsonElement media = source.GetProperty("vendor_media");
        return new AttachmentReference
        {
            VendorMedia = new VendorMediaReference
            {
                Vendor = media.GetProperty("vendor").GetString()!,
                AccountId = media.GetProperty("account_id").GetString()!,
                MediaId = media.GetProperty("media_id").GetString()!
            }
        };
    }

    private static void AssertInboundMessage(JsonElement expected, InboundMessagePayload actual)
    {
        Assert.Equal(expected.GetProperty("message_id").GetString(), actual.MessageId);
        JsonElement conversation = expected.GetProperty("conversation");
        Assert.Equal(conversation.GetProperty("vendor").GetString(), actual.Conversation.Vendor);
        Assert.Equal(conversation.GetProperty("account_id").GetString(), actual.Conversation.AccountId);
        Assert.Equal(
            conversation.GetProperty("conversation_id").GetString(),
            actual.Conversation.ConversationId);
        Assert.Equal(
            conversation.GetProperty("kind").GetString() == "group"
                ? ConversationKind.Group
                : ConversationKind.Private,
            actual.Conversation.Kind);
        Assert.Equal(expected.GetProperty("sender_id").GetString(), actual.SenderId);
        Assert.Equal(
            expected.GetProperty("sender_display_name").GetString(),
            actual.SenderDisplayName);

        JsonElement expectedContent = expected.GetProperty("content");
        Assert.Equal(expectedContent.GetArrayLength(), actual.Content.Count);
        for (int index = 0; index < actual.Content.Count; index++)
        {
            JsonProperty part = expectedContent[index].EnumerateObject().Single();
            MessageContentPart actualPart = actual.Content[index];
            switch (part.Name)
            {
                case "text":
                    Assert.Equal(
                        part.Value.GetProperty("text").GetString(),
                        actualPart.Text.Text);
                    break;
                case "mention":
                    JsonElement mention = part.Value;
                    Assert.Equal(
                        mention.GetProperty("target").GetString() == "everyone"
                            ? MentionTarget.Everyone
                            : MentionTarget.User,
                        actualPart.Mention.Target);
                    Assert.Equal(
                        mention.GetProperty("target_id").GetString(),
                        actualPart.Mention.TargetId);
                    break;
                case "image":
                    AssertAttachment(
                        part.Value.GetProperty("reference"),
                        actualPart.Image.Reference);
                    break;
                case "file":
                    JsonElement file = part.Value;
                    AssertAttachment(file.GetProperty("reference"), actualPart.File.Reference);
                    Assert.Equal(file.GetProperty("file_name").GetString(), actualPart.File.FileName);
                    break;
                default:
                    throw new InvalidOperationException($"Unknown expected content kind: {part.Name}");
            }
        }

        if (expected.TryGetProperty("reply", out JsonElement reply))
        {
            Assert.Equal(reply.GetProperty("message_id").GetString(), actual.Reply.MessageId);
        }

        if (expected.TryGetProperty("vendor_extension", out JsonElement extension))
        {
            Assert.Equal(extension.GetProperty("vendor").GetString(), actual.VendorExtension.Vendor);
            JsonElement facts = extension.GetProperty("facts");
            Assert.Equal(facts.GetArrayLength(), actual.VendorExtension.Facts.Count);
            for (int index = 0; index < facts.GetArrayLength(); index++)
            {
                Assert.Equal(
                    facts[index].GetProperty("name").GetString(),
                    actual.VendorExtension.Facts[index].Name);
                Assert.Equal(
                    facts[index].GetProperty("value").GetString(),
                    actual.VendorExtension.Facts[index].Value);
            }
        }
    }

    private static void AssertAttachment(
        JsonElement expected,
        AttachmentReference actual)
    {
        if (expected.TryGetProperty("remote_uri", out JsonElement uri))
        {
            Assert.Equal(uri.GetString(), actual.RemoteUri);
            return;
        }

        JsonElement media = expected.GetProperty("vendor_media");
        Assert.Equal(media.GetProperty("vendor").GetString(), actual.VendorMedia.Vendor);
        Assert.Equal(media.GetProperty("account_id").GetString(), actual.VendorMedia.AccountId);
        Assert.Equal(media.GetProperty("media_id").GetString(), actual.VendorMedia.MediaId);
    }

    private static void AssertJsonEqual(JsonElement expected, JsonElement actual)
    {
        Assert.True(
            JsonElement.DeepEquals(expected, actual),
            $"Expected {expected.GetRawText()} but got {actual.GetRawText()}.");
    }

    private static string RepositoryPath()
    {
        DirectoryInfo? directory = new(AppContext.BaseDirectory);
        while (directory is not null)
        {
            if (File.Exists(Path.Combine(directory.FullName, "source-manifest.json")))
            {
                return directory.FullName;
            }

            directory = directory.Parent;
        }

        throw new InvalidOperationException("repository root was not found");
    }
}
