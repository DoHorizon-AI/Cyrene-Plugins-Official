from __future__ import annotations

from cyrene_public_web.html_text import extract_visible_text


def test_visible_text_skips_active_content_but_preserves_title() -> None:
    source = (
        b"<html><head><title>Example title</title><script>head-secret</script></head>"
        b"<body><style>hidden-style</style><p>Visible text</p></body></html>"
    )
    text, title, truncated = extract_visible_text(source, "utf-8")
    assert title == "Example title"
    assert "Visible text" in text
    assert "head-secret" not in text
    assert "hidden-style" not in text
    assert truncated is False


def test_visible_text_has_a_hard_character_limit() -> None:
    text, _title, truncated = extract_visible_text(
        b"<html><body>" + b"x" * 600_000 + b"</body></html>",
        "utf-8",
    )
    assert len(text) == 500_000
    assert truncated is True
