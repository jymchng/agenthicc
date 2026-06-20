"""Tests for HTTP timeout safety across tool layer (PRD-108)."""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agenthicc.tools.http import agenthicc_http_client, configure, is_network_error


# ── tools/http.py — shared factory ───────────────────────────────────────────

@pytest.mark.unit
def test_configure_sets_default_timeout() -> None:
    configure(45.0)
    from agenthicc.tools import http as _http
    assert _http._default_timeout == 45.0
    configure(30.0)  # restore


@pytest.mark.unit
def test_configure_clamps_negative_to_zero() -> None:
    configure(-5.0)
    from agenthicc.tools import http as _http
    assert _http._default_timeout == 0.0
    configure(30.0)  # restore


@pytest.mark.unit
def test_is_network_error_stdlib_timeout() -> None:
    assert is_network_error(TimeoutError("timed out"))


@pytest.mark.unit
def test_is_network_error_connection_error() -> None:
    assert is_network_error(ConnectionError("refused"))


@pytest.mark.unit
def test_is_network_error_oserror() -> None:
    assert is_network_error(OSError("pipe broken"))


@pytest.mark.unit
def test_is_network_error_httpx_read_timeout() -> None:
    import httpx
    exc = httpx.ReadTimeout("read timed out", request=None)
    assert is_network_error(exc)


@pytest.mark.unit
def test_is_network_error_httpx_connect_timeout() -> None:
    import httpx
    exc = httpx.ConnectTimeout("connect timed out", request=None)
    assert is_network_error(exc)


@pytest.mark.unit
def test_is_network_error_httpx_connect_error() -> None:
    import httpx
    exc = httpx.ConnectError("connection refused", request=None)
    assert is_network_error(exc)


@pytest.mark.unit
def test_is_network_error_value_error_is_false() -> None:
    assert not is_network_error(ValueError("bad input"))


@pytest.mark.unit
def test_is_network_error_runtime_error_is_false() -> None:
    assert not is_network_error(RuntimeError("logic error"))


@pytest.mark.unit
def test_is_network_error_boto_read_timeout() -> None:
    class ReadTimeoutError(Exception):
        pass
    assert is_network_error(ReadTimeoutError("S3 timed out"))


@pytest.mark.unit
async def test_agenthicc_http_client_yields_client() -> None:
    async with agenthicc_http_client() as client:
        import httpx
        assert isinstance(client, httpx.AsyncClient)


@pytest.mark.unit
async def test_agenthicc_http_client_respects_timeout_override() -> None:
    import httpx
    async with agenthicc_http_client(timeout=42.0) as client:
        # httpx.Timeout stores read timeout as a float
        assert client.timeout.read == 42.0


@pytest.mark.unit
async def test_agenthicc_http_client_zero_timeout_means_none() -> None:
    import httpx
    configure(0.0)
    async with agenthicc_http_client() as client:
        # 0.0 → None (unbounded)
        assert client.timeout.read is None
    configure(30.0)  # restore


# ── SearchWebTool — network error boundary ────────────────────────────────────

@pytest.mark.unit
async def test_search_web_tool_returns_error_dict_on_read_timeout() -> None:
    """ReadTimeout from Brave API must NOT propagate — returns {"ok": False}."""
    import httpx
    from agenthicc.skills.web_search import SearchWebTool

    tool = SearchWebTool(api_key="test-key", engine="brave")

    with patch("agenthicc.tools.http.agenthicc_http_client") as mock_cm:
        mock_client = AsyncMock()
        mock_client.get.side_effect = httpx.ReadTimeout(
            "Read timed out", request=MagicMock()
        )
        mock_cm.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_cm.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await tool.execute({"query": "test query"}, {})

    assert result["ok"] is False
    assert "ReadTimeout" in result["error"]
    assert result.get("recoverable") is True


@pytest.mark.unit
async def test_search_web_tool_returns_error_dict_on_connect_timeout() -> None:
    import httpx
    from agenthicc.skills.web_search import SearchWebTool

    tool = SearchWebTool(api_key="key", engine="brave")

    with patch("agenthicc.tools.http.agenthicc_http_client") as mock_cm:
        mock_client = AsyncMock()
        mock_client.get.side_effect = httpx.ConnectTimeout(
            "Connect timed out", request=MagicMock()
        )
        mock_cm.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_cm.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await tool.execute({"query": "test"}, {})

    assert result["ok"] is False
    assert "ConnectTimeout" in result["error"]


@pytest.mark.unit
async def test_search_web_tool_reraises_non_network_errors() -> None:
    """Logic errors (e.g. KeyError) must still propagate."""
    from agenthicc.skills.web_search import SearchWebTool

    tool = SearchWebTool(api_key="key", engine="brave")

    with patch("agenthicc.tools.http.agenthicc_http_client") as mock_cm:
        mock_client = AsyncMock()
        mock_client.get.side_effect = KeyError("unexpected")
        mock_cm.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_cm.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(KeyError):
            await tool.execute({"query": "test"}, {})


@pytest.mark.unit
async def test_fetch_page_tool_error_includes_class_name() -> None:
    """FetchPageTool already catches errors — verify class name is in error."""
    import httpx
    from agenthicc.skills.web_search import FetchPageTool

    tool = FetchPageTool()

    with patch("agenthicc.tools.http.agenthicc_http_client") as mock_cm:
        mock_client = AsyncMock()
        mock_client.get.side_effect = httpx.ReadTimeout(
            "read timed out", request=MagicMock()
        )
        mock_cm.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_cm.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await tool.execute({"url": "https://example.com"}, {})

    assert result["ok"] is False
    assert "ReadTimeout" in result["error"]   # class name present
    assert result.get("recoverable") is True


