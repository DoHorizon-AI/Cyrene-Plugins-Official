"""Visible-text extraction for bounded HTML pages."""

from __future__ import annotations

import re
from html.parser import HTMLParser

_IGNORED_TAGS = frozenset(
    {"script", "style", "noscript", "svg", "template", "head", "iframe", "object"}
)
_WHITESPACE = re.compile(r"[\t\r\f\v ]+")
_EXCESS_NEWLINES = re.compile(r"\n{3,}")
_MAX_VISIBLE_TEXT_CHARS = 500_000
_MAX_HTML_NODES = 100_000


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self._ignored_depth = 0
        self._title_depth = 0
        self._node_count = 0
        self._visible_chars = 0
        self.truncated = False

    def handle_starttag(self, tag: str, attrs) -> None:
        if not self._visit_node():
            return
        normalized = tag.lower()
        if normalized in _IGNORED_TAGS:
            self._ignored_depth += 1
        if normalized == "title":
            self._title_depth += 1
        if normalized in {
            "p",
            "div",
            "br",
            "li",
            "tr",
            "h1",
            "h2",
            "h3",
            "h4",
            "section",
            "article",
        }:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if not self._visit_node():
            return
        normalized = tag.lower()
        if normalized in _IGNORED_TAGS and self._ignored_depth:
            self._ignored_depth -= 1
        if normalized == "title" and self._title_depth:
            self._title_depth -= 1
        if normalized in {"p", "div", "li", "tr", "h1", "h2", "h3", "h4", "section", "article"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._visit_node():
            return
        normalized = _WHITESPACE.sub(" ", data).strip()
        if normalized:
            if self._title_depth:
                title_remaining = 512 - sum(len(part) for part in self.title_parts)
                if title_remaining > 0:
                    self.title_parts.append(normalized[:title_remaining])
            if not self._ignored_depth:
                remaining = _MAX_VISIBLE_TEXT_CHARS - self._visible_chars
                if remaining > 0:
                    visible = normalized[:remaining]
                    self.parts.append(visible)
                    self._visible_chars += len(visible)
                    if len(visible) != len(normalized):
                        self.truncated = True
                else:
                    self.truncated = True

    def _visit_node(self) -> bool:
        self._node_count += 1
        if self._node_count > _MAX_HTML_NODES:
            self.truncated = True
            return False
        return True


def extract_visible_text(
    data: bytes, charset: str | None
) -> tuple[str, str | None, bool]:
    if len(data) > 2 * 1024 * 1024:
        raise ValueError("HTML source exceeds its byte limit")
    encoding = charset or "utf-8"
    try:
        html = data.decode(encoding, errors="strict")
    except (LookupError, UnicodeDecodeError):
        try:
            html = data.decode("utf-8", errors="replace")
        except UnicodeDecodeError as error:
            raise ValueError("HTML source could not be decoded") from error
    parser = _VisibleTextParser()
    parser.feed(html)
    parser.close()
    text = "".join(parser.parts)
    text = _EXCESS_NEWLINES.sub("\n\n", text).strip()
    title = " ".join(parser.title_parts).strip()[:512] or None
    return text[:_MAX_VISIBLE_TEXT_CHARS], title, parser.truncated
