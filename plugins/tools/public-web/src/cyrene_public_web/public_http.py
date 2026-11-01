"""Pinned-address HTTP reads with public-IP, redirect, and body-size guards."""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlsplit, urlunsplit

_DNS_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="public-web-dns")
_DNS_SLOTS = threading.BoundedSemaphore(8)
_HTTP_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="public-web-http")
_HTTP_SLOTS = threading.BoundedSemaphore(16)


class PublicHttpError(RuntimeError):
    """A fixed-host HTTP read was rejected or failed safely."""

    def __init__(self, code: str, *, retryable: bool = False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class HttpDocument:
    """Bounded response metadata and content from one non-redirected GET."""

    requested_url: str
    content_type: str
    charset: str | None
    status_code: int
    headers: dict[str, str]
    body: bytes


def canonicalize_public_url(
    value: str,
    loopback_test_origins: frozenset[str],
    *,
    dns_timeout_seconds: float = 3,
) -> str:
    """Validate a result URL and reject private DNS targets before returning it."""

    target = _parse_url(value, loopback_test_origins)
    _resolve_public_address(
        target.hostname,
        target.port,
        target.loopback,
        timeout_seconds=dns_timeout_seconds,
    )
    return target.canonical_url


def validate_result_url(value: str, loopback_test_origins: frozenset[str]) -> str:
    """Sanitize a search result without making a request to its host."""

    target = _parse_url(value, loopback_test_origins)
    try:
        literal = ipaddress.ip_address(target.hostname)
    except ValueError:
        return target.canonical_url
    _validate_ip(literal, target.loopback)
    return target.canonical_url


def fetch_get(
    url: str,
    *,
    timeout_seconds: float,
    max_response_bytes: int,
    loopback_test_origins: frozenset[str],
    extra_headers: dict[str, str] | None = None,
    accept: str | None = None,
    cancellation: object = None,
) -> HttpDocument:
    """Resolve once, pin the connection, and enforce a total request deadline."""

    _check_cancelled(cancellation)
    target = _parse_url(url, loopback_test_origins)
    address = _resolve_public_address(
        target.hostname,
        target.port,
        target.loopback,
        timeout_seconds=min(timeout_seconds, 3),
    )
    connection: http.client.HTTPConnection
    if target.scheme == "https":
        connection = _PinnedHTTPSConnection(
            target.hostname,
            target.port,
            address,
            timeout_seconds,
        )
    else:
        connection = _PinnedHTTPConnection(
            target.hostname,
            target.port,
            address,
            timeout_seconds,
        )
    headers = {
        "Host": _host_header(target.hostname, target.port, target.scheme),
        "Accept": accept
        or "text/html,application/xhtml+xml,application/pdf,"
        "application/vnd.openxmlformats-officedocument.*,text/plain,text/markdown,text/csv,image/png,image/jpeg",
        "Accept-Encoding": "identity",
        "User-Agent": "Cyrene-Public-Web/0.1 (+https://cyrene.io)",
    }
    if extra_headers:
        for name, value in extra_headers.items():
            if name.lower() not in {"authorization", "accept", "content-type"}:
                raise PublicHttpError("INVALID_REQUEST")
            if any(char in name + value for char in "\r\n"):
                raise PublicHttpError("INVALID_REQUEST")
            headers[name] = value
    if not _HTTP_SLOTS.acquire(timeout=0.05):
        raise PublicHttpError("REQUEST_BUSY", retryable=True)
    try:
        try:
            future = _HTTP_POOL.submit(
                _perform_get,
                connection,
                target,
                headers,
                max_response_bytes,
            )
        except RuntimeError:
            _HTTP_SLOTS.release()
            connection.close()
            raise PublicHttpError("REQUEST_BUSY", retryable=True) from None
        future.add_done_callback(lambda _future: _HTTP_SLOTS.release())
        deadline = time.monotonic() + timeout_seconds
        while True:
            _check_cancelled(cancellation)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                connection.close()
                raise PublicHttpError("REQUEST_TIMEOUT", retryable=True)
            try:
                return future.result(timeout=min(0.05, remaining))
            except FutureTimeout:
                continue
    except InterruptedError:
        connection.close()
        if "future" in locals():
            future.cancel()
        raise
    finally:
        connection.close()


def _perform_get(
    connection: http.client.HTTPConnection,
    target: _Target,
    headers: dict[str, str],
    max_response_bytes: int,
) -> HttpDocument:
    """Perform one bounded GET in the cancellable, deadline-supervised worker."""

    try:
        connection.request("GET", target.path_and_query, headers=headers)
        response = connection.getresponse()
        status = response.status
        if 300 <= status < 400:
            raise PublicHttpError("REDIRECT_REJECTED")
        if status != 200:
            raise PublicHttpError("HTTP_FAILURE", retryable=status >= 500)
        encoding = response.getheader("Content-Encoding", "identity").strip().lower()
        if encoding not in {"", "identity"}:
            raise PublicHttpError("UNSUPPORTED_CONTENT_ENCODING")
        content_length = response.getheader("Content-Length")
        if content_length is not None:
            try:
                if int(content_length) > max_response_bytes:
                    raise PublicHttpError("RESPONSE_LIMIT_EXCEEDED")
            except ValueError:
                raise PublicHttpError("INVALID_RESPONSE") from None
        body = response.read(max_response_bytes + 1)
        if len(body) > max_response_bytes:
            raise PublicHttpError("RESPONSE_LIMIT_EXCEEDED")
        content_type = response.getheader("Content-Type", "application/octet-stream")
        media_type, charset = _parse_content_type(content_type)
        allowed_headers = {
            name.lower(): value[:512]
            for name, value in response.getheaders()
            if name.lower() in {"content-type", "last-modified", "etag", "date"}
        }
        return HttpDocument(
            requested_url=target.canonical_url,
            content_type=media_type,
            charset=charset,
            status_code=status,
            headers=allowed_headers,
            body=body,
        )
    except PublicHttpError:
        raise
    except TimeoutError:
        raise PublicHttpError("REQUEST_TIMEOUT", retryable=True) from None
    except (OSError, ssl.SSLError, http.client.HTTPException):
        raise PublicHttpError("NETWORK_FAILURE", retryable=True) from None
    finally:
        connection.close()


def _check_cancelled(cancellation: object) -> None:
    if cancellation is None:
        return
    is_set = getattr(cancellation, "is_set", None)
    if (callable(is_set) and is_set()) or getattr(cancellation, "cancelled", False):
        raise InterruptedError("public web request cancelled")


@dataclass(frozen=True, slots=True)
class _Target:
    scheme: str
    hostname: str
    port: int
    path_and_query: str
    canonical_url: str
    loopback: bool


def _parse_url(value: str, loopback_test_origins: frozenset[str]) -> _Target:
    if not isinstance(value, str) or not 8 <= len(value) <= 4096:
        raise PublicHttpError("INVALID_URL")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise PublicHttpError("INVALID_URL")
    try:
        parts = urlsplit(value)
        port = parts.port
        raw_host = parts.hostname
    except ValueError:
        raise PublicHttpError("INVALID_URL") from None
    if (
        parts.scheme not in {"http", "https"}
        or not raw_host
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise PublicHttpError("INVALID_URL")
    hostname = raw_host.encode("idna").decode("ascii").lower()
    effective_port = port or (443 if parts.scheme == "https" else 80)
    origin = f"{parts.scheme}://{_format_host(hostname)}"
    if port is not None:
        origin += f":{port}"
    loopback = origin in loopback_test_origins
    if parts.scheme == "http" and not loopback:
        raise PublicHttpError("INSECURE_URL")
    if not parts.path.startswith("/"):
        raise PublicHttpError("INVALID_URL")
    if "\\" in parts.path:
        raise PublicHttpError("INVALID_URL")
    try:
        query_pairs = parse_qsl(parts.query, keep_blank_values=True, max_num_fields=100)
    except ValueError:
        raise PublicHttpError("INVALID_URL") from None
    sensitive_query_names = {
        "access_token",
        "authorization",
        "api_key",
        "apikey",
        "cookie",
        "password",
        "secret",
        "session",
        "signature",
        "sig",
        "token",
    }
    if any(key.lower() in sensitive_query_names for key, _ in query_pairs):
        raise PublicHttpError("SENSITIVE_URL_REJECTED")
    path_and_query = parts.path or "/"
    if parts.query:
        path_and_query += "?" + parts.query
    canonical_url = urlunsplit(
        (parts.scheme, origin.split("://", 1)[1], parts.path or "/", parts.query, "")
    )
    return _Target(
        parts.scheme,
        hostname,
        effective_port,
        path_and_query,
        canonical_url,
        loopback,
    )


def _resolve_public_address(
    hostname: str,
    port: int,
    loopback: bool,
    *,
    timeout_seconds: float,
) -> str:
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None:
        _validate_ip(literal, loopback)
        return str(literal)
    if not _DNS_SLOTS.acquire(timeout=0.05):
        raise PublicHttpError("DNS_BUSY", retryable=True)
    future = _DNS_POOL.submit(socket.getaddrinfo, hostname, port, 0, socket.SOCK_STREAM)
    future.add_done_callback(lambda _future: _DNS_SLOTS.release())
    try:
        results = future.result(timeout=timeout_seconds)
    except FutureTimeout:
        future.cancel()
        raise PublicHttpError("DNS_TIMEOUT", retryable=True) from None
    except OSError:
        raise PublicHttpError("DNS_FAILURE", retryable=True) from None
    addresses: list[str] = []
    for result in results:
        try:
            address = ipaddress.ip_address(result[4][0])
        except (ValueError, IndexError):
            continue
        _validate_ip(address, loopback)
        text = str(address)
        if text not in addresses:
            addresses.append(text)
    if not addresses:
        raise PublicHttpError("DNS_FAILURE", retryable=True)
    # Do not re-resolve or fall back after connection failure; the socket below
    # uses this exact address while TLS still verifies the original hostname.
    return addresses[0]


def _validate_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address, loopback: bool) -> None:
    if loopback and address.is_loopback:
        return
    if not address.is_global:
        raise PublicHttpError("PRIVATE_ADDRESS_REJECTED")


def _host_header(hostname: str, port: int, scheme: str) -> str:
    host = _format_host(hostname)
    default_port = 443 if scheme == "https" else 80
    return host if port == default_port else f"{host}:{port}"


def _format_host(hostname: str) -> str:
    return f"[{hostname}]" if ":" in hostname else hostname


def _parse_content_type(value: str) -> tuple[str, str | None]:
    pieces = [part.strip() for part in value.split(";")]
    media_type = pieces[0].lower()
    charset = None
    for item in pieces[1:]:
        key, separator, raw_value = item.partition("=")
        if separator and key.strip().lower() == "charset":
            charset = raw_value.strip().strip('"')[:64]
    if not media_type or len(media_type) > 128:
        raise PublicHttpError("INVALID_RESPONSE")
    return media_type, charset


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, address: str, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._pinned_address = address

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._pinned_address, self.port), timeout=self.timeout
        )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, address: str, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._pinned_address = address

    def connect(self) -> None:
        raw = socket.create_connection((self._pinned_address, self.port), timeout=self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except Exception:
            raw.close()
            raise
