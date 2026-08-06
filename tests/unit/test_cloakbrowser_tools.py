"""Unit coverage for the optional CloakBrowser boundary (PRD-159)."""

from __future__ import annotations

import asyncio
from pathlib import Path
import textwrap

import pytest

from agenthicc.config import CloakBrowserSettings, load_config
from agenthicc.tools.capabilities import ToolCapability, get_tool_capabilities
from agenthicc.tools.cloakbrowser import (
    BrowserArtifactStore,
    BrowserErrorKind,
    BrowserPolicy,
    BrowserSessionManager,
    BrowserToolError,
    PageSnapshot,
    PageState,
    ScreenshotData,
    make_cloakbrowser_tools,
)
from agenthicc.tools.cloakbrowser.client import BrowserHealth
from agenthicc.tools.cloakbrowser.client import LocalCloakBrowserClient
from agenthicc.tools.sandbox import WorkspaceView

pytestmark = pytest.mark.unit


class FakeBrowserClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    async def health(self) -> BrowserHealth:
        return BrowserHealth("ready", "local")

    async def open_page(self, session_id: str, url: str) -> PageState:
        self.calls.append(("open", (session_id, url)))
        return PageState("page-1", url, "Example")

    async def snapshot(self, session_id: str, page_id: str) -> PageSnapshot:
        return PageSnapshot(
            PageState(page_id, "https://example.com/path?secret=1", "Example"),
            "content",
            links=({"href": "/next", "text": "Next"},),
        )

    async def click(self, session_id: str, page_id: str, selector: str) -> PageState:
        self.calls.append(("click", (selector,)))
        return PageState(page_id, "https://example.com/after", "After")

    async def fill(self, session_id: str, page_id: str, selector: str, value: str) -> PageState:
        self.calls.append(("fill", (selector, value)))
        return PageState(page_id, "https://example.com/", "Example")

    async def press(self, session_id: str, page_id: str, key: str, selector: str) -> PageState:
        self.calls.append(("press", (key, selector)))
        return PageState(page_id, "https://example.com/", "Example")

    async def wait_for(
        self, session_id: str, page_id: str, condition: str, value: str
    ) -> PageState:
        return PageState(page_id, "https://example.com/", "Example")

    async def screenshot(
        self,
        session_id: str,
        page_id: str,
        image_type: str,
        full_page: bool,
    ) -> ScreenshotData:
        return ScreenshotData(b"fake-image", "image/png")

    async def close_page(self, session_id: str, page_id: str) -> None:
        self.calls.append(("close", (page_id,)))

    async def close_session(self, session_id: str) -> None:
        self.calls.append(("close_session", (session_id,)))


class LargeSnapshotClient(FakeBrowserClient):
    async def snapshot(self, session_id: str, page_id: str) -> PageSnapshot:
        return PageSnapshot(
            PageState(page_id, "https://example.com/", "Example"),
            "x" * 300,
            links=({"href": "https://example.com/next?token=hidden", "text": "Next"},) * 100,
        )


def _policy() -> BrowserPolicy:
    async def resolve(_host: str) -> list[str]:
        return ["93.184.216.34"]

    return BrowserPolicy(("example.com",), resolver=resolve)


def _manager(
    tmp_path: Path, *, max_actions: int = 20
) -> tuple[BrowserSessionManager, FakeBrowserClient]:
    client = FakeBrowserClient()
    settings = CloakBrowserSettings(
        enabled=True,
        allowed_domains=["example.com"],
        max_actions_per_turn=max_actions,
    )
    manager = BrowserSessionManager(
        settings,
        "conversation-1",
        tmp_path,
        client=client,
        policy=_policy(),
    )
    return manager, client


def test_default_is_enabled_but_empty_allow_list_denies_navigation() -> None:
    manager = BrowserSessionManager(CloakBrowserSettings(), "s", Path.cwd())
    assert manager.enabled is True
    assert manager.settings.allow_all_domains is False
    assert len(make_cloakbrowser_tools(manager)) == 9
    status = asyncio.run(manager.status())
    assert status["status"] == BrowserErrorKind.DISABLED.value
    assert status["ok"] is False


def test_loaded_default_configuration_enables_deny_all_browser_surface(tmp_path: Path) -> None:
    config = load_config(
        project_path=tmp_path / "missing.toml", user_path=tmp_path / "also-missing.toml"
    )

    assert config.tools.cloakbrowser.enabled is True
    assert config.tools.cloakbrowser.allowed_domains == []
    assert config.tools.cloakbrowser.allow_all_domains is False


