using Cyrene.Message.Connector.V1;
using Google.Protobuf.WellKnownTypes;

var conversation = new ConversationScope
{
    Vendor = "onebot.v11",
    AccountId = "10001",
    ConversationId = "456",
    Kind = ConversationKind.Group,
};

var inbound = new InboundMessagePayload
{
    MessageId = "9002",
    Conversation = conversation,
    SenderId = "123",
    SenderDisplayName = "Alice",
    Reply = new ReplyReference { MessageId = "777" },
};
inbound.Content.Add(new MessageContentPart
{
    Text = new TextContent { Text = "look" },
});
var packedInbound = Any.Pack(inbound);
if (!packedInbound.Is(InboundMessagePayload.Descriptor)
    || packedInbound.Unpack<InboundMessagePayload>().Reply.MessageId != "777")
{
    throw new InvalidOperationException("Inbound connector payload did not round-trip.");
}

var send = new SendMessageRequest
{
    Conversation = conversation.Clone(),
    Reply = new ReplyReference { MessageId = "9002" },
};
send.Content.Add(new MessageContentPart
{
    Text = new TextContent { Text = "answer" },
});
var packedSend = Any.Pack(send);
if (!packedSend.Is(SendMessageRequest.Descriptor))
{
    throw new InvalidOperationException("Outbound connector payload is not canonical.");
}

var delivery = new DeliveryResult
{
    Status = DeliveryStatus.RateLimited,
    Reason = "vendor rate limit",
    RetryAfter = Duration.FromTimeSpan(TimeSpan.FromSeconds(30)),
};
var packedDelivery = Any.Pack(delivery);
if (!packedDelivery.Is(DeliveryResult.Descriptor)
    || packedDelivery.Unpack<DeliveryResult>().Status != DeliveryStatus.RateLimited)
{
    throw new InvalidOperationException("Delivery result did not round-trip.");
}

Console.WriteLine("message.connector.v1 generated C# payload TCK: PASS");
