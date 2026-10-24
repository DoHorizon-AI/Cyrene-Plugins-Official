"""Fixed-host Hub API transport and artifact safety coverage.

All requests are intercepted locally; tests do not access Hugging Face. 中文：测试不访问真实 Hub。
"""

from __future__ import annotations

from typing import Any

import pytest

from huggingface_provider.huggingface_api import (
    HttpResponse,
    HuggingFaceApi,
    HuggingFaceApiFailure,
)
from huggingface_provider.settings import ProviderSettings, RepositoryScope


def make_settings() -> ProviderSettings:
    """Build one model/dataset/Space binding with a one-MiB transfer cap."""

    return ProviderSettings(
        binding_id="hf.binding.test",
        account_center_binding_id="account-center.binding.test",
        account_center_connection_ref="grpc://127.0.0.1:40000",
        account_center_transport="local",
        account_center_root_cert_ref=None,
        account_center_client_cert_ref=None,
        account_center_client_key_ref=None,
        host_secret_names={},
        repositories=(
            RepositoryScope("model", "owner/model", frozenset({"read", "write"})),
            RepositoryScope("dataset", "owner/data", frozenset({"read"})),
            RepositoryScope("space", "owner/demo", frozenset({"read"})),
        ),
        provider_caller_id="huggingface-provider",
        exchange_caller_id="exchange",
        exchange_verification_key=b"x" * 32,
        provider_signing_key=b"p" * 32,
    )


def test_public_list_filters_private_results_and_projects_metadata() -> None:
    observed: list[tuple[str, str, str, dict[str, str]]] = []

    def transport(
        method: str,
        host: str,
        path: str,
        headers: dict[str, str],
        body: bytes | None,
    ) -> HttpResponse:
        observed.append((method, host, path, headers))
        return HttpResponse(
            200,
            {"content-type": "application/json"},
            b'[{"id":"public/model","private":false,"downloads":7,"likes":2,"tags":["text-generation"]},{"id":"private/model","private":true}]',
        )

    api = HuggingFaceApi(make_settings(), transport=transport)
    result = api.list_repositories("model", "hf_private_token", search="bert", limit=5)

    assert result["repositories"] == [
        {
            "repo_id": "public/model",
            "private": False,
            "gated": False,
            "sha": None,
            "last_modified": None,
            "downloads": 7,
            "likes": 2,
            "tags": ["text-generation"],
            "pipeline_tag": None,
        }
    ]
    method, host, path, headers = observed[0]
    assert (method, host, path) == ("GET", "huggingface.co", "/api/models?limit=5&search=bert")
    assert headers["Authorization"] == "Bearer hf_private_token"


def test_download_follows_signed_cdn_url_without_forwarding_hub_token() -> None:
    calls: list[tuple[str, str, dict[str, str]]] = []

    def transport(
        method: str,
        host: str,
        path: str,
        headers: dict[str, str],
        body: bytes | None,
    ) -> HttpResponse:
        calls.append((host, path, headers))
        if host == "huggingface.co":
            return HttpResponse(
                302,
                {"location": "https://cdn-lfs.huggingface.co/blob?signature=private"},
                b"",
            )
        return HttpResponse(200, {"content-type": "text/plain"}, b"artifact")

    api = HuggingFaceApi(make_settings(), transport=transport)
    result = api.download_file("owner/model", "main", "config.json", "hf_secret")

    assert result["content_base64"] == "YXJ0aWZhY3Q="
    assert calls[0][0] == "huggingface.co"
    assert calls[0][2]["Authorization"] == "Bearer hf_secret"
    assert calls[1][0] == "cdn-lfs.huggingface.co"
    assert "Authorization" not in calls[1][2]


def test_download_rejects_untrusted_redirect_host() -> None:
    def transport(*_: Any) -> HttpResponse:
        return HttpResponse(
            302,
            {"location": "https://cdn.huggingface.co.attacker.example/file"},
            b"",
        )

    api = HuggingFaceApi(make_settings(), transport=transport)
    with pytest.raises(HuggingFaceApiFailure, match="UNTRUSTED_DOWNLOAD_REDIRECT"):
        api.download_file("owner/model", "main", "weights.bin", "hf_secret")


def test_upload_is_single_attempt_and_requires_tree_readback() -> None:
    calls: list[tuple[str, str, bytes | None]] = []

    def transport(
        method: str,
        host: str,
        path: str,
        headers: dict[str, str],
        body: bytes | None,
    ) -> HttpResponse:
        calls.append((method, path, body))
        if method == "PUT":
            assert host == "huggingface.co"
            assert headers["Authorization"] == "Bearer hf_secret"
            return HttpResponse(200, {}, b'{"commitUrl":"https://huggingface.co/owner/model/commit/abc"}')
        return HttpResponse(200, {}, b'[{"path":"config.json","type":"file"}]')

    api = HuggingFaceApi(make_settings(), transport=transport)
    result = api.upload_file(
        "model", "owner/model", "main", "config.json", b"{}", "hf_secret"
    )

    assert result["status"] == "CONFIRMED"
    assert calls[0] == ("PUT", "/api/models/owner/model/upload/main/config.json", b"{}")
    assert calls[1][0] == "GET"
    assert calls[1][1] == "/api/models/owner/model/tree/main?recursive=false&expand=false&limit=100"


def test_uncertain_upload_is_not_retried_and_errors_are_sanitized() -> None:
    calls = 0

    def transport(*_: Any) -> HttpResponse:
        nonlocal calls
        calls += 1
        raise TimeoutError("token and private body must never escape")

    api = HuggingFaceApi(make_settings(), transport=transport)
    with pytest.raises(HuggingFaceApiFailure) as caught:
        api.upload_file("model", "owner/model", "main", "config.json", b"{}", "secret")
    assert caught.value.code == "UPSTREAM_UNCERTAIN"
    assert caught.value.uncertain is True
    assert calls == 1
    assert "secret" not in str(caught.value)


def test_upload_503_is_uncertain_and_not_safe_to_retry() -> None:
    calls: list[str] = []

    def transport(
        method: str,
        host: str,
        path: str,
        headers: dict[str, str],
        body: bytes | None,
    ) -> HttpResponse:
        calls.append(method)
        return HttpResponse(503, {"retry-after": "10"}, b"private response")

    api = HuggingFaceApi(make_settings(), transport=transport)
    with pytest.raises(HuggingFaceApiFailure) as caught:
        api.upload_file("model", "owner/model", "main", "config.json", b"{}", "hf_secret")
    assert caught.value.code == "UPSTREAM_UNCERTAIN"
    assert caught.value.retryable is False
    assert caught.value.uncertain is True
    assert calls == ["PUT"]


@pytest.mark.parametrize(
    ("status", "expected", "retryable"),
    [
        (401, "AUTHENTICATION_FAILED", False),
        (403, "AUTHORIZATION_DENIED", False),
        (429, "RATE_LIMITED", True),
    ],
)
def test_auth_permission_and_rate_limit_errors_are_not_empty_successes(
    status: int, expected: str, retryable: bool
) -> None:
    def transport(*_: Any) -> HttpResponse:
        return HttpResponse(status, {"retry-after": "15"}, b"private response detail")

    api = HuggingFaceApi(make_settings(), transport=transport)
    with pytest.raises(HuggingFaceApiFailure) as caught:
        api.list_repositories("model", "hf_secret")
    assert caught.value.code == expected
    assert caught.value.retryable is retryable
    assert caught.value.retry_after == (15 if retryable else None)
    assert "private response detail" not in str(caught.value)
