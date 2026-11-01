"""Local SQLite media catalog and rendering tests.

All fixtures are generated in the test directory. 中文：测试数据仅写入临时目录。
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest
from PIL import Image

from cyrene_media import MediaError, MediaLibrary


def _image_bytes(color: tuple[int, int, int]) -> bytes:
    """Create a small, deterministic PNG fixture."""

    image = Image.new("RGB", (12, 8), color)
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


def test_put_deduplicates_exact_content_and_verifies_readback(tmp_path: Path) -> None:
    library = MediaLibrary(tmp_path / "state")
    content = _image_bytes((20, 40, 60))

    first = library.put(content, title="Sample", alt_text="A sample")
    duplicate = library.put(content, title="Different metadata")
    readback, returned = library.get(first.asset_id)

    assert duplicate.asset_id == first.asset_id
    assert readback.digest == hashlib.sha256(content).hexdigest()
    assert returned == content
    assert library.validate(first.asset_id)["valid"] is True


def test_soft_delete_hides_asset_and_restore_recovers_it(tmp_path: Path) -> None:
    library = MediaLibrary(tmp_path / "state")
    asset = library.put(_image_bytes((80, 100, 120)), title="Recover me")

    assert library.delete(asset.asset_id) is True
    with pytest.raises(MediaError, match="NOT_FOUND"):
        library.get(asset.asset_id)
    restored = library.restore(asset.asset_id)

    assert restored.asset_id == asset.asset_id
    assert library.get(asset.asset_id)[0].deleted_at is None


def test_read_rejects_tampered_blob(tmp_path: Path) -> None:
    library = MediaLibrary(tmp_path / "state")
    asset = library.put(_image_bytes((10, 20, 30)), title="Integrity")
    path = library._blob_path(asset.digest)
    path.write_bytes(b"tampered")

    with pytest.raises(MediaError, match="INTEGRITY_FAILURE"):
        library.get(asset.asset_id)


def test_oversized_blob_is_rejected_before_opening_or_allocating(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cyrene_media.library as media_library

    library = MediaLibrary(tmp_path / "state")
    asset = library.put(_image_bytes((25, 35, 45)), title="Bounded read")
    path = library._blob_path(asset.digest)
    with path.open("wb") as stream:
        stream.truncate(10 * 1024 * 1024 + 1)

    def unexpected_open(*_args: object, **_kwargs: object) -> int:
        raise AssertionError("oversized file should be rejected from lstat")

    monkeypatch.setattr(media_library.os, "open", unexpected_open)
    with pytest.raises(MediaError, match="INTEGRITY_FAILURE"):
        library.get(asset.asset_id)


def test_symlink_blob_is_rejected(tmp_path: Path) -> None:
    library = MediaLibrary(tmp_path / "state")
    asset = library.put(_image_bytes((35, 45, 55)), title="No symlink")
    path = library._blob_path(asset.digest)
    target = tmp_path / "external.blob"
    target.write_bytes(_image_bytes((35, 45, 55)))
    path.unlink()
    path.symlink_to(target)

    with pytest.raises(MediaError, match="INTEGRITY_FAILURE"):
        library.get(asset.asset_id)


def test_list_pages_are_bounded_and_resume_from_cursor(tmp_path: Path) -> None:
    library = MediaLibrary(tmp_path / "state")
    for index in range(3):
        library.put(_image_bytes((index * 20, 40, 60)), title=f"entry {index}")

    first = library.list_assets(limit=2)
    second = library.list_assets(limit=2, cursor=first["next_cursor"])

    assert len(first["items"]) == 2
    assert first["next_cursor"]
    assert len(second["items"]) == 1
    assert second["next_cursor"] is None


def test_invalid_cursor_and_oversized_image_are_rejected(tmp_path: Path) -> None:
    library = MediaLibrary(tmp_path / "state")
    with pytest.raises(MediaError, match="INVALID_INPUT"):
        library.list_assets(cursor="arbitrary")
    with pytest.raises(MediaError, match="QUOTA_EXCEEDED"):
        library.put(b"x" * (10 * 1024 * 1024 + 1), title="large")


def test_card_render_creates_verified_png_asset(tmp_path: Path) -> None:
    library = MediaLibrary(tmp_path / "state")

    asset, content = library.render_card(
        title="Release update",
        body="The local service is ready for review.",
        theme="dark",
    )
    readback, actual = library.get(asset.asset_id)

    assert content.startswith(b"\x89PNG\r\n\x1a\n")
    assert readback.mime_type == "image/png"
    assert len(actual) == len(content)


def test_cjk_card_requires_a_cjk_operator_font(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cyrene_media.library as media_library

    monkeypatch.delenv("CYRENE_MEDIA_FONT", raising=False)
    monkeypatch.setattr(
        media_library,
        "_CJK_FONT_PATHS",
        (),
    )
    library = MediaLibrary(tmp_path / "state")

    with pytest.raises(MediaError, match="FONT_UNAVAILABLE"):
        library.render_card(title="提醒", body="这是中文卡片")
