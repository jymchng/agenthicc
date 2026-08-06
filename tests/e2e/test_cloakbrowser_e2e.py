"""Deterministic E2E journey using a fake browser transport.

The test exercises the public agent tool closures end-to-end without live
internet access, a paid CloakBrowser license, or a browser binary.
"""

from __future__ import annotations

import importlib.util
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

import pytest

from agenthicc.config import CloakBrowserSettings
from agenthicc.tools.cloakbrowser import (
    BrowserPolicy,
    BrowserSessionManager,
    make_cloakbrowser_tools,
)
from agenthicc.tools.cloakbrowser.client import PageSnapshot, PageState
from tests.unit.test_cloakbrowser_tools import FakeBrowserClient

pytestmark = pytest.mark.e2e


@pytest.mark.cloakbrowser
@pytest.mark.skipif(
    importlib.util.find_spec("cloakbrowser") is None,
    reason="install the optional cloakbrowser extra for the upstream contract check",
)
def test_optional_cloakbrowser_exports_are_available() -> None:
    import cloakbrowser

    assert callable(cloakbrowser.launch_async)
    assert callable(cloakbrowser.launch_context_async)
    assert callable(cloakbrowser.launch_persistent_context_async)


class _FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
        body = b"<html><body><h1>Fixture</h1><button>Continue</button></body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return None


class _FixtureClient(FakeBrowserClient):
    body: str
    body_url: str

    async def open_page(self, session_id: str, url: str) -> PageState:
        self.body = await asyncio.to_thread(lambda: urlopen(url, timeout=2).read().decode())
        return PageState("fixture-page", url, "Fixture")

    async def snapshot(self, session_id: str, page_id: str) -> PageSnapshot:
        return PageSnapshot(PageState(page_id, self.body_url, "Fixture"), self.body)


@pytest.mark.asyncio
async def test_local_http_fixture_journey_with_fake_browser_transport(tmp_path: Path) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
    server_task = asyncio.create_task(asyncio.to_thread(server.serve_forever))
    await asyncio.sleep(0.02)
    try:
        client = _FixtureClient()
        url = f"http://127.0.0.1:{server.server_port}/"
        client.body_url = url

        async def resolve(_host: str) -> list[str]:
            return ["127.0.0.1"]

        manager = BrowserSessionManager(
            CloakBrowserSettings(enabled=True, allowed_domains=["127.0.0.1"]),
            "fixture-session",
            tmp_path,
            client=client,
            policy=BrowserPolicy(
                ("127.0.0.1",),
                allowed_ports=frozenset({server.server_port}),
                allow_loopback=True,
                allow_private_addresses=True,
                resolver=resolve,
            ),
        )
        tools = {tool.__name__: tool for tool in make_cloakbrowser_tools(manager)}
        opened = await tools["cloakbrowser_open"](url)
        assert opened["ok"] is True
        snapshot = await tools["cloakbrowser_snapshot"]("fixture-page")
        assert snapshot["snapshot"]["untrusted"] is True
        assert "Fixture" in snapshot["snapshot"]["text"]
        screenshot = await tools["cloakbrowser_screenshot"]("fixture-page")
        assert screenshot["ok"] is True
        await tools["cloakbrowser_close"]("fixture-page")
        await manager.close_session()
    finally:
        await asyncio.to_thread(server.shutdown)
        await asyncio.to_thread(server.server_close)
        await server_task


@pytest.mark.asyncio
async def test_browser_agent_journey_open_observe_interact_capture_close(tmp_path: Path) -> None:
    async def resolve(_host: str) -> list[str]:
        return ["93.184.216.34"]

    manager = BrowserSessionManager(
        CloakBrowserSettings(enabled=True, allowed_domains=["example.com"]),
        "conversation-e2e",
        tmp_path,
        client=FakeBrowserClient(),
        policy=BrowserPolicy(("example.com",), resolver=resolve),
    )
    tools = {tool.__name__: tool for tool in make_cloakbrowser_tools(manager)}

    opened = await tools["cloakbrowser_open"]("https://example.com/")
    assert opened["ok"] is True
    page_id = str(opened["page"]["page_id"])
    assert (await tools["cloakbrowser_snapshot"](page_id))["ok"] is True
    assert (await tools["cloakbrowser_click"](page_id, "button.submit"))["ok"] is True
    screenshot = await tools["cloakbrowser_screenshot"](page_id)
    assert screenshot["ok"] is True
    assert str(screenshot["artifact"]["path"]).startswith(".agenthicc/browser-artifacts/")
    assert (await tools["cloakbrowser_close"](page_id))["ok"] is True
    await manager.close_session()
    reopened = await tools["cloakbrowser_open"]("https://example.com/")
    assert reopened["ok"] is True
    await manager.close_session()
