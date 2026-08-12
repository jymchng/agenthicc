"""Unit coverage for the optional Playwright browser backend."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from pathlib import Path

import pytest

from agenthicc.config import PlaywrightSettings, load_config
from agenthicc.tools.capabilities import ToolCapability, get_tool_capabilities
from agenthicc.tools.cloakbrowser import BrowserPolicy, BrowserSessionManager
from agenthicc.tools.cloakbrowser.errors import BrowserErrorKind
from agenthicc.tools.playwright import (
    PLAYWRIGHT_AGENT_TOOLS,
    create_playwright_session,
    make_playwright_tools,
)
from agenthicc.tools.playwright.client import PlaywrightBrowserClient
from agenthicc.workflows.create_workflow.validation import validate_workflow_file

pytestmark = pytest.mark.unit


class _FakeBrowserClient:
    async def health(self):
        from agenthicc.tools.cloakbrowser.client import BrowserHealth

        return BrowserHealth("ready", "local", "")

    async def open_page(self, session_id: str, url: str):
        from agenthicc.tools.cloakbrowser.client import PageState

        return PageState("page-1", url, "Fixture")

    async def snapshot(self, session_id: str, page_id: str):
        from agenthicc.tools.cloakbrowser.client import PageSnapshot, PageState

        return PageSnapshot(PageState(page_id, "https://example.com/", "Fixture"), "fixture")

    async def click(self, session_id: str, page_id: str, selector: str):
        from agenthicc.tools.cloakbrowser.client import PageState

        return PageState(page_id, "https://example.com/", "Fixture")

    async def fill(self, session_id: str, page_id: str, selector: str, value: str):
        return await self.click(session_id, page_id, selector)

    async def press(self, session_id: str, page_id: str, key: str, selector: str):
        return await self.click(session_id, page_id, selector)

    async def wait_for(self, session_id: str, page_id: str, condition: str, value: str):
        return await self.click(session_id, page_id, "body")

    async def screenshot(self, session_id: str, page_id: str, image_type: str, full_page: bool):
        from agenthicc.tools.cloakbrowser.client import ScreenshotData

        return ScreenshotData(b"png", "image/png")

    async def close_page(self, session_id: str, page_id: str) -> None:
        return None

    async def close_session(self, session_id: str) -> None:
        return None


def _manager(tmp_path: Path) -> BrowserSessionManager:
    return BrowserSessionManager(
        PlaywrightSettings(enabled=True, allowed_domains=["example.com"]),
        "conversation-1",
        tmp_path,
        client=_FakeBrowserClient(),
        policy=BrowserPolicy(
            ("example.com",),
            resolver=lambda _host: _addresses(),
        ),
        backend_name="Playwright",
    )


async def _addresses() -> list[str]:
    return ["93.184.216.34"]


def test_playwright_tools_match_the_shared_browser_contract(tmp_path: Path) -> None:
    tools = make_playwright_tools(_manager(tmp_path))

    assert tuple(tool.__name__ for tool in tools) == PLAYWRIGHT_AGENT_TOOLS
    assert get_tool_capabilities(tools[0]) == frozenset({ToolCapability.READ})
    assert get_tool_capabilities(tools[1]) == frozenset(
        {ToolCapability.READ, ToolCapability.NETWORK}
    )
    assert get_tool_capabilities(tools[4]) == frozenset(
        {ToolCapability.WRITE, ToolCapability.NETWORK}
    )


def test_playwright_tools_enforce_shared_policy_and_artifacts(tmp_path: Path) -> None:
    async def check() -> None:
        manager = _manager(tmp_path)
        tools = {tool.__name__: tool for tool in make_playwright_tools(manager)}

        opened = await tools["playwright_open"]("https://example.com/")
        assert opened["ok"] is True
        page_id = str(opened["page"]["page_id"])
        assert (await tools["playwright_snapshot"](page_id))["ok"] is True
        assert (await tools["playwright_screenshot"](page_id))["ok"] is True
        denied = await tools["playwright_open"]("https://not-example.test/")
        assert denied["error_kind"] == BrowserErrorKind.POLICY_DENIED.value
        await manager.close_session()

    asyncio.run(check())


def test_playwright_open_restarts_manager_after_close_session(tmp_path: Path) -> None:
    async def check() -> None:
        manager = _manager(tmp_path)
        tools = {tool.__name__: tool for tool in make_playwright_tools(manager)}

        first = await tools["playwright_open"](
            "https://example.com/", operation_id="open-before-close"
        )
        assert first["ok"] is True

        await manager.close_session()
        assert (await manager.status())["status"] == BrowserErrorKind.CLOSED.value

        # The closures are retained by the agent registry across cleanup. The
        # public open tool must lazily reactivate the same manager and client.
        reopened = await tools["playwright_open"](
            "https://example.com/", operation_id="open-before-close"
        )
        assert reopened["ok"] is True
        assert (await manager.status())["ok"] is True

    asyncio.run(check())


def test_playwright_config_is_optional_and_selectable(tmp_path: Path) -> None:
    config_path = tmp_path / "agenthicc.toml"
    config_path.write_text(
        """
        [tools]
        browser_backend = "playwright"

        [tools.playwright]
        browser_type = "firefox"
        allowed_domains = ["https://example.com", "https://*.example.org:8443"]
        allow_all_domains = false
        max_pages = 2
        """,
        encoding="utf-8",
    )

    config = load_config(project_path=config_path, user_path=tmp_path / "missing.toml")

    assert config.tools.browser_backend == "playwright"
    assert config.tools.playwright.browser_type == "firefox"
    assert config.tools.playwright.allowed_domains == [
        "https://*.example.org:8443",
        "https://example.com",
    ]
    assert config.tools.playwright.allow_all_domains is False
    assert config.tools.playwright.max_pages == 2


def test_playwright_default_policy_allows_loopback_and_private_addresses(
    tmp_path: Path,
) -> None:
    settings = PlaywrightSettings()
    manager = create_playwright_session(settings, "conversation-default", tmp_path)

    assert settings.allow_all_domains is True
    assert manager.policy is not None
    assert manager.policy.allow_all_domains is True
    assert manager.policy.allow_loopback is True
    assert manager.policy.allow_private_addresses is True
    assert 3000 in manager.policy.allowed_ports


def test_playwright_config_rejects_invalid_browser_type() -> None:
    with pytest.raises(ValueError, match="browser_type"):
        PlaywrightSettings(browser_type="safari")


def test_playwright_policy_allows_local_preview_ports(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "agenthicc.tools.cloakbrowser.policy.socket.getaddrinfo",
        lambda *_args, **_kwargs: [("", "", "", "", ("127.0.0.1", 0))],
    )
    manager = create_playwright_session(
        PlaywrightSettings(enabled=False, allowed_domains=["localhost"]),
        "conversation-local",
        tmp_path,
    )

    async def check() -> None:
        assert manager.policy is not None
        assert 3000 in manager.policy.allowed_ports
        assert manager.policy.allow_loopback is True
        assert await manager.policy.validate_url("http://localhost:3000/") == (
            "http://localhost:3000/"
        )

    asyncio.run(check())


def test_missing_playwright_dependency_is_structured(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = __import__("importlib").import_module

    def missing(name: str, *args: object, **kwargs: object) -> object:
        if name == "playwright.async_api":
            raise ModuleNotFoundError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("agenthicc.tools.playwright.client.importlib.import_module", missing)
    client = PlaywrightBrowserClient(
        PlaywrightSettings(enabled=True, allowed_domains=["example.com"]),
        BrowserPolicy(("example.com",), resolver=lambda _host: _addresses()),
        Path.cwd(),
    )

    health = asyncio.run(client.health())

    assert health.status == BrowserErrorKind.DEPENDENCY_MISSING.value
    assert "playwright" in health.message.lower()


class _FakeLocator:
    first = None

    def __init__(self) -> None:
        self.first = self

    async def inner_text(self, **_kwargs: object) -> str:
        return "fixture body"

    async def count(self) -> int:
        return 0

    async def click(self, **_kwargs: object) -> None:
        return None

    async def fill(self, _value: str, **_kwargs: object) -> None:
        return None

    async def press(self, _key: str, **_kwargs: object) -> None:
        return None

    async def wait_for(self, **_kwargs: object) -> None:
        return None


class _FakePage:
    def __init__(self) -> None:
        self.url = "about:blank"
        self.route_handler = None
        self.closed = False

    async def title(self) -> str:
        return "Fixture"

    async def route(self, _pattern: str, handler: object) -> None:
        self.route_handler = handler

    async def goto(self, url: str, **_kwargs: object) -> None:
        self.url = url

    async def locator(self, _selector: str) -> _FakeLocator:
        return _FakeLocator()

    async def screenshot(self, **_kwargs: object) -> bytes:
        return b"png"

    async def close(self) -> None:
        self.closed = True


class _FakeContext:
    def __init__(self) -> None:
        self.pages: list[_FakePage] = []
        self.closed = False

    async def new_page(self) -> _FakePage:
        page = _FakePage()
        self.pages.append(page)
        return page

    async def close(self) -> None:
        self.closed = True


class _FakeBrowser:
    def __init__(self) -> None:
        self.context = _FakeContext()
        self.closed = False

    async def new_context(self, **_kwargs: object) -> _FakeContext:
        return self.context

    async def close(self) -> None:
        self.closed = True


class _FakeBrowserType:
    def __init__(self) -> None:
        self.browser = _FakeBrowser()

    async def launch(self, **_kwargs: object) -> _FakeBrowser:
        return self.browser

    async def launch_persistent_context(self, _profile: str, **_kwargs: object) -> _FakeContext:
        return self.browser.context


class _FakePlaywright:
    def __init__(self) -> None:
        self.chromium = _FakeBrowserType()

    async def stop(self) -> None:
        return None


class _FakePlaywrightContextManager:
    async def start(self) -> _FakePlaywright:
        return _FakePlaywright()


class _FakeRoute:
    def __init__(self) -> None:
        self.aborted = False
        self.continued = False

    async def abort(self, **_kwargs: object) -> None:
        self.aborted = True

    async def continue_(self) -> None:
        self.continued = True


def test_playwright_client_launches_and_routes_subresources(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import agenthicc.tools.playwright.client as playwright_client

    monkeypatch.setattr(
        playwright_client.importlib,
        "import_module",
        lambda _name: SimpleNamespace(async_playwright=lambda: _FakePlaywrightContextManager()),
    )
    client = PlaywrightBrowserClient(
        PlaywrightSettings(enabled=True, allowed_domains=["example.com"]),
        BrowserPolicy(("example.com",), resolver=lambda _host: _addresses()),
        tmp_path,
    )

    async def check() -> None:
        health = await client.health()
        assert health.status == "ready"
        page_state = await client.open_page("session", "https://example.com/")
        snapshot = await client.snapshot("session", page_state.page_id)
        assert snapshot.text == "fixture body"
        await client.click("session", page_state.page_id, "button.submit")
        screenshot = await client.screenshot("session", page_state.page_id, "png", False)
        assert screenshot.content == b"png"

        page = client._pages[("session", page_state.page_id)]
        assert isinstance(page, _FakePage)
        assert page.route_handler is not None
        denied_route = _FakeRoute()
        await page.route_handler(denied_route, SimpleNamespace(url="https://not-example.test/"))
        assert denied_route.aborted is True
        allowed_route = _FakeRoute()
        await page.route_handler(allowed_route, SimpleNamespace(url="https://example.com/api"))
        assert allowed_route.continued is True
        await client.close_session("session")
        assert page.closed is True
        reopened = await client.open_page("session", "https://example.com/")
        assert reopened.page_id != ""
        await client.close_session("session")

    asyncio.run(check())


def test_playwright_client_honors_channel_executable_and_persistent_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import agenthicc.tools.playwright.client as playwright_client

    class BrowserType(_FakeBrowserType):
        def __init__(self) -> None:
            super().__init__()
            self.launch_options: dict[str, object] | None = None
            self.profile: str | None = None

        async def launch_persistent_context(self, profile: str, **kwargs: object) -> _FakeContext:
            self.profile = profile
            self.launch_options = kwargs
            return self.browser.context

    browser_type = BrowserType()

    class Playwright(_FakePlaywright):
        def __init__(self) -> None:
            self.chromium = browser_type

    class ContextManager(_FakePlaywrightContextManager):
        async def start(self) -> Playwright:
            return Playwright()

    monkeypatch.setattr(
        playwright_client.importlib,
        "import_module",
        lambda _name: SimpleNamespace(async_playwright=lambda: ContextManager()),
    )
    client = PlaywrightBrowserClient(
        PlaywrightSettings(
            enabled=True,
            allowed_domains=["example.com"],
            browser_channel="chrome",
            executable_path="/usr/bin/chromium",
            allow_persistent_profiles=True,
            profile_root="profiles",
        ),
        BrowserPolicy(("example.com",), resolver=lambda _host: _addresses()),
        tmp_path,
        session_id="opaque-session",
    )

    async def check() -> None:
        context = await client._get_context()
        assert isinstance(context, _FakeContext)
        assert browser_type.profile == str(tmp_path / "profiles" / "opaque-session")
        assert browser_type.launch_options == {
            "headless": True,
            "channel": "chrome",
            "executable_path": "/usr/bin/chromium",
            "accept_downloads": True,
        }
        await client.close_session("session")

    asyncio.run(check())


@pytest.mark.parametrize(
    "api_factory",
    [
        lambda: SimpleNamespace(async_playwright=None),
        lambda: SimpleNamespace(async_playwright=lambda: object()),
    ],
)
def test_playwright_client_reports_startup_shape_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    api_factory: object,
) -> None:
    import agenthicc.tools.playwright.client as playwright_client

    monkeypatch.setattr(playwright_client.importlib, "import_module", lambda _name: api_factory())  # type: ignore[operator]
    client = PlaywrightBrowserClient(
        PlaywrightSettings(allowed_domains=["example.com"]),
        BrowserPolicy(("example.com",), resolver=lambda _host: _addresses()),
        tmp_path,
    )
    health = asyncio.run(client.health())
    assert health.status in {
        BrowserErrorKind.EXECUTION.value,
        BrowserErrorKind.BROWSER_UNAVAILABLE.value,
    }


def test_playwright_client_reports_missing_browser_type_and_runtime_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import agenthicc.tools.playwright.client as playwright_client

    class MissingBrowserType:
        async def start(self) -> object:
            return SimpleNamespace()

    monkeypatch.setattr(
        playwright_client.importlib,
        "import_module",
        lambda _name: SimpleNamespace(async_playwright=lambda: MissingBrowserType()),
    )
    missing = PlaywrightBrowserClient(
        PlaywrightSettings(browser_type="firefox", allowed_domains=["example.com"]),
        BrowserPolicy(("example.com",), resolver=lambda _host: _addresses()),
        tmp_path,
    )
    assert asyncio.run(missing.health()).status == BrowserErrorKind.INVALID_INPUT.value

    class FailingStart:
        async def start(self) -> object:
            raise RuntimeError("browser binary missing")

    monkeypatch.setattr(
        playwright_client.importlib,
        "import_module",
        lambda _name: SimpleNamespace(async_playwright=lambda: FailingStart()),
    )
    failing = PlaywrightBrowserClient(
        PlaywrightSettings(allowed_domains=["example.com"]),
        BrowserPolicy(("example.com",), resolver=lambda _host: _addresses()),
        tmp_path,
    )
    assert asyncio.run(failing.health()).status == BrowserErrorKind.BROWSER_UNAVAILABLE.value


def test_playwright_health_keeps_unexpected_errors_safe(tmp_path: Path) -> None:
    client = PlaywrightBrowserClient(
        PlaywrightSettings(allowed_domains=["example.com"]),
        BrowserPolicy(("example.com",), resolver=lambda _host: _addresses()),
        tmp_path,
    )

    async def broken() -> object:
        raise RuntimeError("private runtime detail")

    client._get_context = broken  # type: ignore[method-assign]
    health = asyncio.run(client.health())
    assert health.status == BrowserErrorKind.BROWSER_UNAVAILABLE.value
    assert "private runtime" not in health.message


def test_playwright_cleanup_ignores_shutdown_errors(tmp_path: Path) -> None:
    class BrokenClose:
        async def close(self) -> None:
            raise RuntimeError("close failed")

    class BrokenStop:
        async def stop(self) -> None:
            raise RuntimeError("stop failed")

    client = PlaywrightBrowserClient(
        PlaywrightSettings(allowed_domains=["example.com"]),
        BrowserPolicy(("example.com",), resolver=lambda _host: _addresses()),
        tmp_path,
    )
    client._context = BrokenClose()
    client._browser = BrokenClose()
    client._playwright = BrokenStop()
    asyncio.run(client._cleanup_runtime())
    assert client._context is None
    assert client._browser is None
    assert client._playwright is None


def test_generated_workflows_may_use_canonical_playwright_tools(tmp_path: Path) -> None:
    source = """
from agenthicc.workflows.plugin import PhaseSpec, WorkflowPlugin

class Demo(WorkflowPlugin):
    name = "demo"
    description = "demo"
    phases = [PhaseSpec(name="one")]

TOOL_NAME = "playwright_open"
"""
    path = tmp_path / "demo.py"
    path.write_text(source, encoding="utf-8")

    report = validate_workflow_file(str(path), expected_name="demo", root=tmp_path)

    assert report.ok is True


def test_generated_workflows_cannot_import_playwright_directly(tmp_path: Path) -> None:
    source = """
from playwright.async_api import async_playwright
from agenthicc.workflows.plugin import PhaseSpec, WorkflowPlugin

class Demo(WorkflowPlugin):
    name = "demo"
    description = "demo"
    phases = [PhaseSpec(name="one")]
"""
    path = tmp_path / "demo.py"
    path.write_text(source, encoding="utf-8")

    report = validate_workflow_file(str(path), expected_name="demo", root=tmp_path)

    assert report.ok is False
    assert "must not import" in report.errors[0]
