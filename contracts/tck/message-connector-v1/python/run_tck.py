#!/usr/bin/env python3
"""Validate generated Python bindings for the direct connector payload.

中文:验证直连连接器载荷的生成 Python 绑定。"""

from __future__ import annotations

import sys
from pathlib import Path


if len(sys.argv) != 2:
    raise SystemExit("usage: run_tck.py <generated-python-root>")
sys.path.insert(0, str(Path(sys.argv[1]).resolve()))

from google.protobuf.any_pb2 import Any  # noqa: E402
from cyrene.message.connector.v1 import message_connector_pb2  # noqa: E402


scope = message_connector_pb2.ConversationScope(
    vendor="onebot.v11",
    account_id="10001",
    conversation_id="456",
    kind=message_connector_pb2.CONVERSATION_KIND_GROUP,
)
inbound = message_connector_pb2.InboundMessagePayload(
    message_id="9002",
    conversation=scope,
    sender_id="123",
    content=[
        message_connector_pb2.MessageContentPart(
            text=message_connector_pb2.TextContent(text="look")
        )
    ],
    reply=message_connector_pb2.ReplyReference(message_id="777"),
)
packed_inbound = Any()
packed_inbound.Pack(inbound)
unpacked_inbound = message_connector_pb2.InboundMessagePayload()
assert packed_inbound.Unpack(unpacked_inbound)
assert unpacked_inbound.reply.message_id == "777"

delivery = message_connector_pb2.DeliveryResult(
    status=message_connector_pb2.DELIVERY_STATUS_RATE_LIMITED,
    reason="vendor rate limit",
)
delivery.retry_after.seconds = 30
packed_delivery = Any()
packed_delivery.Pack(delivery)
unpacked_delivery = message_connector_pb2.DeliveryResult()
assert packed_delivery.Unpack(unpacked_delivery)
assert unpacked_delivery.retry_after.seconds == 30

print("message.connector.v1 generated Python payload TCK: PASS")
