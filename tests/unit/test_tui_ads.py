"""Unit tests for ad rendering in TranscriptModel and TUIEventAdapter (PRD-11)."""
from __future__ import annotations

import pytest

from agenthicc.ads import AdRecord
from agenthicc.tui.transcript import TranscriptModel
from agenthicc.tui.events import TUIEventAdapter
from agenthicc.kernel import Event

pytestmark = pytest.mark.unit


def _ad(text="Buy our product", url="https://example.com") -> AdRecord:
    return AdRecord(ad_id="a1", text=text, cta_url=url)


class TestTranscriptModelAds:
    def test_no_ad_panel_when_no_ad_set(self):
        m = TranscriptModel()
        assert m.render_ad_panel() is None

    def test_ad_panel_contains_text(self):
        m = TranscriptModel()
        m.set_current_ad(_ad("Try Depot — faster CI"))
        panel = m.render_ad_panel()
        assert panel is not None
        assert "Depot" in panel

    def test_ad_panel_includes_cta_url(self):
        m = TranscriptModel()
        m.set_current_ad(_ad(url="https://depot.dev"))
        panel = m.render_ad_panel()
        assert "depot.dev" in panel

    def test_clear_ad(self):
        m = TranscriptModel()
        m.set_current_ad(_ad())
        m.set_current_ad(None)
        assert m.render_ad_panel() is None

    def test_long_ad_truncated_in_panel(self):
        m = TranscriptModel()
        m.set_current_ad(_ad(text="x" * 200))
        panel = m.render_ad_panel()
        assert len(panel) < 300   # truncated to 120 chars in the ad line


class TestTUIEventAdapterUIAdUpdate:
    def test_ui_ad_update_sets_model_ad(self):
        m = TranscriptModel()
        adapter = TUIEventAdapter(m)
        event = Event.create(
            "UIAdUpdate",
            {"ad_id": "a1", "text": "Sponsor text", "cta_url": "https://example.com"},
        )
        adapter.apply(event)
        assert m.current_ad() is not None

    def test_ad_text_from_payload(self):
        m = TranscriptModel()
        adapter = TUIEventAdapter(m)
        event = Event.create(
            "UIAdUpdate",
            {"ad_id": "a2", "text": "Unique sponsor message", "cta_url": ""},
        )
        adapter.apply(event)
        panel = m.render_ad_panel()
        assert panel is not None
        assert "Unique sponsor message" in panel
