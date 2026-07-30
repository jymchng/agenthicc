"""Transport-level coverage for the optional CloakBrowser adapter."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from agenthicc.config import CloakBrowserSettings
from agenthicc.tools.cloakbrowser.client import (
    CdpCloakBrowserClient,
    LocalCloakBrowserClient,
    PageState,
    UnavailableBrowserClient,
    _invoke,
    _invoke_callable,
    _page_state,
)
from agenthicc.tools.cloakbrowser.errors import BrowserErrorKind, BrowserToolError
from agenthicc.tools.cloakbrowser.policy import BrowserPolicy

pytestmark = pytest.mark.unit


async def _addresses() -> list[str]:
    return ["93.184.216.34"]


def _settings(**kwargs: object) -> CloakBrowserSettings:
    return CloakBrowserSettings(allowed_domains=["example.com"], **kwargs)  # type: ignore[arg-type]


def _policy() -> BrowserPolicy:
    return BrowserPolicy(("example.com",), resolver=lambda _host: _addresses())


class _MethodTarget:
    def sync(self, value: str) -> str:
        return value

    async def async_method(self, value: str) -> str:
        return value


def test_optional_invocation_helpers_support_sync_async_and_missing_methods() -> None:
    target = _MethodTarget()

    async def check() -> None:
        assert await _invoke(target, "sync", "sync") == "sync"
        assert await _invoke(target, "async_method", "async") == "async"
        assert await _invoke_callable(lambda value: value, "callable") == "callable"
        assert await _invoke_callable(lambda: asyncio.sleep(0, result="awaited")) == "awaited"
        with pytest.raises(BrowserToolError, match="does not support"):
            await _invoke(target, "missing")

    asyncio.run(check())


class _StatePage:
    def __init__(self) -> None:
        self.url = "https://example.com/path"

    async def title(self) -> str:
        return "Example"


def test_page_state_supports_callable_url_and_redacted_dataclasses() -> None:
    page = _StatePage()
    page.url = lambda: "https://example.com/path?token=secret"  # type: ignore[method-assign]
    state = asyncio.run(_page_state(page, "p1"))
    assert state == PageState("p1", "https://example.com/path?token=secret", "Example")
    assert "secret" not in str(state.to_dict())


@pytest.mark.parametrize(
    "method",
    ["open_page", "snapshot", "click", "fill", "press", "wait_for", "screenshot", "close_page"],
)
def test_unavailable_client_rejects_all_operations(method: str) -> None:
    client = UnavailableBrowserClient("local", BrowserErrorKind.DISABLED, "disabled")

    async def check() -> None:
        operation = getattr(client, method)
        args = {
            "open_page": ("s", "https://example.com"),
            "snapshot": ("s", "p"),
            "click": ("s", "p", "button"),
            "fill": ("s", "p", "input", "value"),
            "press": ("s", "p", "Enter", "input"),
            "wait_for": ("s", "p", "selector", "body"),
            "screenshot": ("s", "p", "png", False),
            "close_page": ("s", "p"),
        }[method]
        with pytest.raises(BrowserToolError) as exc:
            await operation(*args)
        assert exc.value.kind is BrowserErrorKind.DISABLED
        await client.close_session("s")

    asyncio.run(check())


class _FakeLocator:
    def __init__(self, values: list[object] | None = None, *, broken: bool = False) -> None:
        self.values = values or []
        self.first = self
        self.broken = broken
        self.actions: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    async def count(self) -> int:
        return len(self.values)

    async def nth(self, index: int) -> object:
        return self.values[index]

    async def get_attribute(self, attribute: str) -> str:
        if self.broken:
            raise RuntimeError("malformed element")
        return f"https://example.com/{attribute}"

    async def inner_text(self, **_kwargs: object) -> str:
        if self.broken:
            raise RuntimeError("malformed text")
        return "Control"

    async def click(self, *args: object, **kwargs: object) -> None:
        self.actions.append(("click", args, kwargs))

    async def fill(self, *args: object, **kwargs: object) -> None:
        self.actions.append(("fill", args, kwargs))

    async def press(self, *args: object, **kwargs: object) -> None:
        self.actions.append(("press", args, kwargs))

    async def wait_for(self, *args: object, **kwargs: object) -> None:
        self.actions.append(("wait_for", args, kwargs))


class _FakeRoute:
    def __init__(self) -> None:
        self.aborted = False
        self.continued = False

    async def abort(self, **_kwargs: object) -> None:
        self.aborted = True

    async def continue_(self) -> None:
        self.continued = True


class _FakePage:
    def __init__(self) -> None:
        self.url = "about:blank"
        self.closed = False
        self.route_handler = None
        self.popup_handler = None
        self.locators: dict[str, _FakeLocator] = {
            "body": _FakeLocator(),
            "a": _FakeLocator([_FakeLocator(), _FakeLocator(broken=True)]),
            "button,input,textarea,select": _FakeLocator([_FakeLocator()]),
            "input": _FakeLocator([_FakeLocator()]),
        }

    async def title(self) -> str:
        return "Example"

    async def route(self, _pattern: str, handler: object) -> None:
        self.route_handler = handler

    async def on(self, name: str, handler: object) -> None:
        if name == "popup":
            self.popup_handler = handler

    async def goto(self, url: str, **_kwargs: object) -> None:
        self.url = url

    async def locator(self, selector: str) -> _FakeLocator:
        return self.locators.get(selector, _FakeLocator())

    async def get_by_text(self, _value: str) -> _FakeLocator:
        return _FakeLocator()

    async def wait_for_url(self, value: str, **_kwargs: object) -> None:
        self.url = value.replace("*", "") or self.url

    async def wait_for_load_state(self, _value: str, **_kwargs: object) -> None:
        return None

    async def screenshot(self, **_kwargs: object) -> bytes:
        return b"image"

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


def _client(tmp_path: Path) -> LocalCloakBrowserClient:
    return LocalCloakBrowserClient(_settings(), _policy(), tmp_path, session_id="opaque")


def test_local_client_health_loads_diagnostics_and_handles_optional_module_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import agenthicc.tools.cloakbrowser.client as client_module

    class DiagnosticModule:
        def __init__(self, *, check: object = None, binary: object = None) -> None:
            self.check_installation = check
            self.binary_info = binary

    monkeypatch.setattr(client_module.importlib.util, "find_spec", lambda _name: object())
    monkeypatch.setattr(
        client_module.importlib,
        "import_module",
        lambda _name: DiagnosticModule(check=lambda: False),
    )
    assert asyncio.run(_client(tmp_path).health()).status == BrowserErrorKind.BINARY_MISSING.value
    monkeypatch.setattr(
        client_module.importlib,
        "import_module",
        lambda _name: DiagnosticModule(check=lambda: (_ for _ in ()).throw(RuntimeError("bad"))),
    )
    assert asyncio.run(_client(tmp_path).health()).status == BrowserErrorKind.BINARY_MISSING.value
    monkeypatch.setattr(
        client_module.importlib,
        "import_module",
        lambda _name: DiagnosticModule(binary=lambda: {"installed": False}),
    )
    assert asyncio.run(_client(tmp_path).health()).status == BrowserErrorKind.BINARY_MISSING.value
    monkeypatch.setattr(
        client_module.importlib,
        "import_module",
        lambda _name: DiagnosticModule(binary=lambda: (_ for _ in ()).throw(RuntimeError("bad"))),
    )
    assert asyncio.run(_client(tmp_path).health()).status == BrowserErrorKind.BINARY_MISSING.value


def test_local_client_reports_import_failure_and_reuses_loaded_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import agenthicc.tools.cloakbrowser.client as client_module

    monkeypatch.setattr(client_module.importlib.util, "find_spec", lambda _name: object())
    monkeypatch.setattr(
        client_module.importlib,
        "import_module",
        lambda _name: (_ for _ in ()).throw(RuntimeError("import failed")),
    )
    client = _client(tmp_path)
    assert asyncio.run(client.health()).status == BrowserErrorKind.BINARY_MISSING.value
    assert asyncio.run(client.health()).status == BrowserErrorKind.BINARY_MISSING.value


class _LauncherModule:
    def __init__(self, *, persistent: bool = True) -> None:
        self.context = _FakeContext()
        self.options: dict[str, object] | None = None
        self.profile: str | None = None
        if persistent:
            self.launch_persistent_context_async = self.persistent
        self.launch_context_async = self.launch

    async def launch(self, **kwargs: object) -> _FakeContext:
        self.options = kwargs
        return self.context

    async def persistent(self, profile: str, **kwargs: object) -> _FakeContext:
        self.profile = profile
        self.options = kwargs
        return self.context


def test_local_client_launches_both_context_modes_and_rebinds_before_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import agenthicc.tools.cloakbrowser.client as client_module

    local_module = _LauncherModule()
    monkeypatch.setattr(client_module.importlib.util, "find_spec", lambda _name: object())
    monkeypatch.setattr(client_module.importlib, "import_module", lambda _name: local_module)
    client = LocalCloakBrowserClient(
        _settings(allow_persistent_profiles=False), _policy(), tmp_path, session_id="first"
    )
    client.rebind_session("second")
    assert asyncio.run(client._get_context()) is local_module.context
    assert local_module.options is not None
    assert local_module.options["headless"] is True
    assert client._session_id == "second"
    client.rebind_session("third")
    assert client._session_id == "second"

    persistent_module = _LauncherModule()
    monkeypatch.setattr(client_module.importlib, "import_module", lambda _name: persistent_module)
    persistent = LocalCloakBrowserClient(
        _settings(allow_persistent_profiles=True), _policy(), tmp_path, session_id="session"
    )
    assert asyncio.run(persistent._get_context()) is persistent_module.context
    assert persistent_module.profile == str(
        tmp_path / ".agenthicc" / "browser-profiles" / "session"
    )
    asyncio.run(persistent.close_session("session"))


def test_local_client_rejects_unsupported_launchers_and_persistent_contexts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import agenthicc.tools.cloakbrowser.client as client_module

    monkeypatch.setattr(client_module.importlib.util, "find_spec", lambda _name: object())
    monkeypatch.setattr(client_module.importlib, "import_module", lambda _name: object())
    no_launcher = _client(tmp_path)
    with pytest.raises(BrowserToolError, match="no supported"):
        asyncio.run(no_launcher._get_context())
    no_persistent = LocalCloakBrowserClient(
        _settings(allow_persistent_profiles=True), _policy(), tmp_path
    )
    with pytest.raises(BrowserToolError, match="persistent"):
        asyncio.run(no_persistent._get_context())


def test_local_client_page_actions_snapshot_waits_screenshot_and_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import agenthicc.tools.cloakbrowser.client as client_module

    module = _LauncherModule()
    monkeypatch.setattr(client_module.importlib.util, "find_spec", lambda _name: object())
    monkeypatch.setattr(client_module.importlib, "import_module", lambda _name: module)
    client = _client(tmp_path)

    async def check() -> None:
        state = await client.open_page("session", "https://example.com/")
        assert state.page_id in {key[1] for key in client._pages}
        page = client._pages[("session", state.page_id)]
        assert isinstance(page, _FakePage)
        assert page.route_handler is not None
        route = _FakeRoute()
        await page.route_handler(route, SimpleNamespace(url="https://example.com/api"))  # type: ignore[operator]
        assert route.continued is True
        blocked = _FakeRoute()
        await page.route_handler(blocked, SimpleNamespace(url="https://evil.example"))  # type: ignore[operator]
        assert blocked.aborted is True
        snapshot = await client.snapshot("session", state.page_id)
        assert snapshot.text == "Control"
        assert snapshot.links
        await client.click("session", state.page_id, "button")
        await client.fill("session", state.page_id, "input", "value")
        await client.press("session", state.page_id, "Enter", "input")
        await client.wait_for("session", state.page_id, "selector", "button")
        await client.wait_for("session", state.page_id, "text", "Example")
        await client.wait_for("session", state.page_id, "url", "https://example.com/*")
        await client.wait_for("session", state.page_id, "load", "load")
        with pytest.raises(BrowserToolError, match="Load state"):
            await client.wait_for("session", state.page_id, "load", "invalid")
        assert (
            await client.screenshot("session", state.page_id, "jpeg", True)
        ).mime_type == "image/jpeg"
        with pytest.raises(BrowserToolError, match="format"):
            await client.screenshot("session", state.page_id, "gif", False)
        client._settings.max_screenshot_bytes = 1
        with pytest.raises(BrowserToolError, match="size"):
            await client.screenshot("session", state.page_id, "png", False)
        client._settings.max_screenshot_bytes = 1024
        await client.close_page("session", state.page_id)
        with pytest.raises(BrowserToolError, match="no longer"):
            await client.snapshot("session", state.page_id)
        await client.close_session("session")

    asyncio.run(check())


class _CdpPlaywright:
    def __init__(self, chromium: object = None) -> None:
        self.chromium = chromium
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


class _CdpContext:
    async def close(self) -> None:
        return None


class _CdpChromium:
    async def connect_over_cdp(self, _endpoint: str) -> object:
        return self

    async def new_context(self) -> _CdpContext:
        return _CdpContext()


def test_cdp_client_health_and_failure_boundaries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import agenthicc.tools.cloakbrowser.client as client_module

    class Manager:
        async def start(self) -> _CdpPlaywright:
            return _CdpPlaywright(_CdpChromium())

    monkeypatch.setattr(
        client_module.importlib,
        "import_module",
        lambda _name: SimpleNamespace(async_playwright=lambda: Manager()),
    )
    client = CdpCloakBrowserClient(_settings(transport="cdp"), _policy(), tmp_path)
    assert asyncio.run(client.health()).status == "ready"
    asyncio.run(client.close_session("session"))

    monkeypatch.setattr(
        client_module.importlib,
        "import_module",
        lambda _name: SimpleNamespace(async_playwright=None),
    )
    unavailable = CdpCloakBrowserClient(_settings(transport="cdp"), _policy(), tmp_path)
    assert asyncio.run(unavailable.health()).status == BrowserErrorKind.EXECUTION.value

    class NoContextBrowser:
        async def connect_over_cdp(self, _endpoint: str) -> object:
            return object()

    class NoContextManager:
        async def start(self) -> _CdpPlaywright:
            return _CdpPlaywright(NoContextBrowser())

    monkeypatch.setattr(
        client_module.importlib,
        "import_module",
        lambda _name: SimpleNamespace(async_playwright=lambda: NoContextManager()),
    )
    no_context = CdpCloakBrowserClient(_settings(transport="cdp"), _policy(), tmp_path)
    assert asyncio.run(no_context.health()).status == BrowserErrorKind.EXECUTION.value
