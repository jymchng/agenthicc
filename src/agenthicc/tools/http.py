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
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                        "recoverable": True}
            raise
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncGenerator

__all__ = [
    "agenthicc_http_client",
    "configure",
    "is_network_error",
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


@asynccontextmanager
async def agenthicc_http_client(
    *,
    timeout: float | None = None,
    follow_redirects: bool = True,
) -> AsyncGenerator["httpx.AsyncClient", None]:  # type: ignore[name-defined]
    """Async context manager yielding a configured ``httpx.AsyncClient``.

    Parameters
    ----------
    timeout:
        Per-call read timeout override in seconds.  Defaults to the
        module-level ``_default_timeout`` (set by ``configure()``).
    follow_redirects:
        Whether to follow HTTP redirects.  Default ``True``.
    """
    import httpx  # noqa: PLC0415

    read_t = timeout if timeout is not None else _default_timeout
    # 0.0 → None (httpx unbounded); positive → that many seconds.
    httpx_timeout = httpx.Timeout(
        read_t if read_t > 0 else None,
        connect=_connect_timeout,
    )
    async with httpx.AsyncClient(
        timeout=httpx_timeout,
        follow_redirects=follow_redirects,
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
        import httpx as _httpx  # noqa: PLC0415

        if isinstance(exc, (_httpx.TimeoutException, _httpx.HTTPError)):
            return True
    except ImportError:
        pass

    # stdlib
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True

    # Catch-all by class name (boto3 / other clients)
    return type(exc).__name__ in _NETWORK_CLASS_NAMES
