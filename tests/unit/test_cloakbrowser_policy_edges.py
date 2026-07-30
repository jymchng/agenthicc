"""Boundary and malformed-input coverage for browser navigation policy."""

from __future__ import annotations

import asyncio

import pytest

from agenthicc.tools.cloakbrowser import BrowserErrorKind, BrowserPolicy, BrowserToolError
from agenthicc.tools.cloakbrowser.policy import redact_link, redact_url

pytestmark = pytest.mark.unit


def test_policy_redaction_and_constructor_validation_edges() -> None:
    assert (
        redact_url("https://Example.com:443/a?token=secret#fragment") == "https://example.com:443/a"
    )
    assert redact_url("relative/path") == "<invalid-url>"
    assert redact_url("https://[bad") == "<invalid-url>"
    assert redact_url("https://example.com:bad/path") == "https://example.com/path"
    assert redact_link("") == ""
    assert redact_link("/next?token=secret#section") == "/next"
    assert redact_link("https://example.com/next?token=secret") == "https://example.com/next"
    assert redact_link("http://[bad") == "<invalid-link>"

    with pytest.raises(ValueError):
        BrowserPolicy(("https://example.com:bad",))
    with pytest.raises(ValueError):
        BrowserPolicy(("https://*.example.*",))
    with pytest.raises(ValueError):
        BrowserPolicy(("example.com",), allowed_ports=frozenset({0}))


def test_policy_validators_reject_bounds_and_unsafe_keyboard_input() -> None:
    policy = BrowserPolicy(("example.com",), max_url_chars=20, max_selector_chars=10)
    for value in ("", "x" * 21, "https://example.com/\n"):
        with pytest.raises(BrowserToolError) as error:
            policy._parse_url(value)
        assert error.value.kind is BrowserErrorKind.INVALID_INPUT
    with pytest.raises(BrowserToolError):
        policy._parse_url("ftp://example.com/")
    with pytest.raises(BrowserToolError):
        policy._parse_url("https://user:pass@example.com/")
    with pytest.raises(BrowserToolError):
        policy._parse_url("https://example.com/#fragment")
    with pytest.raises(BrowserToolError):
        policy.validate_selector(" ")
    with pytest.raises(BrowserToolError):
        policy.validate_selector("button\x00")
    with pytest.raises(BrowserToolError):
        policy.validate_value("input", "x" * 4001)
    assert policy.validate_key(" Enter ") == "Enter"
    assert policy.validate_key("x") == "x"
    for value in ("", "x" * 33, "F13"):
        with pytest.raises(BrowserToolError):
            policy.validate_key(value)
    with pytest.raises(BrowserToolError):
        policy.validate_wait("bad", "value")
    with pytest.raises(BrowserToolError):
        policy.validate_wait("text", " ")
    assert policy.validate_wait("selector", " body ") == ("selector", "body")
    assert policy.validate_wait("url", " https://example.com ") == ("url", "https://example.com")
    assert BrowserPolicy.is_sensitive_target("[name=credit-card]") is True


async def test_policy_resolution_and_network_safety_error_paths() -> None:
    async def public(_host: str) -> list[str]:
        return ["93.184.216.34"]

    strict_port = BrowserPolicy(("example.com",), allowed_ports=frozenset({443}), resolver=public)
    with pytest.raises(BrowserToolError, match="allow-listed"):
        await strict_port.validate_url("https://example.com:8443/")
    all_domains_port = BrowserPolicy(
        allowed_ports=frozenset({443}), allow_all_domains=True, resolver=public
    )
    with pytest.raises(BrowserToolError, match="port"):
        await all_domains_port.validate_url("http://example.com/")
    assert (
        await strict_port.validate_url("https://example.com/path?q=1")
        == "https://example.com/path?q=1"
    )

    async def unavailable(_host: str) -> list[str]:
        raise OSError("dns down")

    with pytest.raises(BrowserToolError) as dns_error:
        await BrowserPolicy(("example.com",), resolver=unavailable).validate_url(
            "https://example.com/"
        )
    assert dns_error.value.kind is BrowserErrorKind.NETWORK

    async def empty(_host: str) -> list[str]:
        return []

    with pytest.raises(BrowserToolError):
        await BrowserPolicy(("example.com",), resolver=empty).validate_url("https://example.com/")

    async def malformed(_host: str) -> list[str]:
        return ["not-an-ip"]

    with pytest.raises(BrowserToolError):
        await BrowserPolicy(("example.com",), resolver=malformed).validate_url(
            "https://example.com/"
        )

    async def private(_host: str) -> list[str]:
        return ["192.168.1.4"]

    with pytest.raises(BrowserToolError, match="Private"):
        await BrowserPolicy(("example.com",), resolver=private).validate_url("https://example.com/")
    permissive = BrowserPolicy(("example.com",), resolver=private, allow_private_addresses=True)
    assert await permissive.validate_url("https://example.com/") == "https://example.com/"

    async def loopback(_host: str) -> list[str]:
        return ["127.0.0.1"]

    with pytest.raises(BrowserToolError, match="Loopback"):
        await BrowserPolicy(("localhost",), resolver=loopback).validate_url("http://localhost/")
    assert (
        await BrowserPolicy(
            ("localhost",), resolver=loopback, allow_loopback=True, allow_private_addresses=True
        ).validate_url("http://localhost/")
        == "http://localhost/"
    )


def test_policy_sync_network_guard_denial_is_translated(monkeypatch: pytest.MonkeyPatch) -> None:
    async def public(_host: str) -> list[str]:
        return ["93.184.216.34"]

    policy = BrowserPolicy(("example.com",), resolver=public)
    monkeypatch.setattr(
        policy._network_guard, "check", lambda _url: (_ for _ in ()).throw(PermissionError())
    )

    async def check() -> None:
        with pytest.raises(BrowserToolError, match="allow-listed"):
            await policy.validate_url("https://example.com/")

    asyncio.run(check())
