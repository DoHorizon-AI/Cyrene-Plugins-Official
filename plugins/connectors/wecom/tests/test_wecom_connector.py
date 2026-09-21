"""WeCom connector tests: mapping, delivery semantics, token cache, proto adapter."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import pytest

from wecom_connector import (
    CAPABILITY_ID,
    DELIVERY_RESULT_TYPE_URL,
    SEND_MESSAGE_REQUEST_TYPE_URL,
    ConnectorError,
    WeComConnector,
    WeComInstanceConfig,
)
from wecom_connector._generated import message_connector_pb2 as message_contract

CONFIG = WeComInstanceConfig(
    binding_id="wecom.binding",
    corp_id="ww-test",
    corp_secret="secret",
    agent_id=1000002,
)


class ScriptedTransport:
    """Deterministic WeCom transport double."""

    def __init__(
        self,
        token_responses: list[Mapping[str, Any]] | None = None,
        send_responses: list[Mapping[str, Any]] | None = None,
        upload_responses: list[Mapping[str, Any]] | None = None,
    ) -> None:
        self.token_responses = list(
            token_responses
            or [
                {
                    "errcode": 0,
                    "errmsg": "ok",
                    "access_token": "token-1",
                    "expires_in": 7200,
                }
            ]
        )
        self.send_responses = list(
            send_responses or [{"errcode": 0, "errmsg": "ok", "msgid": "MSG-1"}]
        )
        self.upload_responses = list(
            upload_responses or [{"errcode": 0, "errmsg": "ok", "media_id": "MEDIA-1"}]
        )
        self.token_calls: list[WeComInstanceConfig] = []
        self.sends: list[tuple[str, dict[str, Any]]] = []
        self.uploads: list[dict[str, Any]] = []

    def get_token(
        self, config: WeComInstanceConfig, *, cancellation: Any | None = None
    ) -> Mapping[str, Any]:
        self.token_calls.append(config)
        if not self.token_responses:
            raise AssertionError("unexpected get_token call")
        return self.token_responses.pop(0)

    def send_agent_message(
        self,
        token: str,
        payload: Mapping[str, Any],
        config: WeComInstanceConfig,
        *,
        cancellation: Any | None = None,
    ) -> Mapping[str, Any]:
        del config
        self.sends.append((token, dict(payload)))
        if not self.send_responses:
            raise AssertionError("unexpected send_agent_message call")
        return self.send_responses.pop(0)

    def upload_media(
        self,
        token: str,
        media_type: str,
        remote_uri: str,
        file_name: str,
        mime_type: str,
        config: WeComInstanceConfig,
        *,
        cancellation: Any | None = None,
    ) -> Mapping[str, Any]:
        del config, cancellation
        self.uploads.append(
            {
                "token": token,
                "media_type": media_type,
                "remote_uri": remote_uri,
                "file_name": file_name,
                "mime_type": mime_type,
            }
        )
        if not self.upload_responses:
            raise AssertionError("unexpected upload_media call")
        return self.upload_responses.pop(0)


def conversation(**overrides: Any) -> dict[str, Any]:
    payload = {
        "vendor": "wecom.app",
        "account_id": "agent:1000002",
        "conversation_id": "zhangsan",
        "kind": "private",
    }
    payload.update(overrides)
    return payload


def connector(transport: ScriptedTransport) -> WeComConnector:
    return WeComConnector(CONFIG, transport=transport)


def test_text_message_maps_target_and_caches_the_token() -> None:
    transport = ScriptedTransport(
        send_responses=[
            {"errcode": 0, "errmsg": "ok", "msgid": "MSG-1"},
            {"errcode": 0, "errmsg": "ok", "msgid": "MSG-2"},
        ]
    )
    plugin = connector(transport)

    first = plugin.send_message(
        {"conversation": conversation(), "content": [{"kind": "text", "text": "hello"}]}
    )
    second = plugin.send_message(
        {"conversation": conversation(), "content": [{"kind": "text", "text": "again"}]}
    )

    assert first["status"] == "accepted"
    assert first["vendor_message_id"] == "MSG-1"
    assert second["status"] == "accepted"
    assert len(transport.token_calls) == 1  # cached across sends

    token, payload = transport.sends[0]
    assert token == "token-1"
    assert payload == {
        "touser": "zhangsan",
        "msgtype": "text",
        "agentid": 1000002,
        "text": {"content": "hello"},
    }


def test_group_markdown_and_mentions_render_deterministically() -> None:
    transport = ScriptedTransport(
        send_responses=[
            {"errcode": 0, "errmsg": "ok", "msgid": "MSG-1"},
            {"errcode": 0, "errmsg": "ok", "msgid": "MSG-2"},
        ]
    )
    plugin = connector(transport)

    plugin.send_message(
        {
            "conversation": conversation(conversation_id="2", kind="group"),
            "content": [{"kind": "text", "text": "deploy done"}],
            "vendor_facts": {"msgtype": "markdown"},
        }
    )
    _, markdown = transport.sends[0]
    assert markdown == {
        "toparty": "2",
        "msgtype": "markdown",
        "agentid": 1000002,
        "markdown": {"content": "deploy done"},
    }

    plugin.send_message(
        {
            "conversation": conversation(conversation_id="2", kind="group"),
            "content": [
                {
                    "kind": "mention",
                    "target": "user",
                    "target_id": "lisi",
                    "display_name": "Li Si",
                },
                {"kind": "text", "text": " please review"},
                {"kind": "mention", "target": "everyone"},
            ],
        }
    )
    _, mention = transport.sends[1]
    assert mention["text"] == {
        "content": "@Li Si please review@all",
        "mentioned_list": ["lisi", "@all"],
    }


def test_delivery_statuses_follow_vendor_errcodes() -> None:
    transport = ScriptedTransport(
        send_responses=[
            {"errcode": 45009, "errmsg": "api freq out of limit"},
            {"errcode": 81013, "errmsg": "user not exist"},
        ]
    )
    plugin = connector(transport)

    limited = plugin.send_message(
        {"conversation": conversation(), "content": [{"kind": "text", "text": "a"}]}
    )
    assert limited["status"] == "rate_limited"
    assert "45009" in limited["reason"]

    rejected = plugin.send_message(
        {"conversation": conversation(), "content": [{"kind": "text", "text": "b"}]}
    )
    assert rejected["status"] == "rejected"
    assert "81013" in rejected["reason"]


def test_expired_token_refreshes_once_and_retries() -> None:
    transport = ScriptedTransport(
        token_responses=[
            {"errcode": 0, "access_token": "token-old", "expires_in": 7200},
            {"errcode": 0, "access_token": "token-new", "expires_in": 7200},
        ],
        send_responses=[
            {"errcode": 42001, "errmsg": "access_token expired"},
            {"errcode": 0, "errmsg": "ok", "msgid": "MSG-2"},
        ],
    )
    plugin = connector(transport)

    result = plugin.send_message(
        {"conversation": conversation(), "content": [{"kind": "text", "text": "retry"}]}
    )

    assert result == {
        "status": "accepted",
        "vendor_message_id": "MSG-2",
        "vendor_facts": [],
    }
    assert [token for token, _ in transport.sends] == ["token-old", "token-new"]
    assert len(transport.token_calls) == 2


def test_transport_failures_do_not_claim_a_delivery_outcome() -> None:
    class BrokenTransport(ScriptedTransport):
        def get_token(self, config, *, cancellation=None):  # type: ignore[no-untyped-def]
            raise ConnectorError("UNAVAILABLE", "wecom gettoken connection refused")

    plugin = connector(BrokenTransport())
    with pytest.raises(ConnectorError) as error:
        plugin.send_message(
            {"conversation": conversation(), "content": [{"kind": "text", "text": "x"}]}
        )
    assert error.value.code == "UNAVAILABLE"


def test_unsupported_targets_and_content_fail_with_typed_errors() -> None:
    plugin = connector(ScriptedTransport())

    rejected = plugin.send_message(
        {
            "conversation": conversation(kind="channel"),
            "content": [{"kind": "text", "text": "x"}],
        }
    )
    assert rejected["status"] == "rejected"
    assert "channel" in rejected["reason"]

    with pytest.raises(ConnectorError) as media:
        plugin.send_message(
            {
                "conversation": conversation(),
                "content": [{"kind": "image"}],
            }
        )
    assert media.value.code == "INVALID_REQUEST"
    assert "reference" in media.value.message

    with pytest.raises(ConnectorError) as fact:
        plugin.send_message(
            {
                "conversation": conversation(),
                "content": [{"kind": "text", "text": "x"}],
                "vendor_facts": {"msgtype": "text", "unknown": "1"},
            }
        )
    assert fact.value.code == "INVALID_REQUEST"


def test_remote_media_uploads_then_sends_the_returned_media_id() -> None:
    transport = ScriptedTransport()
    plugin = connector(transport)

    result = plugin.send_message(
        {
            "conversation": conversation(),
            "content": [
                {
                    "kind": "image",
                    "reference": {"remote_uri": "https://cdn.example.test/photo.png"},
                    "mime_type": "image/png",
                }
            ],
        }
    )

    assert result["status"] == "accepted"
    assert transport.uploads == [
        {
            "token": "token-1",
            "media_type": "image",
            "remote_uri": "https://cdn.example.test/photo.png",
            "file_name": "",
            "mime_type": "image/png",
        }
    ]
    assert transport.sends == [
        (
            "token-1",
            {
                "touser": "zhangsan",
                "msgtype": "image",
                "agentid": 1000002,
                "image": {"media_id": "MEDIA-1"},
            },
        )
    ]


def test_existing_vendor_media_is_reused_only_for_the_bound_agent() -> None:
    transport = ScriptedTransport()
    plugin = connector(transport)
    reference = {
        "vendor_media": {
            "vendor": "wecom.app",
            "account_id": "agent:1000002",
            "media_id": "MEDIA-EXISTING",
        }
    }

    plugin.send_message(
        {
            "conversation": conversation(),
            "content": [
                {
                    "kind": "file",
                    "reference": reference,
                    "file_name": "report.pdf",
                    "mime_type": "application/pdf",
                }
            ],
        }
    )
    assert transport.uploads == []
    assert transport.sends[0][1]["file"] == {"media_id": "MEDIA-EXISTING"}

    reference["vendor_media"]["account_id"] = "agent:other"
    with pytest.raises(ConnectorError) as mismatch:
        plugin.send_message(
            {
                "conversation": conversation(),
                "content": [{"kind": "file", "reference": reference}],
            }
        )
    assert mismatch.value.code == "INVALID_REQUEST"
    assert "binding" in mismatch.value.message


def test_media_upload_refreshes_an_expired_token_and_rejects_mixed_content() -> None:
    transport = ScriptedTransport(
        token_responses=[
            {"errcode": 0, "access_token": "token-old", "expires_in": 7200},
            {"errcode": 0, "access_token": "token-new", "expires_in": 7200},
        ],
        upload_responses=[
            {"errcode": 42001, "errmsg": "access_token expired"},
            {"errcode": 0, "errmsg": "ok", "media_id": "MEDIA-NEW"},
        ],
    )
    plugin = connector(transport)
    image = {
        "kind": "image",
        "reference": {"remote_uri": "https://cdn.example.test/photo.png"},
        "mime_type": "image/png",
    }

    plugin.send_message({"conversation": conversation(), "content": [image]})
    assert [item["token"] for item in transport.uploads] == ["token-old", "token-new"]
    assert transport.sends[0][0] == "token-new"

    with pytest.raises(ConnectorError) as mixed:
        plugin.send_message(
            {
                "conversation": conversation(),
                "content": [image, {"kind": "text", "text": "caption"}],
            }
        )
    assert mixed.value.code == "INVALID_REQUEST"
    assert "one media part" in mixed.value.message


def test_payload_validation_fails_closed() -> None:
    plugin = connector(ScriptedTransport())

    for request, fragment in (
        ({"content": [{"kind": "text", "text": "x"}]}, "conversation"),
        (
            {
                "conversation": conversation(vendor="onebot.v11"),
                "content": [{"kind": "text", "text": "x"}],
            },
            "vendor",
        ),
        (
            {
                "conversation": conversation(account_id="agent:999"),
                "content": [{"kind": "text", "text": "x"}],
            },
            "agent",
        ),
        (
            {
                "conversation": conversation(),
                "content": [{"kind": "text", "text": "x"}],
                "binding_id": "b",
            },
            "binding_id",
        ),
    ):
        with pytest.raises(ConnectorError) as error:
            plugin.send_message(request)
        assert error.value.code == "INVALID_REQUEST"
        assert fragment in error.value.message


def test_reply_reference_is_reported_as_ignored() -> None:
    transport = ScriptedTransport()
    plugin = connector(transport)
    result = plugin.send_message(
        {
            "conversation": conversation(),
            "content": [{"kind": "text", "text": "x"}],
            "reply": {"message_id": "ORIG-1"},
        }
    )
    assert result["vendor_facts"] == [
        {"name": "reply_reference_ignored", "value": "ORIG-1"}
    ]


def test_on_invoke_round_trips_the_typed_contract() -> None:
    transport = ScriptedTransport()
    plugin = connector(transport)

    request = message_contract.SendMessageRequest()
    request.conversation.vendor = "wecom.app"
    request.conversation.account_id = "agent:1000002"
    request.conversation.conversation_id = "zhangsan"
    request.conversation.kind = message_contract.CONVERSATION_KIND_PRIVATE
    request.content.add().text.text = "typed hello"

    ok, typed = plugin.on_invoke(
        CAPABILITY_ID,
        "send_message",
        request.SerializeToString(),
        request_type_url=SEND_MESSAGE_REQUEST_TYPE_URL,
    )
    assert ok is True
    assert typed.type_url == DELIVERY_RESULT_TYPE_URL
    delivery = message_contract.DeliveryResult()
    delivery.ParseFromString(typed.value)
    assert delivery.status == message_contract.DELIVERY_STATUS_ACCEPTED
    assert delivery.vendor_message_id == "MSG-1"
    assert delivery.vendor_extension.vendor == "wecom.app"

    ok, message = plugin.on_invoke(
        CAPABILITY_ID, "inbound_message", request.SerializeToString()
    )
    assert ok is False
    assert message.startswith("METHOD_NOT_FOUND")

    ok, message = plugin.on_invoke(
        CAPABILITY_ID,
        "send_message",
        b"\x00\x01\x02",
        request_type_url=SEND_MESSAGE_REQUEST_TYPE_URL,
    )
    assert ok is False
    assert message.startswith("INVALID_REQUEST")

    ok, message = plugin.on_invoke(
        CAPABILITY_ID,
        "send_message",
        request.SerializeToString(),
        request_type_url="type.cyrene.io/wrong",
    )
    assert ok is False
    assert message.startswith("INVALID_REQUEST")


def test_on_invoke_preserves_typed_attachment_references() -> None:
    transport = ScriptedTransport()
    plugin = connector(transport)
    request = message_contract.SendMessageRequest()
    request.conversation.vendor = "wecom.app"
    request.conversation.account_id = "agent:1000002"
    request.conversation.conversation_id = "zhangsan"
    request.conversation.kind = message_contract.CONVERSATION_KIND_PRIVATE
    request.content.add().file.reference.remote_uri = (
        "https://cdn.example.test/report.pdf"
    )
    request.content[0].file.file_name = "quarterly-report.pdf"
    request.content[0].file.mime_type = "application/pdf"

    ok, typed = plugin.on_invoke(
        CAPABILITY_ID,
        "send_message",
        request.SerializeToString(),
        request_type_url=SEND_MESSAGE_REQUEST_TYPE_URL,
    )

    assert ok is True
    assert typed.type_url == DELIVERY_RESULT_TYPE_URL
    assert transport.uploads[0]["remote_uri"].endswith("report.pdf")
    assert transport.uploads[0]["file_name"] == "quarterly-report.pdf"
    assert transport.sends[0][1]["file"] == {"media_id": "MEDIA-1"}


def test_configuration_comes_from_the_standard_environment(monkeypatch) -> None:
    monkeypatch.setenv("CYRENE_CAPABILITY_BINDING_ID", "wecom.env")
    monkeypatch.setenv(
        "CYRENE_CAPABILITY_CONFIGURATION_JSON",
        json.dumps(
            {
                "corp_id": "ww-env",
                "corp_secret": "env-secret",
                "agent_id": 42,
            }
        ),
    )
    plugin = WeComConnector()
    assert plugin.configured_binding_id == "wecom.env"

    config = WeComInstanceConfig.from_settings(
        {
            "binding_id": "wecom.env",
            "config": json.dumps(
                {"corp_id": "ww-env", "corp_secret": "s", "agent_id": 42}
            ),
        }
    )
    assert config == WeComInstanceConfig(
        binding_id="wecom.env", corp_id="ww-env", corp_secret="s", agent_id=42
    )
