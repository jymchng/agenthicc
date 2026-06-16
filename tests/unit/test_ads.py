"""Unit tests for AdRotator and AdCache (PRD-11)."""
from __future__ import annotations

import time
import pytest
from unittest.mock import MagicMock

from agenthicc.ads import AdCache, AdRecord, AdRotator

pytestmark = pytest.mark.unit


def _ad(n: int = 1) -> AdRecord:
    return AdRecord(ad_id=f"ad{n}", text=f"Ad number {n}", cta_url="https://example.com")


class TestAdCache:
    def test_empty_cache_is_expired(self):
        assert AdCache().is_expired

    def test_fresh_cache_not_expired(self):
        cache = AdCache(ads=[_ad()], fetched_at=time.time())
        assert not cache.is_expired

    def test_save_load_round_trip(self, tmp_path):
        path = tmp_path / "ads.json"
        cache = AdCache(ads=[_ad(1), _ad(2)], fetched_at=time.time())
        cache.save(path)
        loaded = AdCache.load(path)
        assert len(loaded.ads) == 2
        assert loaded.ads[0].ad_id == "ad1"

    def test_load_missing_file_returns_empty(self, tmp_path):
        loaded = AdCache.load(tmp_path / "nonexistent.json")
        assert loaded.ads == []


class TestAdRotator:
    def test_no_ad_when_cache_empty(self):
        r = AdRotator(auth_client=MagicMock(), processor=None)
        assert r.current_ad is None

    def test_returns_first_ad(self):
        r = AdRotator(auth_client=MagicMock(), processor=None)
        r._cache = AdCache(ads=[_ad(1), _ad(2)], fetched_at=time.time())
        assert r.current_ad == _ad(1)

    def test_rotates_on_index_advance(self):
        r = AdRotator(auth_client=MagicMock(), processor=None)
        r._cache = AdCache(ads=[_ad(1), _ad(2)], fetched_at=time.time())
        r._index = 1
        assert r.current_ad == _ad(2)

    def test_dismiss_hides_ad(self):
        r = AdRotator(auth_client=MagicMock(), processor=None)
        r._cache = AdCache(ads=[_ad(1)], fetched_at=time.time())
        r.dismiss()
        assert r.current_ad is None

    def test_truncates_long_ad_text(self):
        long_text = "x" * 200
        ad = AdRecord(ad_id="a1", text=long_text, cta_url="")
        assert len(ad.truncated()) == 120