def test_empty_policy_denies_every_destination() -> None:
    async def check() -> None:
        with pytest.raises(BrowserToolError) as exc:
            await BrowserPolicy().validate_url("https://example.com/")
        assert exc.value.kind is BrowserErrorKind.POLICY_DENIED

    asyncio.run(check())


def test_allow_all_domains_supports_public_hosts_but_not_private_addresses() -> None:
    async def resolve(host: str) -> list[str]:
        return ["127.0.0.1"] if host == "localhost" else ["93.184.216.34"]

    async def check() -> None:
        policy = BrowserPolicy(allow_all_domains=True, resolver=resolve)
        assert await policy.validate_url("https://any-public-host.example/") == (
            "https://any-public-host.example/"
        )
        with pytest.raises(BrowserToolError) as loopback:
            await policy.validate_url("http://localhost/")
        assert loopback.value.kind is BrowserErrorKind.POLICY_DENIED
        with pytest.raises(BrowserToolError):
            await policy.validate_url("http://10.0.0.1/")
        with pytest.raises(BrowserToolError):
            await policy.validate_url("https://any-public-host.example:8443/")

    asyncio.run(check())


def test_manager_applies_allow_all_domains_setting(tmp_path: Path) -> None:
    manager = BrowserSessionManager(
        CloakBrowserSettings(enabled=True, allow_all_domains=True),
        "conversation-1",
        tmp_path,
        client=FakeBrowserClient(),
    )

    assert manager.policy.allow_all_domains is True
    assert manager.policy.allowed_domains == ()


def test_missing_optional_dependency_is_a_safe_health_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("importlib.util.find_spec", lambda _name: None)
    client = LocalCloakBrowserClient(
        CloakBrowserSettings(enabled=True, allowed_domains=["example.com"]),
        _policy(),
        Path.cwd(),
    )
    health = asyncio.run(client.health())
    assert health.status == BrowserErrorKind.DEPENDENCY_MISSING.value
    assert "install" in health.message.lower()


def test_config_parses_optional_browser_section(tmp_path: Path) -> None:
    config_path = tmp_path / "agenthicc.toml"
    config_path.write_text(
        """
        [tools.cloakbrowser]
        enabled = true
        transport = "local"
        allowed_domains = ["example.com", ".example.org"]
        allow_all_domains = true
        max_pages = 2
        max_actions_per_turn = 7
        """,
        encoding="utf-8",
    )
    config = load_config(project_path=config_path, user_path=tmp_path / "missing.toml")
    assert config.tools.cloakbrowser.enabled is True
    assert config.tools.cloakbrowser.allowed_domains == ["example.com", "example.org"]
    assert config.tools.cloakbrowser.allow_all_domains is True
    assert config.tools.cloakbrowser.max_pages == 2
    assert config.tools.cloakbrowser.max_actions_per_turn == 7


def test_config_preserves_cors_style_origins(tmp_path: Path) -> None:
    config_path = tmp_path / "agenthicc.toml"
    config_path.write_text(
        """
        [tools.cloakbrowser]
        allowed_domains = ["HTTPS://Example.com", "https://*.example.org:8443"]
        """,
        encoding="utf-8",
    )

    config = load_config(project_path=config_path, user_path=tmp_path / "missing.toml")

    assert config.tools.cloakbrowser.allowed_domains == [
        "https://*.example.org:8443",
        "https://example.com",
    ]


def test_policy_rejects_unsafe_destinations() -> None:
    async def check() -> None:
        with pytest.raises(BrowserToolError) as exc:
            await _policy().validate_url("https://not-example.test/")
        assert exc.value.kind is BrowserErrorKind.POLICY_DENIED
        with pytest.raises(BrowserToolError):
            await _policy().validate_url("file:///etc/passwd")
        with pytest.raises(BrowserToolError):
            await _policy().validate_url("https://example.com/#fragment")

    asyncio.run(check())


