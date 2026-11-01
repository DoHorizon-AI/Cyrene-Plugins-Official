"""Signed-subject media state isolation tests.

The provider receives only contexts accepted by its HMAC verifier at runtime.
This suite checks that distinct signed subjects resolve distinct catalogs.
中文：不同已验证subject映射到独立目录。
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from cyrene_media import MediaError
from cyrene_media.plugin import MediaProvider
from cyrene_media.settings import MediaSettings, TrustedCaller


def _image(color: tuple[int, int, int]) -> bytes:
    """Create a local PNG fixture."""

    stream = io.BytesIO()
    Image.new("RGB", (4, 4), color).save(stream, format="PNG")
    return stream.getvalue()


class _NoopResolver:
    """Resolver placeholder that fails if the state test invokes external I/O."""

    def read(self, *_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("state partition test must remain local")

    def write(self, *_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("state partition test must remain local")


def _context(subject: str) -> dict[str, str]:
    """Build identity scope data after the plugin's HMAC verification boundary."""

    return {
        "issuer": "https://identity.local",
        "subject": subject,
        "organization_id": "org-1",
        "workspace_id": "workspace-1",
    }


def test_actor_catalogs_are_isolated_in_one_long_lived_provider(
    tmp_path: Path,
) -> None:
    caller = TrustedCaller(
        caller_workload="cyrene-navigator",
        audience="media-library",
        organization_id="org-1",
        workspace_id="workspace-1",
        issuer="https://identity.local",
        verification_key=b"x" * 32,
    )
    settings = MediaSettings(
        binding_id="media-binding-1",
        state_dir=tmp_path / "state",
        timeout_seconds=5,
        trusted_callers=(caller,),
        artifact_resolver=_NoopResolver(),  # type: ignore[arg-type]
    )
    provider = MediaProvider(settings=settings)
    actor_a = _context("https://identity.local/subjects/actor-a")
    actor_b = _context("https://identity.local/subjects/actor-b")

    library_a = provider._library_for(actor_a)
    asset_a = library_a.put(_image((100, 20, 30)), title="Actor A")
    library_b = provider._library_for(actor_b)

    assert library_a is provider._library_for(actor_a)
    assert library_a.root != library_b.root
    assert library_a.get(asset_a.asset_id)[1] == _image((100, 20, 30))
    with pytest.raises(MediaError, match="NOT_FOUND"):
        library_b.get(asset_a.asset_id)
