"""Unit tests for AuthClient and TokenStore (PRD-11)."""

from __future__ import annotations

import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agenthicc.auth import AuthClient, NotLoggedInError, TokenBundle, TokenStore

pytestmark = pytest.mark.unit


def _bundle(plan="free", expires_offset=3600) -> TokenBundle:
    return TokenBundle(
        access_token="tok_access",
        refresh_token="tok_refresh",
        expires_at=time.time() + expires_offset,
        plan=plan,
        email="user@example.com",
        user_id="u1",
    )


class TestTokenBundle:
    def test_not_expired_when_fresh(self):
        assert not _bundle(expires_offset=3600).is_expired

    def test_expired_within_buffer(self):
        # expires in 30s < TOKEN_REFRESH_BUFFER (60s)
        assert _bundle(expires_offset=30).is_expired

    def test_is_pro_for_pro_plan(self):
        assert _bundle(plan="pro").is_pro

    def test_is_not_pro_for_free(self):
        assert not _bundle(plan="free").is_pro

    def test_round_trip_dict(self):
        b = _bundle()
        assert TokenBundle.from_dict(b.to_dict()).access_token == b.access_token


class TestTokenStore:
    def test_returns_none_when_empty(self, tmp_path):
        store = TokenStore()
        store._fallback_path = tmp_path / "tokens.json"
        with patch("keyring.get_password", return_value=None):
            assert store.load() is None

    def test_save_and_load_via_fallback(self, tmp_path):
        store = TokenStore()
        store._fallback_path = tmp_path / "tokens.json"
        bundle = _bundle()
        # keyring unavailable on both save and load → must use file fallback
        with (
            patch("keyring.set_password", side_effect=Exception),
            patch("keyring.get_password", side_effect=Exception),
        ):
            store.save(bundle)
        with patch("keyring.get_password", side_effect=Exception):
            loaded = store.load()
        assert loaded is not None
        assert loaded.access_token == bundle.access_token

    def test_clear_removes_entry(self, tmp_path):
        store = TokenStore()
        store._fallback_path = tmp_path / "tokens.json"
        bundle = _bundle()
        # Force fallback path for both save and clear
        with (
            patch("keyring.set_password", side_effect=Exception),
            patch("keyring.get_password", side_effect=Exception),
        ):
            store.save(bundle)
        with (
            patch("keyring.delete_password", side_effect=Exception),
            patch("keyring.get_password", side_effect=Exception),
        ):
            store.clear()
        with patch("keyring.get_password", side_effect=Exception):
            assert store.load() is None


class TestAuthClientGetToken:
    async def test_returns_token_when_valid(self):
        store = MagicMock()
        store.load.return_value = _bundle(expires_offset=3600)
        client = AuthClient(store)
        token = await client.get_token()
        assert token == "tok_access"

    async def test_raises_when_not_logged_in(self):
        store = MagicMock()
        store.load.return_value = None
        client = AuthClient(store)
        with pytest.raises(NotLoggedInError):
            await client.get_token()

    async def test_refreshes_expired_token(self):
        store = MagicMock()
        store.load.return_value = _bundle(expires_offset=30)  # expires soon
        client = AuthClient(store)
        new_bundle = _bundle(expires_offset=3600)
        with patch.object(client, "_refresh", new_callable=AsyncMock, return_value=new_bundle):
            token = await client.get_token()
        assert token == new_bundle.access_token
        store.save.assert_called_once_with(new_bundle)
