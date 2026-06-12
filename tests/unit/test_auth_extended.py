"""Extended unit tests for AuthClient and TokenStore covering previously
uncovered lines (PRD-11).

Targeted lines:
  auth.py: 80-81, 121-168, 183-186, 189, 196-207, 217-226, 236-244, 248-251
"""
from __future__ import annotations

import json
import socket
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agenthicc.auth import (
    AGENTHICC_REVOKE_URL,
    AGENTHICC_TOKEN_URL,
    AuthClient,
    TokenBundle,
    TokenStore,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bundle(
    access_token: str = "tok_access",
    refresh_token: str = "tok_refresh",
    expires_offset: float = 3600,
    plan: str = "free",
    email: str = "user@example.com",
    user_id: str = "u1",
) -> TokenBundle:
    return TokenBundle(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=time.time() + expires_offset,
        plan=plan,
        email=email,
        user_id=user_id,
    )


def _mock_httpx_client(json_data: dict) -> MagicMock:
    """Build a mock httpx.AsyncClient that returns *json_data* from .post()."""
    mock_response = MagicMock()
    mock_response.json.return_value = json_data
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_response)
    return mock_client


# ---------------------------------------------------------------------------
# TokenStore – fallback path
# ---------------------------------------------------------------------------


class TestTokenStoreFallbackPath:
    def test_fallback_path_is_home_agenthicc(self):
        store = TokenStore()
        assert str(store._fallback_path).endswith(".agenthicc/tokens.json")

    def test_fallback_write_creates_parents(self, tmp_path):
        store = TokenStore()
        nested = tmp_path / "deep" / "dir" / "tokens.json"
        store._fallback_path = nested
        bundle = _bundle()
        # Force keyring to fail so the file fallback runs
        with (
            patch("keyring.set_password", side_effect=Exception),
            patch("keyring.get_password", side_effect=Exception),
        ):
            store.save(bundle)
        assert nested.exists()
        data = json.loads(nested.read_text())
        assert data["access_token"] == "tok_access"

    def test_fallback_load_invalid_json(self, tmp_path):
        store = TokenStore()
        bad_file = tmp_path / "tokens.json"
        bad_file.write_text("NOT VALID JSON{{{")
        store._fallback_path = bad_file
        with patch("keyring.get_password", side_effect=Exception):
            result = store.load()
        assert result is None


# ---------------------------------------------------------------------------
# AuthClient – _find_free_port
# ---------------------------------------------------------------------------


class TestAuthClientFindFreePort:
    def test_finds_free_port_in_valid_range(self):
        port = AuthClient._find_free_port()
        assert isinstance(port, int)
        assert 1024 <= port <= 65535

    def test_port_is_actually_free(self):
        port = AuthClient._find_free_port()
        # If we can bind to it successfully, it was genuinely free
        with socket.socket() as s:
            s.bind(("localhost", port))
            assert s.getsockname()[1] == port


# ---------------------------------------------------------------------------
# AuthClient – _exchange_code
# ---------------------------------------------------------------------------


class TestAuthClientExchangeCode:
    async def test_exchange_code_success(self):
        resp_data = {
            "access_token": "new_access",
            "refresh_token": "new_refresh",
            "expires_in": 7200,
            "plan": "pro",
            "email": "alice@example.com",
            "user_id": "u42",
        }
        mock_client = _mock_httpx_client(resp_data)
        with patch("httpx.AsyncClient", return_value=mock_client):
            client = AuthClient()
            bundle = await client._exchange_code(
                code="auth_code_xyz",
                code_verifier="verifier_abc",
                redirect_uri="http://localhost:12345/callback",
            )
        assert bundle.access_token == "new_access"
        assert bundle.refresh_token == "new_refresh"
        assert bundle.plan == "pro"
        assert bundle.email == "alice@example.com"
        assert bundle.user_id == "u42"
        assert bundle.expires_at > time.time()

    async def test_exchange_code_uses_defaults_for_optional_fields(self):
        resp_data = {
            "access_token": "tok",
            "refresh_token": "ref",
            "expires_in": 3600,
            # plan, email, user_id absent – should default
        }
        mock_client = _mock_httpx_client(resp_data)
        with patch("httpx.AsyncClient", return_value=mock_client):
            client = AuthClient()
            bundle = await client._exchange_code("c", "v", "http://localhost/cb")
        assert bundle.plan == "free"
        assert bundle.email == ""
        assert bundle.user_id == ""

    async def test_exchange_code_http_error_propagates(self):
        import httpx

        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "401", request=MagicMock(), response=MagicMock()
        )
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            client = AuthClient()
            with pytest.raises(Exception):
                await client._exchange_code("c", "v", "http://localhost/cb")


