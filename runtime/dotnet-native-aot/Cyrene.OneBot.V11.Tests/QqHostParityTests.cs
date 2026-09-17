// ┌─────────────────────────────────────────────────────────────────────────┐
// │  📄 QqHostParityTests.cs                                                  │
// │  Namespace: Cyrene.OneBot.V11.Tests                                      │
// │  Role: QQ Host protocol, validator, and lifecycle parity evidence.       │
// │                                                                         │
// │  模块职责：QQ Host 协议、校验器与生命周期的跨语言等价性证据                  │
// └─────────────────────────────────────────────────────────────────────────┘

using System.Buffers.Binary;
using System.Globalization;
using System.Text;
using System.Text.Json;
using Cyrene.OneBot.V11.Core;
using Xunit;

namespace Cyrene.OneBot.V11.Tests;

/// <summary>
/// Mirrors the closed-boundary and fake-Host behaviors of the Python QQNT
/// reference suite.
/// <para>覆盖 Python QQNT 参考套件的封闭边界与 fake Host 行为。</para>
/// </summary>
public sealed class QqHostParityTests
{
    [Fact]
    public void FixedOperationRegistryIsClosedAndExplicit()
    {
        QqHostOperation[] operations = QqHostOperationRegistry.All.ToArray();

        Assert.NotEmpty(operations);
        Assert.Equal(
            operations.Length,
            operations.Select(operation => operation.Name).Distinct(StringComparer.Ordinal).Count());
        Assert.Equal(
            operations.Length,
            operations.Select(operation => $"{operation.Service}\0{operation.Method}")
                .Distinct(StringComparer.Ordinal)
                .Count());
        Assert.All(
            operations,
            operation =>
            {
                Assert.StartsWith("qq.", operation.Name, StringComparison.Ordinal);
                Assert.False(string.IsNullOrWhiteSpace(operation.Service));
                Assert.False(string.IsNullOrWhiteSpace(operation.Method));
                Assert.StartsWith("P", operation.Priority, StringComparison.Ordinal);
                Assert.False(string.IsNullOrWhiteSpace(operation.Mapping));
            });

        Assert.Contains(
            operations,
            operation => operation.Name == "qq.message.send_completion" && !operation.Requestable);
        Assert.Contains(
            operations,
            operation => operation.Name == "qq.media.download_complete" && !operation.Requestable);
    }

    [Fact]
    public void EveryRequestableOperationHasAClosedParameterSchema()
    {
        foreach (QqHostOperation operation in QqHostOperationRegistry.All.Where(
                     operation => operation.Requestable))
        {
            QqHostOperationValidator.ValidateParameters(operation.Name, ParseJson("{}"));
        }
    }

    [Theory]
    [InlineData("qq.group.list", "{\"service\":\"NodeIKernelGroupService\"}")]
    [InlineData("qq.group.list", "{\"method\":\"getGroupList\"}")]
    [InlineData("qq.group.list", "{\"raw_payload\":{}}")]
    [InlineData("qq.group.list", "{\"secret_ref\":\"secret://wrong-scope\"}")]
    [InlineData("qq.group.list", "{\"account_id\":true}")]
    [InlineData("qq.group.list", "{\"count\":-1}")]
    [InlineData("qq.group.list", "{\"offset\":1.5}")]
    [InlineData("qq.group.list", "{\"account_id\":null}")]
    [InlineData("qq.message.send", "{\"elements\":{}}")]
    [InlineData("qq.message.subscribe", "{\"events\":[\"\"]}")]
    public void ValidatorRejectsPassthroughAndInvalidTypedParameters(
        string operation,
        string json)
    {
        QqHostException exception = Assert.Throws<QqHostException>(() =>
            QqHostOperationValidator.ValidateParameters(operation, ParseJson(json)));

        Assert.Equal("INVALID_REQUEST", exception.Code);
    }

