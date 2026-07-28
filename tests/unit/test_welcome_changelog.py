"""Clean-slate tests for the remote welcome-panel changelog."""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from rich.console import Console

from agenthicc.tui.welcome import (
    CHANGELOG_URL,
    _normalize_changelog,
    fetch_changelog,
    render_welcome,
)

pytestmark = pytest.mark.unit


def test_normalize_changelog_accepts_list_and_object_entries() -> None:
    assert _normalize_changelog(
        {
            "items": [
                "Bug fixes",
                {"title": "Faster startup"},
                {"version": "0.2", "changes": ["Nested change"]},
                {"description": ""},
                42,
            ]
        }
    ) == ["Bug fixes", "Faster startup", "Nested change"]


def test_normalize_changelog_rejects_missing_list() -> None:
    assert _normalize_changelog({"version": "1.0", "items": "not a list"}) == []
    assert _normalize_changelog("not json") == []


@pytest.mark.asyncio
async def test_fetch_changelog_uses_remote_json(monkeypatch: pytest.MonkeyPatch) -> None:
    requested: list[str] = []

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> object:
            return {"changelog": ["Remote update"]}

    class Client:
        async def get(self, url: str) -> Response:
            requested.append(url)
            return Response()

    @asynccontextmanager
    async def fake_http_client(**_: object):
        yield Client()

    import agenthicc.tools.http as http_tools

    monkeypatch.setattr(http_tools, "agenthicc_http_client", fake_http_client)

    assert await fetch_changelog() == ["Remote update"]
    assert requested == [CHANGELOG_URL]


@pytest.mark.asyncio
async def test_fetch_changelog_returns_empty_list_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @asynccontextmanager
    async def failing_http_client(**_: object):
        raise RuntimeError("network unavailable")
        yield  # pragma: no cover

    import agenthicc.tools.http as http_tools

    monkeypatch.setattr(http_tools, "agenthicc_http_client", failing_http_client)

    assert await fetch_changelog() == []


def test_welcome_keeps_heading_when_changelog_is_empty() -> None:
    console = Console(record=True, width=120)
    console.print(render_welcome(changelog=[]))

    output = console.export_text()
    assert "What's new" in output
    assert "No list" in output


def test_welcome_renders_remote_entries() -> None:
    console = Console(record=True, width=120)
    console.print(render_welcome(changelog=["Remote update"]))

    output = console.export_text()
    assert "What's new" in output
    assert "• Remote update" in output
    assert "No list" not in output