# ---------------------------------------------------------------------------
# AuthClient – _refresh
# ---------------------------------------------------------------------------


class TestAuthClientRefresh:
    async def test_refresh_returns_new_bundle(self):
        old_bundle = _bundle(access_token="old_access", refresh_token="old_refresh")
        resp_data = {
            "access_token": "refreshed_access",
            "refresh_token": "new_refresh_token",
            "expires_in": 3600,
        }
        mock_client = _mock_httpx_client(resp_data)
        with patch("httpx.AsyncClient", return_value=mock_client):
            client = AuthClient()
            new_bundle = await client._refresh(old_bundle)
        assert new_bundle.access_token == "refreshed_access"
        assert new_bundle.refresh_token == "new_refresh_token"

    async def test_refresh_preserves_old_refresh_token_when_absent_in_response(self):
        old_bundle = _bundle(access_token="old_access", refresh_token="keep_me")
        resp_data = {
            "access_token": "new_access",
            # refresh_token NOT present – original should be kept
            "expires_in": 3600,
        }
        mock_client = _mock_httpx_client(resp_data)
        with patch("httpx.AsyncClient", return_value=mock_client):
            client = AuthClient()
            new_bundle = await client._refresh(old_bundle)
        assert new_bundle.refresh_token == "keep_me"

    async def test_refresh_preserves_email_and_user_id(self):
        old_bundle = _bundle(email="bob@example.com", user_id="u99")
        resp_data = {"access_token": "t", "expires_in": 3600}
        mock_client = _mock_httpx_client(resp_data)
        with patch("httpx.AsyncClient", return_value=mock_client):
            client = AuthClient()
            new_bundle = await client._refresh(old_bundle)
        assert new_bundle.email == "bob@example.com"
        assert new_bundle.user_id == "u99"

    async def test_refresh_preserves_plan_when_absent(self):
        old_bundle = _bundle(plan="enterprise")
        resp_data = {"access_token": "t", "expires_in": 3600}
        mock_client = _mock_httpx_client(resp_data)
        with patch("httpx.AsyncClient", return_value=mock_client):
            client = AuthClient()
            new_bundle = await client._refresh(old_bundle)
        assert new_bundle.plan == "enterprise"


# ---------------------------------------------------------------------------
# AuthClient – _revoke
# ---------------------------------------------------------------------------


class TestAuthClientRevoke:
    async def test_revoke_calls_endpoint_with_token(self):
        mock_client = _mock_httpx_client({})
        with patch("httpx.AsyncClient", return_value=mock_client):
            client = AuthClient()
            await client._revoke("my_access_token")
        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args
        assert call_args[0][0] == AGENTHICC_REVOKE_URL
        assert call_args[1]["data"]["token"] == "my_access_token"

    async def test_revoke_ignores_exceptions(self):
        """_revoke is best-effort; exceptions must not propagate."""
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(side_effect=Exception("network down"))

        with patch("httpx.AsyncClient", return_value=mock_client):
            client = AuthClient()
            # Should not raise
            await client._revoke("some_token")


# ---------------------------------------------------------------------------
# AuthClient – logout
# ---------------------------------------------------------------------------


