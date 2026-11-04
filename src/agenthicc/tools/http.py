"""Shared HTTP client factory for all agenthicc tool HTTP calls (PRD-108).

Usage
-----
All tools that make outbound HTTP requests must use ``agenthicc_http_client()``
instead of constructing ``httpx.AsyncClient`` directly.  This ensures:

* A consistent read timeout sourced from ``[tools] http_timeout_s`` in config.
* A bounded connect timeout (always 10 s) independent of the read timeout.
* Network errors can be detected uniformly via ``is_network_error()``.

Example
-------
::

    from agenthicc.tools.http import agenthicc_http_client, is_network_error

    async def execute(self, args, context):
        try:
            async with agenthicc_http_client() as client:
                r = await client.get("https://example.com/api")
                r.raise_for_status()
                return {"ok": True, "data": r.json()}
        except Exception as exc:
            if is_network_error(exc):
                return {"ok": False, "error_code": "network",
                        "error": f"{type(exc).__name__}: {exc}", "retryable": True}
            raise
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, AsyncGenerator
from urllib.parse import urlsplit, urlunsplit

if TYPE_CHECKING:
    import httpx

__all__ = [
    "HttpDestinationPolicy",
    "HttpPolicyError",
    "agenthicc_http_client",
    "configure",
    "configure_network_policy",
    "is_network_error",
    "network_policy",
]

# Module-level defaults — set once at session startup via configure().
_default_timeout: float = 30.0  # read / total timeout
_connect_timeout: float = 10.0  # always bounded tightly

# Exception class names that count as network errors when httpx is unavailable.
_NETWORK_CLASS_NAMES: frozenset[str] = frozenset(
    {
        "ReadTimeout",
        "ConnectTimeout",
        "WriteTimeout",
        "PoolTimeout",
        "TimeoutException",
        "ReadError",
        "ConnectError",
        "RemoteProtocolError",
        "ReadTimeoutError",
        "ConnectTimeoutError",  # botocore
    }
)


def configure(timeout_s: float) -> None:
    """Set the module-level default read timeout.

    Called once at session startup from ``_build_session_context()`` after
    ``load_config()`` resolves ``ToolSettings.http_timeout_s``.

    Parameters
    ----------
    timeout_s:
        Read timeout in seconds.  ``0.0`` or negative → no read timeout
        (the httpx ``None`` sentinel).
    """
    global _default_timeout
    _default_timeout = max(0.0, timeout_s)


class HttpPolicyError(Exception):
    """A destination the session's network policy refuses."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class HttpDestinationPolicy:
    """Where a model-chosen HTTP request may go (TOOLR-05).

    Only ``http``/``https``; the host must be on *allowed_domains* (a host or
    any of its subdomains), unless *allow_all*; every address the host
    resolves to must be public — loopback, private, link-local and reserved
    ranges (cloud metadata included) are refused. The check runs for the
    first request and again for every redirect hop.
    """

    allowed_domains: tuple[str, ...] = ()
    allow_all: bool = False
    resolver: Callable[[str], Awaitable[Iterable[str]]] | None = field(
        default=None, repr=False, compare=False
    )

    async def check(self, url: str) -> None:
        """Raise :class:`HttpPolicyError` unless *url* may be requested."""
        from agenthicc.tools.cloakbrowser.errors import BrowserErrorKind, BrowserToolError
        from agenthicc.tools.cloakbrowser.policy import BrowserPolicy

        if not self.allow_all and not self.allowed_domains:
            raise HttpPolicyError(
                "outside_allow_list",
                "No network destinations are allowed: add the host to [security] "
                "network_allow_list, or set network_allow_all = true.",
            )
        try:
            parts = urlsplit(url)
        except ValueError as exc:
            raise HttpPolicyError("invalid_argument", f"malformed URL: {url!r}") from exc
        without_fragment = urlunsplit(parts._replace(fragment=""))
        try:
            policy = BrowserPolicy(
                self.allowed_domains,
                allowed_ports=frozenset(range(1, 65_536))
                if self.allow_all
                else frozenset({80, 443}),
                allow_loopback=False,
                allow_private_addresses=False,
                allow_all_domains=self.allow_all,
                resolver=self.resolver,
            )
            await policy.validate_url(without_fragment)
        except BrowserToolError as exc:
            if exc.kind is BrowserErrorKind.NETWORK:
                raise HttpPolicyError("network", f"{parts.hostname}: {exc}") from exc
            if exc.kind is BrowserErrorKind.INVALID_INPUT:
                raise HttpPolicyError("invalid_argument", str(exc)) from exc
            raise HttpPolicyError(
                "outside_allow_list",
                f"{parts.hostname or url!r}: {exc} Add it to [security] network_allow_list "
                "to allow it.",
            ) from exc
        except ValueError as exc:
            raise HttpPolicyError("invalid_argument", str(exc)) from exc


_network_policy = HttpDestinationPolicy()


def configure_network_policy(policy: HttpDestinationPolicy) -> None:
    """Install the session's destination policy (once, at startup)."""
    global _network_policy
    _network_policy = policy


def network_policy() -> HttpDestinationPolicy:
    """The session's policy for model-chosen HTTP destinations."""
    return _network_policy


#: Redirect hops a policy-bound client follows, each one re-checked.
MAX_REDIRECTS = 5


@asynccontextmanager
async def agenthicc_http_client(
    *,
    timeout: float | None = None,
    follow_redirects: bool = True,
    policy: HttpDestinationPolicy | None = None,
) -> AsyncGenerator[httpx.AsyncClient, None]:
    """Async context manager yielding a configured ``httpx.AsyncClient``.

    Parameters
    ----------
    timeout:
        Per-call read timeout override in seconds.  Defaults to the
        module-level ``_default_timeout`` (set by ``configure()``).
    follow_redirects:
        Whether to follow HTTP redirects.  Default ``True``.
    """
    import httpx

    read_t = timeout if timeout is not None else _default_timeout
    # 0.0 → None (httpx unbounded); positive → that many seconds.
    httpx_timeout = httpx.Timeout(
        read_t if read_t > 0 else None,
        connect=_connect_timeout,
    )
    hooks: dict[str, list[Callable[[httpx.Request], Awaitable[None]]]] = {}
    if policy is not None:
        bound = policy

        async def _check_request(request: httpx.Request) -> None:
            # Runs for the first request and again for every redirect hop.
            await bound.check(str(request.url))

        hooks["request"] = [_check_request]
    async with httpx.AsyncClient(
        timeout=httpx_timeout,
        follow_redirects=follow_redirects,
        max_redirects=MAX_REDIRECTS,
        event_hooks=hooks,
    ) as client:
        yield client


def is_network_error(exc: BaseException) -> bool:
    """Return ``True`` when *exc* is a transient network / timeout error.

    Covers:
    * ``httpx.TimeoutException`` (ReadTimeout, ConnectTimeout, PoolTimeout, …)
    * ``httpx.HTTPError`` (connection failures, protocol errors)
    * stdlib ``TimeoutError``, ``ConnectionError``
    * botocore ``ReadTimeoutError``, ``ConnectTimeoutError``
    """
    # httpx hierarchy (lazy import so this module is importable without httpx)
    try:
        import httpx as _httpx

        if isinstance(exc, (_httpx.TimeoutException, _httpx.HTTPError)):
            return True
    except ImportError:
        pass

    # stdlib
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True

    # Catch-all by class name (boto3 / other clients)
    return type(exc).__name__ in _NETWORK_CLASS_NAMES
