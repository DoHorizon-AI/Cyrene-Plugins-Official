"""Validation and assembly helpers for the workspace people directory.

A person is one human of a workspace. Their accounts on other platforms come
from two sources: handles an operator declared, and links the person verified
themselves. Declared handles are addresses only; they never carry credentials.
中文：人员目录的校验与组装。声明的账号只是地址，不携带任何凭据。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .models import AccountCenterError

HANDLE_PROVIDERS = (
    "github",
    "huggingface",
    "azure_devops",
    "wecom",
    "qq",
    "bilibili",
)
FILTER_PROVIDERS = (
    *HANDLE_PROVIDERS,
    "azure",
    "gcp",
    "runpod",
    "microsoft_graph",
)
DEFAULT_PEOPLE_LIMIT = 100
MAX_PEOPLE_LIMIT = 500

_CASE_INSENSITIVE = frozenset({"github", "huggingface", "azure_devops"})
_HANDLE_PATTERNS = {
    "github": re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$"),
    "huggingface": re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$"),
    "azure_devops": re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$"),
    "wecom": re.compile(r"^[A-Za-z0-9_.@-]{1,64}$"),
    "qq": re.compile(r"^[0-9]{5,12}$"),
    "bilibili": re.compile(r"^[1-9][0-9]{0,19}$"),
}
_MAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_UUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _text(value: Any, *, maximum: int) -> str:
    """Return one stripped printable string or raise the fixed request error."""

    if not isinstance(value, str):
        raise AccountCenterError("invalid_request")
    text = value.strip()
    if not text or len(text) > maximum:
        raise AccountCenterError("invalid_request")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in text):
        raise AccountCenterError("invalid_request")
    return text


def normalize_provider(value: Any, *, allowed: Sequence[str] = HANDLE_PROVIDERS) -> str:
    """Accept only a provider that can carry a declared handle."""

    if not isinstance(value, str) or value not in allowed:
        raise AccountCenterError("invalid_request")
    return value


def normalize_handle(provider: str, value: Any) -> tuple[str, str]:
    """Return the handle as written plus its provider-aware comparison key."""

    handle = _text(value, maximum=254)
    pattern = _HANDLE_PATTERNS.get(provider)
    if pattern is None or pattern.fullmatch(handle) is None:
        raise AccountCenterError("invalid_request")
    return handle, handle_key(provider, handle)


def handle_key(provider: str, handle: str) -> str:
    """Compare handles case-insensitively only where the platform does."""

    return handle.lower() if provider in _CASE_INSENSITIVE else handle


def normalize_mail(value: Any) -> str:
    """Lower-case one e-mail address; the directory never stores display forms."""

    mail = _text(value, maximum=254).lower()
    if _MAIL.fullmatch(mail) is None:
        raise AccountCenterError("invalid_request")
    return mail


def normalize_subject(value: Any) -> str:
    """Keep an Entra object ID in its canonical lower-case form."""

    subject = _text(value, maximum=256)
    if any(character.isspace() for character in subject):
        raise AccountCenterError("invalid_request")
    return subject.lower() if _UUID.fullmatch(subject) else subject


def normalize_display_name(value: Any) -> str:
    """Validate a human-readable name shown to the assistant."""

    return _text(value, maximum=120)


def normalize_limit(value: Any) -> int:
    """Bound one response page; booleans are not integers here."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise AccountCenterError("invalid_request")
    if not 1 <= value <= MAX_PEOPLE_LIMIT:
        raise AccountCenterError("invalid_request")
    return value


def assemble_people(
    people: Iterable[Mapping[str, Any]],
    handles: Iterable[Mapping[str, Any]],
    accounts: Iterable[Mapping[str, Any]],
    profiles: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Merge declared people and handles with the owners of verified links.

    A verified link joins a declared person when issuer and subject match;
    otherwise its owner is listed as a person known only by identity.
    中文：已验证关联按 issuer 与 subject 并入已声明的人，否则单独成条。
    """

    persons: dict[str, dict[str, Any]] = {}
    owners: dict[tuple[str, str], str] = {}
    for row in people:
        persons[row["person_ref"]] = {
            "person_ref": row["person_ref"],
            "issuer": row["issuer"],
            "subject": row["subject"],
            "mail": row["mail"],
            "display_name": row["display_name"],
            "accounts": [],
        }
        if row["subject"] is not None:
            owners[(row["issuer"], row["subject"])] = row["person_ref"]
    for row in handles:
        person = persons.get(row["person_ref"])
        if person is not None:
            person["accounts"].append(
                {
                    "source": "declared",
                    "kind": "handle",
                    "provider": row["provider"],
                    "handle": row["handle"],
                    "external_subject": None,
                    "status": "active",
                    "updated_at": row["updated_at"],
                }
            )
    for kind, rows in (("account", accounts), ("profile", profiles)):
        for row in rows:
            owner = (row["anchor_issuer"], row["anchor_subject"])
            key = owners.get(owner)
            if key is None:
                key = f"anchor\x00{owner[0]}\x00{owner[1]}"
                owners[owner] = key
                persons[key] = {
                    "person_ref": None,
                    "issuer": owner[0],
                    "subject": owner[1],
                    "mail": None,
                    "display_name": None,
                    "accounts": [],
                }
            persons[key]["accounts"].append(
                {
                    "source": "verified",
                    "kind": kind,
                    "provider": row["provider"],
                    "handle": None,
                    "external_subject": row["external_subject"],
                    "status": row["status"],
                    "updated_at": row["linked_at"],
                }
            )
    result = list(persons.values())
    for person in result:
        person["accounts"].sort(
            key=lambda item: (
                item["source"],
                item["provider"],
                item["handle"] or item["external_subject"] or "",
                item["updated_at"],
            )
        )
    result.sort(
        key=lambda person: (
            (person["display_name"] or person["mail"] or person["subject"] or "").casefold(),
            person["issuer"],
            person["subject"] or "",
        )
    )
    return result


def _account_matches(account: Mapping[str, Any], provider: str, wanted: str) -> bool:
    """Match a declared handle or a verified external subject to one key."""

    value = account["handle"] if account["source"] == "declared" else account["external_subject"]
    return value is not None and handle_key(provider, str(value)) == wanted


def filter_people(
    people: Sequence[dict[str, Any]],
    *,
    subject: str | None,
    mail: str | None,
    provider: str | None,
    handle: str | None,
    name: str | None,
) -> list[dict[str, Any]]:
    """Keep the people matching every given filter; the filters are ANDed."""

    wanted = handle_key(provider, handle) if provider and handle is not None else None
    needle = name.casefold() if name else None
    matched = []
    for person in people:
        if subject is not None and person["subject"] != subject:
            continue
        if mail is not None and person["mail"] != mail:
            continue
        if provider is not None:
            accounts = [item for item in person["accounts"] if item["provider"] == provider]
            if not accounts:
                continue
            if wanted is not None and not any(
                _account_matches(item, provider, wanted) for item in accounts
            ):
                continue
        if needle is not None:
            haystack = f"{person['display_name'] or ''} {person['mail'] or ''}".casefold()
            if needle not in haystack:
                continue
        matched.append(person)
    return matched