class TestAuthClientLogout:
    async def test_logout_calls_revoke_and_clear_when_token_present(self):
        store = MagicMock()
        bundle = _bundle()
        store.load.return_value = bundle

        client = AuthClient(store)
        with patch.object(client, "_revoke", new_callable=AsyncMock) as mock_revoke:
            await client.logout()

        mock_revoke.assert_called_once_with(bundle.access_token)
        store.clear.assert_called_once()

    async def test_logout_with_no_token_skips_revoke(self):
        store = MagicMock()
        store.load.return_value = None

        client = AuthClient(store)
        with patch.object(client, "_revoke", new_callable=AsyncMock) as mock_revoke:
            await client.logout()

        mock_revoke.assert_not_called()
        store.clear.assert_called_once()


# ---------------------------------------------------------------------------
# AuthClient – current_bundle
# ---------------------------------------------------------------------------


class TestAuthClientLogin:
    """Cover the login() method (lines 121-168) with mocked aiohttp + webbrowser."""

    async def test_login_completes_and_stores_bundle(self):
        """Full mocked login flow: callback fires immediately with a code."""
        import asyncio

        store = MagicMock()
        client = AuthClient(store)

        # Stub _exchange_code to return a bundle directly
        expected_bundle = _bundle(access_token="login_token")

        async def fake_exchange(code, code_verifier, redirect_uri):
            return expected_bundle

        # Stub _find_free_port to return a fixed port
        with (
            patch.object(type(client), "_find_free_port", staticmethod(lambda: 54321)),
            patch.object(client, "_exchange_code", side_effect=fake_exchange),
            patch("webbrowser.open"),
            patch("builtins.print"),
        ):
            # We need to mock aiohttp.web so that the TCP server starts
            # and the callback future gets resolved.
            import asyncio as _asyncio

            class FakeRequest:
                query = {"code": "test_auth_code"}

            class FakeResponse:
                pass

            class FakeRunner:
                async def setup(self):
                    pass
                async def cleanup(self):
                    pass

            class FakeSite:
                def __init__(self, *a, **kw):
                    pass
                async def start(self):
                    pass

            fake_future: _asyncio.Future[str] = _asyncio.get_event_loop().create_future()

            original_get_event_loop = _asyncio.get_event_loop

            def fake_app_factory():
                class FakeApp:
                    class router:
                        @staticmethod
                        def add_get(path, handler):
                            # Immediately resolve the future with the code
                            loop = _asyncio.get_event_loop()
                            loop.call_soon(lambda: fake_future.set_result("test_auth_code") if not fake_future.done() else None)

                fake_app = FakeApp()
                return fake_app

            import types

            fake_web = types.SimpleNamespace(
                Application=fake_app_factory,
                AppRunner=lambda app: FakeRunner(),
                TCPSite=FakeSite,
                Request=FakeRequest,
                Response=lambda **kw: FakeResponse(),
            )

            with patch.dict("sys.modules", {"aiohttp": types.SimpleNamespace(web=fake_web), "aiohttp.web": fake_web}):
                # patch asyncio.wait_for to resolve immediately with the code
                async def fast_wait_for(coro_or_future, timeout):
                    return "test_auth_code"

                with patch("asyncio.wait_for", side_effect=fast_wait_for):
                    bundle = await client.login()

        assert bundle.access_token == "login_token"
        store.save.assert_called_once_with(expected_bundle)


class TestAuthClientCurrentBundle:
    def test_current_bundle_delegates_to_store(self):
        store = MagicMock()
        bundle = _bundle()
        store.load.return_value = bundle
        client = AuthClient(store)
        result = client.current_bundle()
        assert result is bundle
        store.load.assert_called_once()

    def test_current_bundle_returns_none_when_not_logged_in(self):
        store = MagicMock()
        store.load.return_value = None
        client = AuthClient(store)
        assert client.current_bundle() is None