    [Fact]
    public void ValidatorRejectsNestedAndCollectionBounds()
    {
        StringBuilder nested = new("{\"next\":");
        for (int index = 0; index < QqHostOperationValidator.MaxParameterDepth; index++)
        {
            nested.Append("{\"next\":");
        }

        nested.Append("true");
        for (int index = 0; index <= QqHostOperationValidator.MaxParameterDepth; index++)
        {
            nested.Append('}');
        }

        QqHostException deep = Assert.Throws<QqHostException>(() =>
            QqHostOperationValidator.ValidateParameters(
                "qq.message.search",
                ParseJson($"{{\"filter\":{nested}}}")));
        Assert.Contains("nested too deeply", deep.Message, StringComparison.Ordinal);

        StringBuilder events = new("{\"events\":[");
        for (int index = 0; index < QqHostOperationValidator.MaxCollectionItems + 1; index++)
        {
            if (index > 0)
            {
                events.Append(',');
            }

            events.Append("\"message.received\"");
        }

        events.Append("]}");
        QqHostException tooManyItems = Assert.Throws<QqHostException>(() =>
            QqHostOperationValidator.ValidateParameters(
                "qq.message.subscribe",
                ParseJson(events.ToString())));
        Assert.Contains("too many list items", tooManyItems.Message, StringComparison.Ordinal);

        StringBuilder fields = new("{");
        for (int index = 0; index < QqHostOperationValidator.MaxParameterFields + 1; index++)
        {
            if (index > 0)
            {
                fields.Append(',');
            }

            fields.Append("\"field");
            fields.Append(index.ToString(CultureInfo.InvariantCulture));
            fields.Append("\":true");
        }

        fields.Append('}');
        QqHostException tooManyFields = Assert.Throws<QqHostException>(() =>
            QqHostOperationValidator.ValidateParameters(
                "qq.group.list",
                ParseJson(fields.ToString())));
        Assert.Contains("too many fields", tooManyFields.Message, StringComparison.Ordinal);
    }

    [Theory]
    [InlineData("qq.group.list", "{\"session_token\":\"fixture-secret\"}")]
    [InlineData("qq.group.list", "{\"operation\":\"qq.friend.list\"}")]
    [InlineData("qq.media.download", "{\"remote_uri\":\"file:///tmp/secret\"}")]
    [InlineData("qq.media.download", "{\"local_result_reference\":\"/tmp/secret\"}")]
    public void ValidatorRejectsUnsafeResults(string operation, string json)
    {
        QqHostException exception = Assert.Throws<QqHostException>(() =>
            QqHostOperationValidator.ValidateResult(operation, ParseJson(json)));

        Assert.Equal("PROTOCOL_MISMATCH", exception.Code);
    }

    [Fact]
    public void ValidatorRequiresNativeIdentityForMessageSendAndAllowsNullReferences()
    {
        QqHostException missingMessageId = Assert.Throws<QqHostException>(() =>
            QqHostOperationValidator.ValidateResult(
                "qq.message.send",
                ParseJson("{\"sequence\":7,\"random\":11}")));
        Assert.Contains("message_id", missingMessageId.Message, StringComparison.Ordinal);

        QqHostOperationValidator.ValidateResult(
            "qq.media.download",
            ParseJson(
                "{\"operation\":\"qq.media.download\","
                + "\"remote_uri\":null,\"local_result_reference\":null}"));
    }

    [Fact]
    public async Task ProtocolRejectsInvalidLengthAndPayloadShapes()
    {
        await AssertInvalidFrameAsync(Frame(string.Empty));
        await AssertInvalidFrameAsync(new byte[] { 0, 0, 0, 0 });

        byte[] oversized = new byte[QqHostProtocol.FrameHeaderBytes];
        BinaryPrimitives.WriteUInt32BigEndian(
            oversized,
            (uint)(QqHostProtocol.MaxFrameBytes + 1));
        await AssertInvalidFrameAsync(oversized);

        await AssertInvalidFrameAsync(Frame("[]"));
        await AssertInvalidFrameAsync(Frame("{"));
    }

    [Fact]
    public async Task ProtocolReadsFragmentedUtf8Frames()
    {
        byte[] frame = Frame("{\"type\":\"hello\",\"value\":\"中文\"}");
        await using ChunkedMemoryStream stream = new(frame, 1);

        byte[] payload = (await QqHostProtocol.ReadFrameAsync(
            stream,
            CancellationToken.None))!;

        Assert.Equal("{\"type\":\"hello\",\"value\":\"中文\"}", Encoding.UTF8.GetString(payload));
    }

