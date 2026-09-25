"""Read one Plugin activation configuration from the standard worker environment.

中文:从标准工作进程环境中读取一份 Plugin 激活配置。"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping

CONFIGURATION_ENV = "CYRENE_CAPABILITY_CONFIGURATION_JSON"
BINDING_ID_ENV = "CYRENE_CAPABILITY_BINDING_ID"
MAX_CONFIGURATION_BYTES = 64 * 1024


def read_environment_settings() -> dict[str, str] | None:
    """Return generic worker settings for a Plugin ``from_settings`` factory.

        中文:为 Plugin 的 ``from_settings`` 工厂返回通用工作进程设置。"""

    binding_id = os.environ.get(BINDING_ID_ENV)
    encoded = os.environ.get(CONFIGURATION_ENV)
    if not binding_id and encoded is None:
        return None
    if not binding_id:
        raise ValueError(
            f"{BINDING_ID_ENV} is required when Plugin configuration is set"
        )
    if encoded is None:
        encoded = "{}"
    if len(encoded.encode("utf-8")) > MAX_CONFIGURATION_BYTES:
        raise ValueError(f"{CONFIGURATION_ENV} exceeds {MAX_CONFIGURATION_BYTES} bytes")
    try:
        decoded = json.loads(encoded)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{CONFIGURATION_ENV} must contain valid JSON") from exc
    if not isinstance(decoded, Mapping):
        raise TypeError(f"{CONFIGURATION_ENV} must contain a JSON object")
    if any(not isinstance(key, str) for key in decoded):
        raise ValueError(f"{CONFIGURATION_ENV} field names must be strings")
    return {"binding_id": binding_id, "config": encoded}
