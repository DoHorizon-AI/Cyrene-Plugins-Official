"""Activation settings for one ``model.provider.v1`` binding.

中文:一个 ``model.provider.v1`` 绑定的激活设置。"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

BASE_URL_ENV = "CYRENE_CHAT_BASE_URL"
API_KEY_ENV = "CYRENE_CHAT_API_KEY"
TIMEOUT_ENV = "CYRENE_CHAT_TIMEOUT"
EMBEDDINGS_ENV = "CYRENE_EMBEDDINGS_SUPPORTED"
EMBEDDINGS_MODEL_ENV = "CYRENE_EMBEDDINGS_MODEL"
EMBEDDINGS_BASE_URL_ENV = "CYRENE_EMBEDDINGS_BASE_URL"
EMBEDDINGS_API_KEY_ENV = "CYRENE_EMBEDDINGS_API_KEY"
EMBEDDINGS_TIMEOUT_ENV = "CYRENE_EMBEDDINGS_TIMEOUT"

CHAT_COMPLETIONS_PATH = "/v1/chat/completions"
EMBEDDINGS_PATH = "/v1/embeddings"
DEFAULT_TIMEOUT_SECONDS = 30.0


class ConfigurationError(ValueError):
    """The binding is unusable; the Plugin must fail closed, not guess.

    中文:此绑定不可用;Plugin 必须按失败即拒绝处理,不得猜测。"""


def _positive_float(raw: str | None, *, field: str, default: float) -> float:
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as error:
        raise ConfigurationError(f"{field} must be a number") from error
    if not math.isfinite(value) or value <= 0:
        raise ConfigurationError(f"{field} must be positive")
    return value


def _boolean(raw: str | None, *, default: bool) -> bool:
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _validate_endpoint_base_url(value: str, field_name: str) -> None:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ConfigurationError(
            f"{field_name} must be an http(s) base URL without userinfo or fragment"
        )


@dataclass(frozen=True, slots=True)
class ProviderSettings:
    """The upstream endpoint a single binding is allowed to reach.

    中文:单个绑定获准访问的上游端点。"""

    base_url: str
    api_key: str | None = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    embeddings_supported: bool = False
    embeddings_model: str | None = None
    embeddings_base_url: str | None = None
    embeddings_api_key: str | None = None
    embeddings_timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        _validate_endpoint_base_url(self.base_url, "base_url")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ConfigurationError("timeout_seconds must be a finite positive number")
        if (
            not math.isfinite(self.embeddings_timeout_seconds)
            or self.embeddings_timeout_seconds <= 0
        ):
            raise ConfigurationError("embeddings_timeout_seconds must be a finite positive number")
        if self.embeddings_model is not None and (
            not isinstance(self.embeddings_model, str) or not self.embeddings_model.strip()
        ):
            raise ConfigurationError("embeddings_model must be non-empty text when provided")
        if self.embeddings_model is not None and len(self.embeddings_model) > 256:
            raise ConfigurationError("embeddings_model exceeds the maximum length")
        if self.embeddings_supported and not self.embeddings_base_url:
            raise ConfigurationError("embeddings_base_url is required when embeddings are enabled")
        if self.embeddings_base_url is not None:
            _validate_endpoint_base_url(self.embeddings_base_url, "embeddings_base_url")

    @property
    def chat_completions_url(self) -> str:
        """Full upstream URL; the binding may carry a path prefix.

        中文:完整上游 URL;绑定可以包含路径前缀。"""

        return _operation_url(self.base_url, CHAT_COMPLETIONS_PATH)

    @property
    def embeddings_url(self) -> str:
        """Return the embeddings operation endpoint for this binding.

        中文:返回此 binding 的 embeddings 操作 endpoint。
        """

        if self.embeddings_base_url is None:
            raise ConfigurationError(
                "CYRENE_EMBEDDINGS_BASE_URL is required when embeddings are enabled"
            )
        return _operation_url(self.embeddings_base_url, EMBEDDINGS_PATH)

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> ProviderSettings:
        """Read the operator-provided activation environment.

        中文:读取操作者提供的激活环境。"""

        source = os.environ if environ is None else environ
        raw_base = (source.get(BASE_URL_ENV) or "").strip()
        if not raw_base:
            raise ConfigurationError(f"{BASE_URL_ENV} is required")
        api_key = (source.get(API_KEY_ENV) or "").strip() or None
        return cls(
            base_url=raw_base,
            api_key=api_key,
            timeout_seconds=_positive_float(
                source.get(TIMEOUT_ENV),
                field=TIMEOUT_ENV,
                default=DEFAULT_TIMEOUT_SECONDS,
            ),
            embeddings_supported=_boolean(source.get(EMBEDDINGS_ENV), default=False),
            embeddings_model=(source.get(EMBEDDINGS_MODEL_ENV) or "").strip() or None,
            embeddings_base_url=(source.get(EMBEDDINGS_BASE_URL_ENV) or "").strip() or None,
            embeddings_api_key=(source.get(EMBEDDINGS_API_KEY_ENV) or "").strip() or None,
            embeddings_timeout_seconds=_positive_float(
                source.get(EMBEDDINGS_TIMEOUT_ENV),
                field=EMBEDDINGS_TIMEOUT_ENV,
                default=DEFAULT_TIMEOUT_SECONDS,
            ),
        )


def _operation_url(base_url: str, operation_path: str) -> str:
    """Append an OpenAI operation path while accepting a base URL ending in /v1.

    中文:追加 OpenAI 操作路径，并兼容以 `/v1` 结尾的 base URL。
    """

    parsed = urlsplit(base_url)
    base_path = parsed.path.rstrip("/")
    suffix = operation_path
    if base_path.endswith("/v1") and operation_path.startswith("/v1/"):
        suffix = operation_path[len("/v1") :]
    return urlunsplit((parsed.scheme, parsed.netloc, f"{base_path}{suffix}", parsed.query, ""))