# ── Outlook _OutlookNetworkError boundary ─────────────────────────────────────

@pytest.mark.unit
async def test_outlook_get_raises_outlook_network_error_on_read_timeout() -> None:
    import httpx
    from agenthicc.tools.outlook import GraphApiOutlookBackend, _OutlookNetworkError

    backend = GraphApiOutlookBackend(token="token")

    with patch("agenthicc.tools.http.agenthicc_http_client") as mock_cm:
        mock_client = AsyncMock()
        mock_client.get.side_effect = httpx.ReadTimeout(
            "read timed out", request=MagicMock()
        )
        mock_cm.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_cm.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(_OutlookNetworkError) as exc_info:
            await backend._get("/me/messages")

    assert "ReadTimeout" in str(exc_info.value)


@pytest.mark.unit
async def test_outlook_post_raises_outlook_network_error_on_connect_timeout() -> None:
    import httpx
    from agenthicc.tools.outlook import GraphApiOutlookBackend, _OutlookNetworkError

    backend = GraphApiOutlookBackend(token="token")

    with patch("agenthicc.tools.http.agenthicc_http_client") as mock_cm:
        mock_client = AsyncMock()
        mock_client.post.side_effect = httpx.ConnectTimeout(
            "connect timed out", request=MagicMock()
        )
        mock_cm.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_cm.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(_OutlookNetworkError):
            await backend._post("/me/sendMail", {})


@pytest.mark.unit
async def test_outlook_safe_call_converts_to_error_dict() -> None:
    """_safe_call in agent_tools.py converts _OutlookNetworkError to dict."""
    from agenthicc.tools.outlook import _OutlookNetworkError
    from agenthicc.tools.outlook.agent_tools import _safe_call

    async def failing_coro():
        raise _OutlookNetworkError("ReadTimeout: graph.microsoft.com timed out")

    result = await _safe_call(failing_coro())
    assert result["ok"] is False
    assert "ReadTimeout" in result["error"]
    assert result["recoverable"] is True


@pytest.mark.unit
async def test_outlook_safe_call_passes_through_on_success() -> None:
    from agenthicc.tools.outlook.agent_tools import _safe_call

    async def ok_coro():
        return {"id": "123", "subject": "Hello"}

    result = await _safe_call(ok_coro())
    assert result == {"id": "123", "subject": "Hello"}


# ── auth.py — AuthNetworkError ────────────────────────────────────────────────

@pytest.mark.unit
async def test_auth_exchange_code_raises_auth_network_error_on_timeout() -> None:
    import httpx
    from agenthicc.auth import AuthClient, AuthNetworkError

    client = AuthClient.__new__(AuthClient)  # skip __init__

    with patch("agenthicc.tools.http.agenthicc_http_client") as mock_cm:
        mock_http = AsyncMock()
        mock_http.post.side_effect = httpx.ReadTimeout(
            "read timed out", request=MagicMock()
        )
        mock_cm.return_value.__aenter__ = AsyncMock(return_value=mock_http)
        mock_cm.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(AuthNetworkError) as exc_info:
            await client._exchange_code("code", "verifier", "http://localhost/cb")

    assert "timed out" in str(exc_info.value).lower()
    assert "ReadTimeout" in str(exc_info.value)


@pytest.mark.unit
async def test_auth_refresh_raises_auth_network_error_on_timeout() -> None:
    import httpx
    from agenthicc.auth import AuthClient, AuthNetworkError, TokenBundle

    client = AuthClient.__new__(AuthClient)
    bundle = TokenBundle(
        access_token="old", refresh_token="rt",
        expires_at=0.0, plan="free", email="u@e.com", user_id="u1",
    )

    with patch("agenthicc.tools.http.agenthicc_http_client") as mock_cm:
        mock_http = AsyncMock()
        mock_http.post.side_effect = httpx.ConnectTimeout(
            "connect timed out", request=MagicMock()
        )
        mock_cm.return_value.__aenter__ = AsyncMock(return_value=mock_http)
        mock_cm.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(AuthNetworkError) as exc_info:
            await client._refresh(bundle)

    assert "ConnectTimeout" in str(exc_info.value)


@pytest.mark.unit
async def test_auth_network_error_is_not_bare_read_timeout() -> None:
    """AuthNetworkError message is human-readable, not a raw exception repr."""
    import httpx
    from agenthicc.auth import AuthClient, AuthNetworkError

    client = AuthClient.__new__(AuthClient)

    with patch("agenthicc.tools.http.agenthicc_http_client") as mock_cm:
        mock_http = AsyncMock()
        mock_http.post.side_effect = httpx.ReadTimeout("raw", request=MagicMock())
        mock_cm.return_value.__aenter__ = AsyncMock(return_value=mock_http)
        mock_cm.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(AuthNetworkError) as exc_info:
            await client._exchange_code("c", "v", "r")

    msg = str(exc_info.value)
    # Must mention the exception type AND give actionable advice
    assert "ReadTimeout" in msg
    assert "connection" in msg.lower() or "timed out" in msg.lower()


# ── config — http_timeout_s field ────────────────────────────────────────────

@pytest.mark.unit
def test_tool_settings_has_http_timeout_s() -> None:
    from agenthicc.config import ToolSettings
    settings = ToolSettings()
    assert hasattr(settings, "http_timeout_s")
    assert settings.http_timeout_s == 30.0


@pytest.mark.unit
def test_tool_settings_http_timeout_s_configurable() -> None:
    from agenthicc.config import ToolSettings
    settings = ToolSettings(http_timeout_s=60.0)
    assert settings.http_timeout_s == 60.0