    [Fact]
    public async Task FakeHostDispatchesEveryRequestableOperationAndShutsDownCleanly()
    {
        if (!CanRunFixture())
        {
            return;
        }

        string dataDirectory = TemporaryPath("qq-host-all-operations");
        string operationLog = Path.Combine(dataDirectory, "operations.log");
        string controlLog = Path.Combine(dataDirectory, "control.log");
        Directory.CreateDirectory(dataDirectory);
        await using QqHostClient client = CreateFixtureClient(
            dataDirectory,
            operationLog: operationLog,
            controlLog: controlLog);

        await client.StartAsync(CancellationToken.None);
        Assert.Equal("NATIVE_READY", client.State);
        Assert.Equal(1, client.Generation);

        foreach (QqHostOperation operation in QqHostOperationRegistry.All.Where(
                     operation => operation.Requestable))
        {
            JsonElement result = await client.RequestAsync(
                operation.Name,
                ParseJson("{\"account_id\":\"10001\"}"),
                CancellationToken.None);
            Assert.Equal(JsonValueKind.Object, result.ValueKind);
        }

        string[] dispatched = File.ReadAllLines(operationLog);
        Assert.Equal(
            QqHostOperationRegistry.All
                .Where(operation => operation.Requestable)
                .Select(operation => operation.Name)
                .OrderBy(name => name, StringComparer.Ordinal),
            dispatched.OrderBy(name => name, StringComparer.Ordinal));

        await client.CloseAsync(CancellationToken.None);
        await WaitUntilAsync(
            () => ContainsControlType(controlLog, "shutdown"),
            TimeSpan.FromSeconds(1));
        Assert.Equal("STOPPED", client.State);
    }

    [Theory]
    [InlineData("wrong_version", "UNSUPPORTED_VERSION")]
    [InlineData("wrong_abi", "UNSUPPORTED_VERSION")]
    [InlineData("missing_abi", "PROTOCOL_MISMATCH")]
    [InlineData("wrong_binding", "PROTOCOL_MISMATCH")]
    [InlineData("wrong_generation", "PROTOCOL_MISMATCH")]
    [InlineData("malformed_hello", "PROTOCOL_MISMATCH")]
    public async Task FakeHostRejectsEveryIncompatibleHello(
        string mode,
        string expectedCode)
    {
        if (!CanRunFixture())
        {
            return;
        }

        string dataDirectory = TemporaryPath($"qq-host-{mode}");
        await using QqHostClient client = CreateFixtureClient(dataDirectory, mode);

        QqHostException exception = await Assert.ThrowsAsync<QqHostException>(
            () => client.StartAsync(CancellationToken.None));

        Assert.Equal(expectedCode, exception.Code);
        Assert.Equal("STOPPED", client.State);
    }

    [Fact]
    public async Task CancellationSendsControlFrameAndIgnoresLateResponse()
    {
        if (!CanRunFixture())
        {
            return;
        }

        string dataDirectory = TemporaryPath("qq-host-cancel");
        string requestLog = Path.Combine(dataDirectory, "requests.log");
        string controlLog = Path.Combine(dataDirectory, "control.log");
        Directory.CreateDirectory(dataDirectory);
        await using QqHostClient client = CreateFixtureClient(
            dataDirectory,
            mode: "cancel",
            requestLog: requestLog,
            controlLog: controlLog);
        await client.StartAsync(CancellationToken.None);

        using CancellationTokenSource cancellation = new();
        Task<JsonElement> pending = client.RequestAsync(
            "qq.group.detail",
            ParseJson("{\"account_id\":\"10001\"}"),
            cancellation.Token);
        await WaitUntilAsync(
            () => ContainsRequestOperation(requestLog, "qq.group.detail"),
            TimeSpan.FromSeconds(1));
        cancellation.Cancel();

        QqHostException exception = await Assert.ThrowsAsync<QqHostException>(
            () => pending);
        Assert.Equal("CANCELLED", exception.Code);
        await WaitUntilAsync(
            () => ContainsControlType(controlLog, "cancel"),
            TimeSpan.FromSeconds(1));

        JsonElement result = await client.RequestAsync(
            "qq.group.list",
            ParseJson("{\"account_id\":\"10001\"}"),
            CancellationToken.None);
        Assert.Equal("qq.group.list", result.GetProperty("operation").GetString());
        Assert.Equal("NATIVE_READY", client.State);
    }

