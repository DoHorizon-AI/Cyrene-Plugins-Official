// ┌─────────────────────────────────────────────────────────────────────────┐
// │  📄 QqHostProtocol.cs                                                     │
// │  Namespace: Cyrene.Im.Core                                                 │
// │  Role: Length-delimited QQ Host stdio protocol primitives.                │
// │                                                                         │
// │  模块职责：实现 QQ Host 有界大端长度分帧与 UTF-8 JSON 协议基础                 │
// └─────────────────────────────────────────────────────────────────────────┘

using System.Buffers.Binary;
using System.Text.Json;
using System.Text.Json.Serialization.Metadata;

namespace Cyrene.Im.Core;

/// <summary>Cyrene-owned QQ Host stdio protocol constants.</summary>
public static class QqHostProtocol
{
    public const string Protocol = "cyrene.qq.host.v1";
    public const string Version = "1";
    public const int FrameHeaderBytes = 4;
    public const int MaxFrameBytes = 8 * 1024 * 1024;

    public static byte[] Encode<T>(T message, JsonTypeInfo<T> typeInfo)
    {
        byte[] payload;
        try
        {
            payload = JsonSerializer.SerializeToUtf8Bytes(message, typeInfo);
        }
        catch (JsonException exception)
        {
            throw new QqHostProtocolException(
                "QQ Host protocol message is not JSON serializable.",
                exception);
        }

        if (payload.Length is <= 0 or > MaxFrameBytes)
        {
            throw new QqHostProtocolException(
                "QQ Host protocol message exceeds the frame limit.");
        }

        byte[] frame = new byte[FrameHeaderBytes + payload.Length];
        BinaryPrimitives.WriteUInt32BigEndian(
            frame.AsSpan(0, FrameHeaderBytes),
            (uint)payload.Length);
        payload.CopyTo(frame, FrameHeaderBytes);
        return frame;
    }

    public static ValueTask WriteAsync(
        Stream stream,
        QqHostRequest message,
        CancellationToken cancellationToken) => WriteEncodedAsync(
            stream,
            Encode(message, QqHostJsonContext.Default.QqHostRequest),
            cancellationToken);

    public static ValueTask WriteAsync(
        Stream stream,
        QqHostShutdown message,
        CancellationToken cancellationToken) => WriteEncodedAsync(
            stream,
            Encode(message, QqHostJsonContext.Default.QqHostShutdown),
            cancellationToken);

    public static async ValueTask<byte[]?> ReadFrameAsync(
        Stream stream,
        CancellationToken cancellationToken)
    {
        byte[] header = new byte[FrameHeaderBytes];
        bool hasHeader = await ReadExactAsync(stream, header, cancellationToken);
        if (!hasHeader)
        {
            return null;
        }

        uint length = BinaryPrimitives.ReadUInt32BigEndian(header);
        if (length is 0 or > MaxFrameBytes)
        {
            throw new QqHostProtocolException(
                "QQ Host frame length is invalid or exceeds the limit.");
        }

        byte[] payload = new byte[length];
        if (!await ReadExactAsync(stream, payload, cancellationToken))
        {
            throw new QqHostProtocolException("QQ Host frame payload is truncated.");
        }

        try
        {
            using JsonDocument document = JsonDocument.Parse(payload);
            if (document.RootElement.ValueKind != JsonValueKind.Object)
            {
                throw new QqHostProtocolException(
                    "QQ Host frame payload must be a JSON object.");
            }
        }
        catch (JsonException exception)
        {
            throw new QqHostProtocolException(
                "QQ Host frame payload is not valid UTF-8 JSON.",
                exception);
        }

        return payload;
    }

    private static async ValueTask WriteEncodedAsync(
        Stream stream,
        byte[] frame,
        CancellationToken cancellationToken)
    {
        await stream.WriteAsync(frame, cancellationToken);
        await stream.FlushAsync(cancellationToken);
    }

    private static async ValueTask<bool> ReadExactAsync(
        Stream stream,
        Memory<byte> destination,
        CancellationToken cancellationToken)
    {
        int offset = 0;
        while (offset < destination.Length)
        {
            int read = await stream.ReadAsync(
                destination[offset..],
                cancellationToken);
            if (read == 0)
            {
                if (offset == 0)
                {
                    return false;
                }

                throw new QqHostProtocolException("QQ Host frame is truncated.");
            }

            offset += read;
        }

        return true;
    }
}

/// <summary>Malformed or oversized QQ Host protocol data.</summary>
public sealed class QqHostProtocolException : Exception
{
    public QqHostProtocolException(string message)
        : base(message)
    {
    }

    public QqHostProtocolException(string message, Exception innerException)
        : base(message, innerException)
    {
    }
}
