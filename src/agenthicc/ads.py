# src/agenthicc/ads.py
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agenthicc.auth import AuthClient
    from agenthicc.kernel.processor import EventProcessor

__all__ = ["AdRotator", "AdRecord", "AdCache"]

AGENTHICC_ADS_URL = "https://api.agenthicc.ai/v1/ads"
AD_CACHE_TTL      = 3600   # 1 hour
AD_ROTATION_SEC   = 60     # advance every 60 active seconds
AD_DISMISS_SEC    = 300    # Esc dismisses for 5 minutes
AD_MAX_LENGTH     = 120    # characters


@dataclass(frozen=True)
class AdRecord:
    ad_id: str
    text: str        # <= AD_MAX_LENGTH chars, plain UTF-8
    cta_url: str     # display-only URL, not a hyperlink

    def truncated(self) -> str:
        return self.text[:AD_MAX_LENGTH]


@dataclass
class AdCache:
    ads: list[AdRecord] = field(default_factory=list)
    fetched_at: float = 0.0

    @property
    def is_expired(self) -> bool:
        return time.time() - self.fetched_at > AD_CACHE_TTL

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "fetched_at": self.fetched_at,
            "ads": [{"ad_id": a.ad_id, "text": a.text, "cta_url": a.cta_url}
                    for a in self.ads],
        }))

    @classmethod
    def load(cls, path: Path) -> AdCache:
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text())
            return cls(
                fetched_at=data.get("fetched_at", 0.0),
                ads=[AdRecord(**a) for a in data.get("ads", [])],
            )
        except Exception:
            return cls()


class AdRotator:
    """Manages ad fetching, caching, rotation, and dismiss state.

    Designed to run as a background asyncio.Task. Emits UIAdUpdate events
    to the kernel when the displayed ad changes.
    """

    def __init__(
        self,
        auth_client: AuthClient,
        processor: EventProcessor | None,
        cache_path: Path = Path(".agenthicc/ads_cache.json"),
    ) -> None:
        self._auth = auth_client
        self._processor = processor
        self._cache = AdCache.load(cache_path)
        self._cache_path = cache_path
        self._index: int = 0
        self._dismissed_until: float = 0.0
        self._running = False

    @property
    def current_ad(self) -> AdRecord | None:
        if not self._cache.ads:
            return None
        if time.monotonic() < self._dismissed_until:
            return None
        return self._cache.ads[self._index % len(self._cache.ads)]

    def dismiss(self) -> None:
        """Dismiss the current ad for AD_DISMISS_SEC seconds."""
        self._dismissed_until = time.monotonic() + AD_DISMISS_SEC

    async def run(self) -> None:
        """Background loop: refresh cache and rotate ads."""
        self._running = True
        while self._running:
            if self._cache.is_expired:
                await self._fetch_ads()
            if self._processor is not None and self.current_ad is not None:
                from agenthicc.kernel import Event
                await self._processor.emit(Event.create(
                    "UIAdUpdate",
                    {"ad_id": self.current_ad.ad_id,
                     "text": self.current_ad.truncated(),
                     "cta_url": self.current_ad.cta_url},
                ))
            await asyncio.sleep(AD_ROTATION_SEC)
            self._index += 1

    async def stop(self) -> None:
        self._running = False

    async def _fetch_ads(self) -> None:
        try:
            from agenthicc.tools.http import agenthicc_http_client  # noqa: PLC0415
            token = await self._auth.get_token()
            async with agenthicc_http_client(timeout=5.0) as client:
                resp = await client.get(
                    AGENTHICC_ADS_URL,
                    headers={"Authorization": f"Bearer {token}"},
                )
                resp.raise_for_status()
                data = resp.json()
            self._cache = AdCache(
                ads=[AdRecord(ad_id=a["id"], text=a["text"], cta_url=a.get("cta_url", ""))
                     for a in data.get("ads", [])],
                fetched_at=time.time(),
            )
            self._cache.save(self._cache_path)
        except Exception:
            pass   # best-effort -- never fail the TUI over ads
