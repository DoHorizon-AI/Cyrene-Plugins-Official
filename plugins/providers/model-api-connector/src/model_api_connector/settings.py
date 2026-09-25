"""Activation settings for one ``model.provider.v1`` binding.

中文：一个 ``model.provider.v1`` 绑定的激活设置。"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

BASE_URL_ENV = "CYRENE_CHAT_BASE_URL"
API_KEY_ENV = "CYRENE_CHAT_API_KEY"
TIMEOUT_ENV = "CYRENE_CHAT_TIMEOUT"
EMBEDDINGS_ENV = "CYRENE_EMBEDDINGS_SUPPORTED"

CHAT_COMPLETIONS_PATH = "/v1/chat/completions"
DEFAULT_TIMEOUT_SECONDS = 30.0


class ConfigurationError(ValueError):
    """The binding is unusable; the Plugin must fail closed, not guess.

        中文：此绑定不可用；Plugin 必须按失败即拒绝处理，不得猜测。"""


def _positive_float(raw: str | None, *, field: str, default: float) -> float:
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as error:
        raise ConfigurationError(f"{field} must be a number") from error
    if value <= 0:
        raise ConfigurationError(f"{field} must be positive")
    return value


def _boolean(raw: str | None, *, default: bool) -> bool:
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class ProviderSettings:
    """The upstream endpoint a single binding is allowed to reach.

        中文：单个绑定获准访问的上游端点。"""

    base_url: str
    api_key: str | None = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    embeddings_supported: bool = False

    def __post_init__(self) -> None:
        if not self.base_url.startswith(("http://", "https://")):
            raise ConfigurationError("base_url must be an http(s) URL")
        if self.timeout_seconds <= 0:
            raise ConfigurationError("timeout_seconds must be positive")

    @property
    def chat_completions_url(self) -> str:
        """Full upstream URL; the binding may carry a path prefix.

            中文：完整上游 URL；绑定可以包含路径前缀。"""

        return self.base_url.rstrip("/") + CHAT_COMPLETIONS_PATH

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> ProviderSettings:
        """Read the operator-provided activation environment.

            中文：读取操作者提供的激活环境。"""

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
        )