    private static bool CanRunFixture() =>
        OperatingSystem.IsLinux() && File.Exists("/usr/bin/python3");

    private static QqHostClient CreateFixtureClient(
        string dataDirectory,
        string mode = "normal",
        string? operationLog = null,
        string? requestLog = null,
        string? controlLog = null)
    {
        string fixture = RepositoryPath(
            "plugins/connectors/onebot-v11/tests/fixtures/fake_qq_host.py");
        List<string> arguments = new() { fixture, $"--mode={mode}" };
        if (operationLog is not null)
        {
            arguments.Add($"--operation-log={operationLog}");
        }

        if (requestLog is not null)
        {
            arguments.Add($"--request-log={requestLog}");
        }

        if (controlLog is not null)
        {
            arguments.Add($"--control-log={controlLog}");
        }

        return new QqHostClient(
            new QqHostLaunchConfiguration(
                $"qq-parity-{Guid.NewGuid():N}",
                "/usr/bin/python3",
                arguments,
                dataDirectory,
                "fixture-client",
                "fake-qqnt-linux-x86_64"));
    }

    private static async Task AssertInvalidFrameAsync(byte[] frame)
    {
        await using MemoryStream stream = new(frame);
        await Assert.ThrowsAsync<QqHostProtocolException>(async () =>
            await QqHostProtocol.ReadFrameAsync(stream, CancellationToken.None));
    }

    private static byte[] Frame(string payload)
    {
        byte[] bytes = Encoding.UTF8.GetBytes(payload);
        byte[] frame = new byte[QqHostProtocol.FrameHeaderBytes + bytes.Length];
        BinaryPrimitives.WriteUInt32BigEndian(frame, (uint)bytes.Length);
        bytes.CopyTo(frame, QqHostProtocol.FrameHeaderBytes);
        return frame;
    }

    private static JsonElement ParseJson(string json)
    {
        using JsonDocument document = JsonDocument.Parse(json);
        return document.RootElement.Clone();
    }

    private static string TemporaryPath(string name) => Path.Combine(
        Path.GetTempPath(),
        $"cyrene-{name}-{Guid.NewGuid():N}");

    private static bool ContainsControlType(string path, string type)
    {
        try
        {
            return File.Exists(path)
                && File.ReadAllLines(path).Any(
                    line => line.Contains($"\"type\":\"{type}\"", StringComparison.Ordinal));
        }
        catch (IOException)
        {
            return false;
        }
    }

    private static bool ContainsRequestOperation(string path, string operation)
    {
        try
        {
            return File.Exists(path)
                && File.ReadAllLines(path).Any(
                    line => line.Contains(
                        $"\"operation\":\"{operation}\"",
                        StringComparison.Ordinal));
        }
        catch (IOException)
        {
            return false;
        }
    }

    private static async Task WaitUntilAsync(Func<bool> condition, TimeSpan timeout)
    {
        DateTime deadline = DateTime.UtcNow.Add(timeout);
        while (!condition() && DateTime.UtcNow < deadline)
        {
            await Task.Delay(TimeSpan.FromMilliseconds(10));
        }

        Assert.True(condition(), "condition was not satisfied before the timeout");
    }

    private static string RepositoryPath(params string[] parts)
    {
        DirectoryInfo? directory = new(AppContext.BaseDirectory);
        while (directory is not null)
        {
            if (File.Exists(Path.Combine(directory.FullName, "source-manifest.json")))
            {
                return Path.Combine(new[] { directory.FullName }.Concat(parts).ToArray());
            }

            directory = directory.Parent;
        }

        throw new InvalidOperationException("repository root was not found");
    }

    private sealed class ChunkedMemoryStream : MemoryStream
    {
        private readonly int _maxChunk;

        public ChunkedMemoryStream(byte[] buffer, int maxChunk)
            : base(buffer)
        {
            _maxChunk = maxChunk;
        }

        public override ValueTask<int> ReadAsync(
            Memory<byte> buffer,
            CancellationToken cancellationToken = default)
        {
            int length = Math.Min(_maxChunk, buffer.Length);
            return base.ReadAsync(buffer[..length], cancellationToken);
        }
    }
}
