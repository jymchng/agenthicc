"""Cached account tier: start-up never needs the OS keyring (AGX-02)."""

from __future__ import annotations

import json
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from agenthicc.auth import ACCOUNT_TIER_TTL_S, AuthClient, TokenBundle, TokenStore

pytestmark = pytest.mark.unit


def _bundle(plan: str) -> TokenBundle:
    return TokenBundle(
        access_token="secret-access-token",
        refresh_token="secret-refresh-token",
        expires_at=time.time() + 3600,
        plan=plan,
        email="person@example.com",
        user_id="user-7",
    )


def _store(tmp_path: Path) -> TokenStore:
    store = TokenStore()
    store._fallback_path = tmp_path / "tokens.json"
    return store


def _tier_file(tmp_path: Path) -> Path:
    return tmp_path / "account-tier.json"


@contextmanager
def _offline_keyring() -> Iterator[None]:
    """Force the file fallback, as on a host without a secret service."""
    with (
        patch("keyring.get_password", side_effect=RuntimeError("no backend")),
        patch("keyring.set_password", side_effect=RuntimeError("no backend")),
        patch("keyring.delete_password", side_effect=RuntimeError("no backend")),
    ):
        yield


@pytest.mark.parametrize(
    ("plan", "tier"), [("free", "free"), ("pro", "pro"), ("enterprise", "pro")]
)
def test_saving_tokens_records_the_tier_without_token_material(
    tmp_path: Path, plan: str, tier: str
) -> None:
    store = _store(tmp_path)
    with _offline_keyring():
        store.save(_bundle(plan))

    raw = _tier_file(tmp_path).read_text(encoding="utf-8")
    assert json.loads(raw)["tier"] == tier
    for secret in ("secret-access-token", "secret-refresh-token", "person@example.com", "user-7"):
        assert secret not in raw
    assert stat.S_IMODE(_tier_file(tmp_path).stat().st_mode) == 0o600


def test_clearing_tokens_records_a_signed_out_tier(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with _offline_keyring():
        store.save(_bundle("free"))
        store.clear()
    assert store.cached_tier() == "signed_out"


def test_a_fresh_cache_answers_without_reading_the_keyring(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store._remember_tier("pro", now=1_000.0)
    with patch.object(TokenStore, "load", side_effect=AssertionError("keyring read")):
        assert store.account_tier(now=1_000.0 + ACCOUNT_TIER_TTL_S - 1) == "pro"


@pytest.mark.parametrize("age", [ACCOUNT_TIER_TTL_S, ACCOUNT_TIER_TTL_S + 60, -60])
def test_a_stale_or_future_cache_is_refreshed_from_the_token_store(
    tmp_path: Path, age: float
) -> None:
    store = _store(tmp_path)
    store._remember_tier("pro", now=10_000.0)
    with patch.object(TokenStore, "load", return_value=_bundle("free")) as load:
        assert store.account_tier(now=10_000.0 + age) == "free"
    load.assert_called_once()
    assert store.cached_tier(now=10_000.0 + age) == "free"


def test_signed_out_users_are_cached_too(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with patch.object(TokenStore, "load", return_value=None):
        assert store.account_tier() == "signed_out"
    with patch.object(TokenStore, "load", side_effect=AssertionError("keyring read")):
        assert store.account_tier() == "signed_out"


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        json.dumps(["free"]),
        json.dumps({"schema": 99, "tier": "free", "checked_at": time.time()}),
        json.dumps({"schema": 1, "tier": "platinum", "checked_at": time.time()}),
        json.dumps({"schema": 1, "tier": "free", "checked_at": "yesterday"}),
        json.dumps({"schema": 1, "tier": "free", "checked_at": True}),
    ],
)
def test_malformed_caches_are_ignored(tmp_path: Path, content: str) -> None:
    store = _store(tmp_path)
    _tier_file(tmp_path).write_text(content, encoding="utf-8")
    assert store.cached_tier() is None


def test_an_unwritable_cache_still_returns_the_tier(tmp_path: Path) -> None:
    store = TokenStore()
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory", encoding="utf-8")
    store._fallback_path = blocker / "tokens.json"
    with patch.object(TokenStore, "load", return_value=_bundle("pro")):
        assert store.account_tier() == "pro"
    assert store.cached_tier() is None


def test_client_tier_delegates_to_its_store(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store._remember_tier("free")
    assert AuthClient(store).account_tier() == "free"


async def test_get_token_reads_the_store_off_the_event_loop(tmp_path: Path) -> None:
    import threading

    store = _store(tmp_path)
    loop_thread = threading.current_thread()
    seen: list[threading.Thread] = []

    def load() -> TokenBundle:
        seen.append(threading.current_thread())
        return _bundle("free")

    with patch.object(TokenStore, "load", side_effect=load):
        assert await AuthClient(store).get_token() == "secret-access-token"
    assert seen and seen[0] is not loop_thread
