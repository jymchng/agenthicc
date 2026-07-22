"""Extended unit tests for AdRotator, AdCache, and AdRecord (coverage extension)."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agenthicc.ads import (
    AD_CACHE_TTL,
    AdCache,
    AdRecord,
    AdRotator,
)

pytestmark = pytest.mark.unit


# ── helpers ───────────────────────────────────────────────────────────────


def _make_ad(
    ad_id: str = "ad-1", text: str = "Buy now", cta_url: str = "https://x.com"
) -> AdRecord:
    return AdRecord(ad_id=ad_id, text=text, cta_url=cta_url)


def _make_cache(ads: list | None = None, fetched_at: float | None = None) -> AdCache:
    """Build an AdCache. Pass ads=[] for an explicitly empty cache."""
    return AdCache(
        ads=[_make_ad()] if ads is None else ads,
        fetched_at=fetched_at if fetched_at is not None else time.time(),
    )


def _make_auth(token: str = "tok") -> MagicMock:
    auth = MagicMock()
    auth.get_token = AsyncMock(return_value=token)
    return auth


def _make_rotator(
    ads: list | None = None,
    fetched_at: float | None = None,
    processor: object | None = None,
    cache_path: Path | None = None,
) -> AdRotator:
    auth = _make_auth()
    rotator = AdRotator(
        auth_client=auth,
        processor=processor,
        cache_path=cache_path or Path("/dev/null/nonexistent_ads.json"),
    )
    # Override cache directly to avoid file I/O
    rotator._cache = _make_cache(ads=ads, fetched_at=fetched_at)
    return rotator


# ── AdCache.is_expired edge ───────────────────────────────────────────────


class TestAdCacheExpiry:
    def test_cache_fresh_not_expired(self):
        cache = _make_cache(fetched_at=time.time())
        assert not cache.is_expired

    def test_cache_exactly_expired(self):
        """fetched_at = now - AD_CACHE_TTL means is_expired is True."""
        cache = _make_cache(fetched_at=time.time() - AD_CACHE_TTL)
        assert cache.is_expired

    def test_cache_far_past_expired(self):
        cache = _make_cache(fetched_at=0.0)
        assert cache.is_expired

    def test_cache_one_second_before_ttl(self):
        """One second before TTL boundary: not yet expired."""
        cache = _make_cache(fetched_at=time.time() - AD_CACHE_TTL + 1)
        assert not cache.is_expired

    def test_cache_load_missing_file_returns_empty(self, tmp_path):
        """AdCache.load on a nonexistent path returns an empty AdCache."""
        p = tmp_path / "no_such_file.json"
        cache = AdCache.load(p)
        assert cache.ads == []
        assert cache.fetched_at == 0.0

    def test_cache_load_corrupt_json_returns_empty(self, tmp_path):
        """AdCache.load on corrupt JSON returns an empty AdCache."""
        p = tmp_path / "corrupt.json"
        p.write_text("{{{invalid json")
        cache = AdCache.load(p)
        assert cache.ads == []

    def test_cache_save_and_reload(self, tmp_path):
        """save() + load() round-trips the cache correctly."""
        p = tmp_path / "ads.json"
        original = AdCache(
            ads=[_make_ad("ad-99", "Hello", "https://z.com")],
            fetched_at=1234567890.0,
        )
        original.save(p)
        loaded = AdCache.load(p)
        assert len(loaded.ads) == 1
        assert loaded.ads[0].ad_id == "ad-99"
        assert loaded.fetched_at == 1234567890.0


# ── AdRotator.current_ad ──────────────────────────────────────────────────


class TestCurrentAd:
    def test_current_ad_none_when_cache_empty(self):
        rotator = _make_rotator(ads=[])  # explicitly empty
        assert rotator.current_ad is None

    def test_current_ad_returns_ad_when_available(self):
        rotator = _make_rotator()
        assert rotator.current_ad is not None
        assert rotator.current_ad.ad_id == "ad-1"

    def test_current_ad_none_after_dismiss(self):
        """After dismiss(), current_ad returns None for AD_DISMISS_SEC."""
        rotator = _make_rotator()
        assert rotator.current_ad is not None
        rotator.dismiss()
        assert rotator.current_ad is None

    def test_dismiss_expires(self):
        """Setting dismissed_until in the past restores current_ad."""
        rotator = _make_rotator()
        rotator.dismiss()
        assert rotator.current_ad is None
        # Wind back the dismiss timer
        rotator._dismissed_until = time.monotonic() - 1.0
        assert rotator.current_ad is not None

    def test_current_ad_cycles_through_multiple_ads(self):
        ads = [_make_ad("a1"), _make_ad("a2"), _make_ad("a3")]
        rotator = _make_rotator(ads=ads)
        rotator._index = 0
        assert rotator.current_ad.ad_id == "a1"
        rotator._index = 1
        assert rotator.current_ad.ad_id == "a2"
        rotator._index = 2
        assert rotator.current_ad.ad_id == "a3"
        # Wraps around
        rotator._index = 3
        assert rotator.current_ad.ad_id == "a1"


# ── AdRotator.stop ────────────────────────────────────────────────────────


class TestRotatorStop:
    async def test_stop_exits_loop(self):
        """stop() sets _running=False, breaking the run() loop quickly."""
        rotator = _make_rotator()
        _real_sleep = asyncio.sleep  # capture reference before patching

        async def _fast_sleep(n: float) -> None:
            # yield to event loop once so run() can check _running, then return
            await _real_sleep(0)

        with patch("agenthicc.ads.asyncio.sleep", new=_fast_sleep):
            task = asyncio.create_task(rotator.run())
            await _real_sleep(0)  # let run() start its first iteration
            await rotator.stop()
            await _real_sleep(0)  # let run() observe _running=False
            try:
                await asyncio.wait_for(task, timeout=1.0)
            except asyncio.TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                pytest.fail("rotator.run() did not exit after stop()")

        assert rotator._running is False


# ── AdRotator.run emits UIAdUpdate ───────────────────────────────────────


class TestRotatorRunEmits:
    async def test_rotator_run_emits_ui_ad_update(self):
        """run() emits a UIAdUpdate event when there is a current ad."""
        mock_processor = MagicMock()
        mock_processor.emit = AsyncMock()

        rotator = _make_rotator(processor=mock_processor)
        # Ensure cache is fresh (not expired) so _fetch_ads is not called
        rotator._cache = _make_cache(fetched_at=time.time())

        async def _fast_sleep(n: float) -> None:
            await rotator.stop()

        with patch("agenthicc.ads.asyncio.sleep", side_effect=_fast_sleep):
            await rotator.run()

        mock_processor.emit.assert_called_once()
        call_args = mock_processor.emit.call_args[0][0]
        assert call_args.event_type == "UIAdUpdate"

    async def test_rotator_run_does_not_emit_when_no_current_ad(self):
        """run() skips emit when there is no current ad (cache empty)."""
        mock_processor = MagicMock()
        mock_processor.emit = AsyncMock()

        rotator = _make_rotator(ads=[], processor=mock_processor)
        rotator._cache = AdCache(ads=[], fetched_at=time.time())

        async def _fast_sleep(n: float) -> None:
            await rotator.stop()

        with patch("agenthicc.ads.asyncio.sleep", side_effect=_fast_sleep):
            await rotator.run()

        mock_processor.emit.assert_not_called()

    async def test_rotator_run_increments_index(self):
        """Each run() iteration increments _index."""
        rotator = _make_rotator(processor=None)
        rotator._cache = _make_cache(fetched_at=time.time())
        iteration_count = [0]

        async def _fast_sleep(n: float) -> None:
            iteration_count[0] += 1
            if iteration_count[0] >= 2:
                await rotator.stop()

        with patch("agenthicc.ads.asyncio.sleep", side_effect=_fast_sleep):
            await rotator.run()

        assert rotator._index >= 1


# ── AdRotator._fetch_ads ──────────────────────────────────────────────────


class TestFetchAds:
    async def test_fetch_ads_success_populates_cache(self):
        """_fetch_ads on a successful HTTP response populates self._cache."""
        rotator = _make_rotator()
        rotator._cache = AdCache(ads=[], fetched_at=0.0)

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "ads": [{"id": "fetched-1", "text": "A great offer", "cta_url": "https://e.com"}]
        }

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            await rotator._fetch_ads()

        assert len(rotator._cache.ads) == 1
        assert rotator._cache.ads[0].ad_id == "fetched-1"
        assert rotator._cache.fetched_at > 0

    async def test_fetch_ads_http_error_no_crash(self):
        """_fetch_ads swallows exceptions and leaves cache unchanged."""
        rotator = _make_rotator()
        original_cache = rotator._cache

        with patch("httpx.AsyncClient", side_effect=Exception("network error")):
            await rotator._fetch_ads()

        # Cache unchanged on failure
        assert rotator._cache is original_cache

    async def test_fetch_ads_uses_auth_token(self):
        """_fetch_ads calls get_token() to obtain the auth token."""
        auth = _make_auth(token="my-token-123")
        rotator = AdRotator(
            auth_client=auth,
            processor=None,
            cache_path=Path("/dev/null/x.json"),
        )
        rotator._cache = AdCache(ads=[], fetched_at=0.0)

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"ads": []}

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            await rotator._fetch_ads()

        auth.get_token.assert_awaited_once()
        # Verify the auth header was sent with the correct token
        call_kwargs = mock_client.get.call_args[1]
        assert call_kwargs["headers"]["Authorization"] == "Bearer my-token-123"

    async def test_fetch_ads_http_status_error_no_crash(self):
        """_fetch_ads handles raise_for_status() errors without crashing."""
        import httpx

        rotator = _make_rotator()
        original_cache = rotator._cache

        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "403", request=MagicMock(), response=MagicMock()
        )

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            await rotator._fetch_ads()

        assert rotator._cache is original_cache

    async def test_fetch_ads_saves_cache_to_path(self, tmp_path):
        """_fetch_ads saves the populated cache to _cache_path."""
        cache_file = tmp_path / "ads_cache.json"
        auth = _make_auth()
        rotator = AdRotator(auth_client=auth, processor=None, cache_path=cache_file)
        rotator._cache = AdCache(ads=[], fetched_at=0.0)

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "ads": [{"id": "saved-1", "text": "Saved ad", "cta_url": ""}]
        }

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            await rotator._fetch_ads()

        assert cache_file.exists()
        reloaded = AdCache.load(cache_file)
        assert len(reloaded.ads) == 1
        assert reloaded.ads[0].ad_id == "saved-1"


# ── AdRecord.truncated ────────────────────────────────────────────────────


class TestAdRecord:
    def test_truncated_short_text(self):
        ad = AdRecord(ad_id="x", text="short", cta_url="")
        assert ad.truncated() == "short"

    def test_truncated_long_text(self):
        long_text = "A" * 200
        ad = AdRecord(ad_id="x", text=long_text, cta_url="")
        result = ad.truncated()
        assert len(result) == 120
        assert result == "A" * 120
