"""Browser navigation and input policy.

The policy is intentionally independent from the optional CloakBrowser
package.  It can therefore be tested and enforced before an optional import
or browser process is started.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from urllib.parse import SplitResult, urlsplit, urlunsplit

from agenthicc.tools.sandbox import NetworkGuard

from .errors import BrowserErrorKind, BrowserToolError

__all__ = ["BrowserPolicy", "redact_link", "redact_url"]

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_SENSITIVE_FIELD = re.compile(
    r"(?:password|passwd|passcode|secret|token|api[_-]?key|authorization|cookie|"
    r"credit[_-]?card|card[_-]?number|cvv|ssn)",
    re.IGNORECASE,
)
_SAFE_KEYS = frozenset(
    {
        "enter",
        "tab",
        "escape",
        "space",
        "backspace",
        "delete",
        "arrowleft",
        "arrowright",
        "arrowup",
        "arrowdown",
        "home",
        "end",
        "pageup",
        "pagedown",
    }
)


@dataclass(frozen=True, slots=True)
class _AllowedOrigin:
    """One normalized bare-domain or CORS-style origin rule."""

    host: str
    scheme: str | None = None
    port: int | None = None
    subdomains: bool = True
    include_root: bool = True
    exact_origin: bool = False

    def matches(
        self, parsed: SplitResult, effective_port: int, allowed_ports: frozenset[int]
    ) -> bool:
        host = (parsed.hostname or "").rstrip(".").lower()
        if self.scheme is not None and parsed.scheme.lower() != self.scheme:
            return False
        if self.exact_origin:
            if self.port != effective_port:
                return False
        elif effective_port not in allowed_ports:
            return False
        if host == self.host:
            return self.include_root
        return self.subdomains and host.endswith(f".{self.host}")


def _parse_allowed_origin(value: str) -> _AllowedOrigin:
    """Parse a bare host or an HTTP(S) origin without accepting URL paths."""
    raw = value.strip().lower()
    if not raw or raw == "*" or raw == "null":
        raise ValueError("CloakBrowser allow-list entries must name an origin or host")

    if "://" in raw:
        try:
            parsed = urlsplit(raw)
            port = parsed.port
        except ValueError as exc:
            raise ValueError("CloakBrowser origin is malformed") from exc
        host = (parsed.hostname or "").rstrip(".")
        if (
            parsed.scheme not in {"http", "https"}
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "CloakBrowser origins must be HTTP(S) host origins without credentials or paths"
            )
        wildcard = host.startswith("*.")
        host = host[2:] if wildcard else host
        if not host or "*" in host:
            raise ValueError("CloakBrowser origin wildcard must be a leading '*.domain' prefix")
        return _AllowedOrigin(
            host=host,
            scheme=parsed.scheme,
            port=port if port is not None else (443 if parsed.scheme == "https" else 80),
            subdomains=wildcard,
            include_root=not wildcard,
            exact_origin=True,
        )

    if any(character in raw for character in "/?#@"):
        raise ValueError("CloakBrowser host entries must not contain paths or URL components")
    wildcard = raw.startswith("*.")
    host = raw[2:] if wildcard else raw.lstrip(".")
    if not host or "*" in host or any(character.isspace() for character in host):
        raise ValueError("CloakBrowser host entry is malformed")
    return _AllowedOrigin(host=host, subdomains=True, include_root=not wildcard)


def redact_url(value: str) -> str:
    """Return a URL without credentials, query parameters, or fragments."""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "<invalid-url>"
    if not parsed.scheme or not parsed.netloc:
        return "<invalid-url>"
    host = parsed.hostname or ""
    try:
        port = parsed.port
    except ValueError:
        port = None
    netloc = host if port is None else f"{host}:{port}"
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", "", ""))


def redact_link(value: str) -> str:
    """Redact an absolute or relative link without exposing query secrets."""
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "<invalid-link>"
    if parsed.scheme and parsed.netloc:
        return redact_url(value)
    return urlunsplit(("", "", parsed.path, "", ""))


@dataclass(frozen=True)
class BrowserPolicy:
    """Fail-closed policy for browser destinations and bounded inputs."""

    allowed_domains: tuple[str, ...] = ()
    allowed_ports: frozenset[int] = frozenset({80, 443})
    allow_loopback: bool = False
    allow_private_addresses: bool = False
    max_url_chars: int = 4096
    max_selector_chars: int = 512
    max_value_chars: int = 4000
    max_wait_value_chars: int = 512
    resolver: Callable[[str], Awaitable[Iterable[str]]] | None = field(default=None, repr=False)
    allow_all_domains: bool = field(default=False, kw_only=True)
    _allowed_origins: tuple[_AllowedOrigin, ...] = field(init=False, repr=False, compare=False)
    _network_guard: NetworkGuard = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        normalized = tuple(
            sorted({item.strip().lower() for item in self.allowed_domains if item.strip()})
        )
        object.__setattr__(self, "allowed_domains", normalized)
        if not self.allowed_ports or any(
            port not in range(1, 65536) for port in self.allowed_ports
        ):
            raise ValueError("CloakBrowser allowed_ports contains an invalid port")
        try:
            allowed_origins = tuple(_parse_allowed_origin(item) for item in normalized)
        except ValueError:
            raise
        object.__setattr__(self, "_allowed_origins", allowed_origins)
        object.__setattr__(
            self,
            "_network_guard",
            NetworkGuard([origin.host for origin in allowed_origins]),
        )

    def _parse_url(self, url: str) -> SplitResult:
        if not isinstance(url, str) or not url.strip() or len(url) > self.max_url_chars:
            raise BrowserToolError(BrowserErrorKind.INVALID_INPUT, "URL is empty or too long.")
        if _CONTROL_CHARS.search(url):
            raise BrowserToolError(
                BrowserErrorKind.INVALID_INPUT, "URL contains control characters."
            )
        try:
            parsed = urlsplit(url.strip())
            # Accessing ``port`` validates malformed numeric ports.
            parsed.port
        except ValueError as exc:
            raise BrowserToolError(BrowserErrorKind.INVALID_INPUT, "URL is malformed.") from exc
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            raise BrowserToolError(
                BrowserErrorKind.POLICY_DENIED,
                "Only approved http(s) destinations are allowed.",
            )
        if parsed.username is not None or parsed.password is not None:
            raise BrowserToolError(
                BrowserErrorKind.POLICY_DENIED, "URL credentials are not allowed."
            )
        if parsed.fragment:
            raise BrowserToolError(BrowserErrorKind.POLICY_DENIED, "URL fragments are not allowed.")
        return parsed

    def _check_origin(self, parsed: SplitResult) -> None:
        host = (parsed.hostname or "").rstrip(".").lower()
        effective_port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        if self.allow_all_domains:
            if effective_port not in self.allowed_ports:
                raise BrowserToolError(BrowserErrorKind.POLICY_DENIED, "URL port is not allowed.")
        elif not any(
            origin.matches(parsed, effective_port, self.allowed_ports)
            for origin in self._allowed_origins
        ):
            raise BrowserToolError(
                BrowserErrorKind.POLICY_DENIED, "Destination is not allow-listed."
            )
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return
        if address.is_loopback and not self.allow_loopback:
            raise BrowserToolError(
                BrowserErrorKind.POLICY_DENIED, "Loopback destinations are blocked."
            )
        if (
            address.is_private or address.is_link_local or address.is_reserved
        ) and not self.allow_private_addresses:
            raise BrowserToolError(
                BrowserErrorKind.POLICY_DENIED, "Private destinations are blocked."
            )

    async def _resolve(self, host: str) -> Iterable[str]:
        if self.resolver is not None:
            return await self.resolver(host)
        results = await asyncio.to_thread(socket.getaddrinfo, host, None, type=socket.SOCK_STREAM)
        return {str(item[4][0]) for item in results if item[4]}

    async def validate_url(self, url: str) -> str:
        """Validate a destination and return the normalized URL.

        DNS results are checked on every navigation/request.  This keeps the
        adapter fail-closed if an allow-listed hostname is later rebound to a
        private address.
        """
        parsed = self._parse_url(url)
        host = (parsed.hostname or "").rstrip(".").lower()
        self._check_origin(parsed)
        try:
            addresses = await self._resolve(host)
        except OSError as exc:
            raise BrowserToolError(
                BrowserErrorKind.NETWORK, "Destination could not be resolved."
            ) from exc
        if not addresses:
            raise BrowserToolError(BrowserErrorKind.NETWORK, "Destination could not be resolved.")
        for raw_address in addresses:
            try:
                address = ipaddress.ip_address(raw_address)
            except ValueError as exc:
                raise BrowserToolError(
                    BrowserErrorKind.NETWORK, "Destination resolution was invalid."
                ) from exc
            if address.is_loopback and not self.allow_loopback:
                raise BrowserToolError(
                    BrowserErrorKind.POLICY_DENIED, "Loopback destinations are blocked."
                )
            if (
                address.is_private or address.is_link_local or address.is_reserved
            ) and not self.allow_private_addresses:
                raise BrowserToolError(
                    BrowserErrorKind.POLICY_DENIED, "Private destinations are blocked."
                )
        # Keep the existing network policy useful to operators who configure
        # the same domains globally.  It is redundant with the explicit check,
        # but gives this adapter the same boundary as other network tools.
        if not self.allow_all_domains:
            try:
                self._network_guard.check(url)
            except PermissionError as exc:
                raise BrowserToolError(
                    BrowserErrorKind.POLICY_DENIED, "Destination is not allow-listed."
                ) from exc
        return urlunsplit(
            (parsed.scheme.lower(), parsed.netloc, parsed.path or "/", parsed.query, "")
        )

    def validate_selector(self, selector: str) -> str:
        if (
            not isinstance(selector, str)
            or not selector.strip()
            or len(selector) > self.max_selector_chars
        ):
            raise BrowserToolError(BrowserErrorKind.INVALID_INPUT, "Selector is empty or too long.")
        if _CONTROL_CHARS.search(selector):
            raise BrowserToolError(
                BrowserErrorKind.INVALID_INPUT, "Selector contains control characters."
            )
        return selector.strip()

    def validate_value(self, selector: str, value: str) -> tuple[str, str]:
        safe_selector = self.validate_selector(selector)
        if not isinstance(value, str) or len(value) > self.max_value_chars:
            raise BrowserToolError(
                BrowserErrorKind.INVALID_INPUT, "Form value is empty or too long."
            )
        if self.is_sensitive_target(safe_selector):
            raise BrowserToolError(
                BrowserErrorKind.POLICY_DENIED,
                "Sensitive form fields cannot be filled by the browser agent.",
            )
        return safe_selector, value

    def validate_key(self, key: str) -> str:
        if not isinstance(key, str) or len(key) > 32 or not key.strip():
            raise BrowserToolError(BrowserErrorKind.INVALID_INPUT, "Keyboard key is invalid.")
        normalized = key.strip()
        if normalized.lower() not in _SAFE_KEYS and not (
            len(normalized) == 1 and normalized.isprintable()
        ):
            raise BrowserToolError(
                BrowserErrorKind.POLICY_DENIED, "Keyboard key is not allow-listed."
            )
        return normalized

    def validate_wait(self, condition: str, value: str) -> tuple[str, str]:
        if condition not in {"selector", "text", "url", "load_state"}:
            raise BrowserToolError(BrowserErrorKind.INVALID_INPUT, "Wait condition is invalid.")
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value) > self.max_wait_value_chars
        ):
            raise BrowserToolError(
                BrowserErrorKind.INVALID_INPUT, "Wait value is empty or too long."
            )
        if condition == "selector":
            return condition, self.validate_selector(value)
        if condition == "url":
            return condition, value.strip()
        return condition, value.strip()

    @staticmethod
    def is_sensitive_target(selector: str) -> bool:
        return bool(_SENSITIVE_FIELD.search(selector))