def test_policy_supports_exact_and_wildcard_cors_style_origins() -> None:
    async def resolve(_host: str) -> list[str]:
        return ["93.184.216.34"]

    async def check() -> None:
        exact = BrowserPolicy(("https://example.com",), resolver=resolve)
        assert await exact.validate_url("https://example.com/path") == ("https://example.com/path")
        assert await exact.validate_url("https://example.com:443/path") == (
            "https://example.com:443/path"
        )
        with pytest.raises(BrowserToolError):
            await exact.validate_url("http://example.com/path")
        with pytest.raises(BrowserToolError):
            await exact.validate_url("https://sub.example.com/path")

        custom_port = BrowserPolicy(("https://example.com:8443",), resolver=resolve)
        assert await custom_port.validate_url("https://example.com:8443/path") == (
            "https://example.com:8443/path"
        )
        with pytest.raises(BrowserToolError):
            await custom_port.validate_url("https://example.com/path")

        wildcard = BrowserPolicy(("https://*.example.com",), resolver=resolve)
        assert await wildcard.validate_url("https://sub.example.com/path") == (
            "https://sub.example.com/path"
        )
        with pytest.raises(BrowserToolError):
            await wildcard.validate_url("https://example.com/path")

    asyncio.run(check())


def test_policy_rejects_unsafe_allow_list_entries() -> None:
    for value in (
        "*",
        "https://user:secret@example.com",
        "https://example.com/path",
        "https://example.com?token=secret",
        "https://example.com/#fragment",
    ):
        with pytest.raises(ValueError):
            BrowserPolicy((value,))


def test_manager_redacts_outputs_and_enforces_sensitive_fields(tmp_path: Path) -> None:
    async def check() -> None:
        manager, _client = _manager(tmp_path)
        opened = await manager.open("https://example.com/path?token=secret")
        assert opened["ok"] is True
        snapshot = await manager.snapshot("page-1")
        assert "secret" not in str(snapshot)
        denied = await manager.fill("page-1", "input[name=password]", "not-recorded")
        assert denied["ok"] is False
        assert denied["error_kind"] == BrowserErrorKind.POLICY_DENIED.value

    asyncio.run(check())


def test_manager_bounds_untrusted_snapshot_data_at_the_session_boundary(tmp_path: Path) -> None:
    async def check() -> None:
        settings = CloakBrowserSettings(
            enabled=True,
            allowed_domains=["example.com"],
            max_snapshot_chars=256,
        )
        manager = BrowserSessionManager(
            settings,
            "conversation-1",
            tmp_path,
            client=LargeSnapshotClient(),
            policy=_policy(),
        )
        await manager.open("https://example.com/")
        result = await manager.snapshot("page-1")
        snapshot = result["snapshot"]
        assert isinstance(snapshot, dict)
        assert len(snapshot["text"]) == 256
        assert snapshot["truncated"] is True
        assert len(snapshot["links"]) == 50
        assert "hidden" not in str(snapshot["links"])

    asyncio.run(check())


def test_manager_limits_actions_and_checkpoint_has_no_live_objects(tmp_path: Path) -> None:
    async def check() -> None:
        manager, _client = _manager(tmp_path, max_actions=1)
        await manager.open("https://example.com/")
        first = await manager.click("page-1", "button.submit")
        second = await manager.press("page-1", "Enter")
        assert first["ok"] is True
        assert second["error_kind"] == BrowserErrorKind.LIMIT_EXCEEDED.value
        checkpoint = manager.checkpoint_payload()
        assert checkpoint["session_id"] != "conversation-1"
        assert checkpoint["conversation_id"] == "conversation-1"
        assert "client" not in checkpoint
        assert all(isinstance(item, dict) for item in checkpoint["open_pages"])
        manager.restore_checkpoint(checkpoint)
        assert (await manager.snapshot("page-1"))["error_kind"] == BrowserErrorKind.NOT_FOUND.value

    asyncio.run(check())


def test_operation_id_replays_result_without_repeating_browser_action(tmp_path: Path) -> None:
    async def check() -> None:
        manager, client = _manager(tmp_path)
        first = await manager.open("https://example.com/", operation_id="open-once")
        second = await manager.open("https://example.com/", operation_id="open-once")
        assert first == second
        assert first["operation_id"] == "open-once"
        assert [name for name, _args in client.calls].count("open") == 1

        checkpoint = manager.checkpoint_payload()
        assert checkpoint["completed_operation_ids"] == ["open-once"]
        manager.restore_checkpoint(checkpoint)
        resumed_retry = await manager.open("https://example.com/", operation_id="open-once")
        assert resumed_retry["error_kind"] == BrowserErrorKind.STALE_PAGE.value
        assert [name for name, _args in client.calls].count("open") == 1

        invalid = await manager.open("https://example.com/", operation_id="bad id")
        assert invalid["error_kind"] == BrowserErrorKind.INVALID_INPUT.value

    asyncio.run(check())


def test_cloakbrowser_open_restarts_manager_after_close_session(tmp_path: Path) -> None:
    async def check() -> None:
        manager, client = _manager(tmp_path)
        tools = {tool.__name__: tool for tool in make_cloakbrowser_tools(manager)}

        first = await tools["cloakbrowser_open"](
            "https://example.com/", operation_id="open-before-close"
        )
        assert first["ok"] is True
        assert (await manager.click("page-1", "button.submit"))["ok"] is True
        assert manager.actions_used == 1

        await manager.close_session()
        assert (await manager.status())["status"] == BrowserErrorKind.CLOSED.value
        assert manager.actions_used == 0

        # The closures are retained by the agent registry across cleanup. The
        # public open tool must lazily reactivate the same manager and client.
        reopened = await tools["cloakbrowser_open"](
            "https://example.com/", operation_id="open-before-close"
        )
        assert reopened["ok"] is True
        assert (await manager.status())["ok"] is True
        assert (await manager.click("page-1", "button.submit"))["ok"] is True
        assert [name for name, _args in client.calls].count("open") == 2

    asyncio.run(check())


def test_tools_have_expected_names_and_capabilities(tmp_path: Path) -> None:
    manager, _client = _manager(tmp_path)
    tools = make_cloakbrowser_tools(manager)
    assert [tool.__name__ for tool in tools] == [
        "cloakbrowser_status",
        "cloakbrowser_open",
        "cloakbrowser_snapshot",
        "cloakbrowser_click",
        "cloakbrowser_fill",
        "cloakbrowser_press",
        "cloakbrowser_wait_for",
        "cloakbrowser_screenshot",
        "cloakbrowser_close",
    ]
    assert get_tool_capabilities(tools[0]) == frozenset({ToolCapability.READ})
    assert get_tool_capabilities(tools[1]) == frozenset(
        {ToolCapability.READ, ToolCapability.NETWORK}
    )
    assert get_tool_capabilities(tools[4]) == frozenset(
        {ToolCapability.WRITE, ToolCapability.NETWORK}
    )


def test_artifact_store_stays_inside_workspace(tmp_path: Path) -> None:
    store = BrowserArtifactStore(WorkspaceView(tmp_path))
    artifact = store.write_screenshot("conversation-1", b"png", mime_type="image/png")
    assert artifact.byte_count == 3
    assert (tmp_path / artifact.relative_path).read_bytes() == b"png"
    with pytest.raises(ValueError):
        store.write_screenshot("../escape", b"x")


def test_cdp_endpoint_is_loopback_only() -> None:
    with pytest.raises(ValueError):
        CloakBrowserSettings(
            enabled=True,
            transport="cdp",
            cdp_endpoint="http://remote.example:9222",
            allowed_domains=["example.com"],
        )
    with pytest.raises(ValueError):
        CloakBrowserSettings(
            enabled=True,
            transport="cdp",
            cdp_endpoint="http://[invalid",
            allowed_domains=["example.com"],
        )


def test_persistent_profile_root_cannot_escape_workspace() -> None:
    with pytest.raises(ValueError):
        CloakBrowserSettings(
            enabled=True,
            allow_persistent_profiles=True,
            profile_root="../profiles",
            allowed_domains=["example.com"],
        )


def test_generated_workflow_cannot_bypass_browser_adapter(tmp_path: Path) -> None:
    from agenthicc.workflows.create_workflow.validation import validate_workflow_file

    source = textwrap.dedent(
        """
        from playwright.async_api import async_playwright
        from agenthicc.workflows.plugin import PhaseSpec, WorkflowPlugin

        class Demo(WorkflowPlugin):
            name = "demo"
            description = "demo"
            phases = [PhaseSpec(name="one")]
        """
    )
    path = tmp_path / "demo.py"
    path.write_text(source, encoding="utf-8")
    report = validate_workflow_file(str(path), expected_name="demo", root=tmp_path)
    assert report.ok is False
    assert "must not import" in report.errors[0]

    unknown = tmp_path / "unknown.py"
    unknown.write_text(
        source.replace(
            "from playwright.async_api import async_playwright", "TOOL = 'cloakbrowser_unknown'"
        ),
        encoding="utf-8",
    )
    report = validate_workflow_file(str(unknown), expected_name="demo", root=tmp_path)
    assert report.ok is False
    assert "Unknown browser tool" in " ".join(report.errors)
