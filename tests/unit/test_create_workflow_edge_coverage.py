"""Boundary coverage for the create_workflow catalog, draft, and validator."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agenthicc.tools.capabilities import ToolCapability, tool_network_read, tool_write
from agenthicc.tui.conversation_store import AppState
from agenthicc.workflows.create_workflow import catalog
from agenthicc.workflows.create_workflow.catalog import (
    AuthoringSnapshot,
    AuthoringSnapshotCache,
    ToolCatalogEntry,
    build_authoring_snapshot,
    build_tool_catalog,
    explain_tool_access,
)
from agenthicc.workflows.create_workflow.draft import (
    DraftError,
    build_draft_path,
    publish_draft,
    scan_draft,
    stage_legacy_package,
)
from agenthicc.workflows.create_workflow.inspection_tools import make_inspection_tools
from agenthicc.workflows.create_workflow.validation import validate_workflow_file
from agenthicc.workflows import name_that_ui
from lauren_ai._tools import tool

pytestmark = pytest.mark.unit


@tool_write
@tool()
async def write_edge(path: str, content: str) -> dict[str, object]:
    """Write a bounded edge-test document."""
    return {"path": path, "content": content}


@tool_network_read
@tool()
async def network_edge(url: str) -> dict[str, object]:
    """Read a remote edge-test document."""
    return {"url": url}


def _config(*, browser: object | None = None, mcp: object | None = None) -> SimpleNamespace:
    app = AppState.create()
    return SimpleNamespace(
        app_state=app,
        all_plugin_tools=lambda: [write_edge, network_edge],
        mcp_registry=mcp,
        browser_tools=[],
        browser_manager=browser,
        workspace_scope=SimpleNamespace(primary_root=Path("/workspace")),
        workspace_access=SimpleNamespace(mode="safe"),
    )


def test_catalog_helpers_bound_and_redact_all_supported_shapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert (
        catalog._redact({"Authorization": "secret", "nested": {"token": "x"}})["Authorization"]
        == "[redacted]"
    )
    assert catalog._redact(["a"] * 200)
    assert catalog._redact(object())
    assert catalog._bounded_text("\x00 value ") == "value"
    monkeypatch.setattr(catalog, "_tool_schema", lambda _tool: ["not-a-mapping"])
    assert catalog._schema(write_edge)["name"] == "write_edge"
    huge = {"description": "x" * 40_000, "parameters": {"x": "y"}}
    monkeypatch.setattr(catalog, "_tool_schema", lambda _tool: huge)
    assert catalog._schema(write_edge)["parameters"]["bounded"] is True  # type: ignore[index]
    assert catalog._source_for(write_edge, {"write_edge": "explicit"}) == "explicit"
    assert catalog._source_for(network_edge, {}) == "plugin"
    assert catalog._capability_values(None) == ()
    assert catalog._capability_values(ToolCapability.WRITE) == ("write",)
    assert catalog._capability_values(7) == ()

    with pytest.raises(ValueError, match="require a name"):
        ToolCatalogEntry(" ", "", {}, (), False, "plugin")
    entry = ToolCatalogEntry("edge", "description", {"x": 1}, (), False, "plugin")
    assert "fingerprint" not in entry.to_dict(include_fingerprint=False)
    assert entry.to_dict()["fingerprint"]


def test_catalog_access_decisions_and_snapshot_rendering_cover_optional_paths() -> None:
    mode = SimpleNamespace(name="Plan", blocked_capabilities=frozenset({ToolCapability.WRITE}))
    entries = build_tool_catalog(
        [write_edge, write_edge, network_edge],
        active_mode=mode,
        phase_capabilities=(ToolCapability.READ,),
        allowed_tool_names=frozenset({"network_edge"}),
        unavailable_by_name={"network_edge": "dependency_missing"},
        source_by_name={"network_edge": "mcp"},
    )
    assert len(entries) == 2
    write_entry = next(item for item in entries if item.name == "write_edge")
    decision = explain_tool_access(
        write_entry,
        active_mode=mode,
        phase_capabilities=(ToolCapability.READ,),
        policy_constraints=("policy",),
    )
    assert decision.available is False
    assert decision.to_dict()["reasons"]
    assert catalog._render_items([{"x": "y"}])

    snapshot = AuthoringSnapshot(
        snapshot_id="snapshot",
        phase_name="phase",
        browser={"backend": "playwright"},
        mcp=({"server": "server", "secret": "hide"},),
        workspace={"root": "/workspace"},
        cache={"stable": True},
        checkpoint={"schema": 1},
        unavailable=({"name": "browser", "reason": "missing"},),
        tools=tuple(entries),
    )
    rendered = snapshot.render()
    assert "BROWSER:" in rendered
    assert "MCP:" in rendered
    assert "UNAVAILABLE OPTIONAL FEATURES:" in rendered
    assert "secret" in rendered  # key is retained while its value is redacted
    assert snapshot.checkpoint_reference()["tool_names"]


def test_catalog_browser_mcp_and_cache_failure_paths() -> None:
    assert catalog._safe_browser_summary(_config())["status"] == "not_configured"
    browser = SimpleNamespace(
        backend_name="CloakBrowser",
        enabled=True,
        settings=SimpleNamespace(allow_all_domains=True),
        client=SimpleNamespace(_health=SimpleNamespace(status="binary_missing")),
    )
    summary = catalog._safe_browser_summary(_config(browser=browser))
    assert summary["optional_dependency"] == "cloakbrowser"
    assert summary["dependency_status"] == "binary_missing"

    broken = SimpleNamespace(status=lambda: (_ for _ in ()).throw(RuntimeError("offline")))
    assert catalog._safe_mcp_summary(_config(mcp=broken))[0]["state"] == "status_error"
    incomplete = SimpleNamespace(status=lambda: {"server": {"status": "ready"}})
    assert catalog._safe_mcp_summary(_config(mcp=incomplete))

    no_role = build_authoring_snapshot(_config(), phase_role="unknown", phase_capabilities=None)
    explicit = build_authoring_snapshot(_config(), phase_capabilities=())
    assert no_role.phase_capability_source == "role_default_unrestricted"
    assert explicit.phase_capability_source == "explicit_unrestricted"
    cache = AuthoringSnapshotCache(max_entries=1)
    first = cache.get_or_build(_config(), phase_name="one")
    second = cache.get_or_build(_config(), phase_name="two")
    assert first is not second
    cache.clear()
    assert cache._entries == {}


def test_draft_manifest_and_publication_boundaries(tmp_path: Path) -> None:
    draft = build_draft_path(tmp_path, "run-1", "demo")
    draft.mkdir(parents=True)
    (draft / "runner.py").write_text("print('one')\n", encoding="utf-8")
    (draft / "__pycache__").mkdir()
    (draft / "__pycache__" / "ignored.pyc").write_bytes(b"x")
    manifest = scan_draft(draft, root=tmp_path, run_id="run-1", workflow_name="demo")
    assert manifest.render().startswith("[DRAFT MANIFEST]")
    assert json.loads(json.dumps(manifest.to_dict()))["files"]
    record = publish_draft(manifest, root=tmp_path)
    assert record.to_dict()["status"] == "published"
    assert Path(record.published_path, "runner.py").exists()

    with pytest.raises(DraftError, match="exactly"):
        scan_draft(tmp_path / "missing", root=tmp_path, run_id="run-1", workflow_name="demo")
    with pytest.raises(DraftError, match="unsafe"):
        build_draft_path(tmp_path, "../bad", "demo")


def test_legacy_staging_accepts_file_and_package_and_rejects_invalid_inputs(tmp_path: Path) -> None:
    source_file = tmp_path / "legacy.py"
    source_file.write_text("VALUE = 1\n", encoding="utf-8")
    destination = tmp_path / ".agenthicc" / "workflows" / "staged"
    assert stage_legacy_package(source_file, destination=destination, root=tmp_path) == destination
    assert (destination / "runner.py").exists()

    source_package = tmp_path / "package"
    source_package.mkdir()
    (source_package / "runner.py").write_text("VALUE = 2\n", encoding="utf-8")
    assert (
        stage_legacy_package(source_package, destination=destination, root=tmp_path) == destination
    )
    assert (destination / "runner.py").read_text(encoding="utf-8") == "VALUE = 2\n"

    outside = Path("/tmp") / "not-in-workspace-agenthicc"
    with pytest.raises(DraftError, match="outside the workspace"):
        stage_legacy_package(outside, destination=destination, root=tmp_path)


@pytest.mark.asyncio
async def test_validator_and_inspection_tools_cover_unavailable_inputs(tmp_path: Path) -> None:
    path = tmp_path / "workflow.py"
    path.write_text("not valid python (", encoding="utf-8")
    report = validate_workflow_file(str(path), expected_name="workflow", root=tmp_path)
    assert not report.ok
    assert report.render()
    tools = {getattr(item, "__name__", ""): item for item in make_inspection_tools()}
    assert (await tools["validate_workflow_cache_contract"](""))["ok"] is False  # type: ignore[operator]


def test_name_that_ui_parser_cache_and_formatting_edges(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert name_that_ui.js_unescape(r"A\n\u0042\uZZZZ\q") == "A\nBuZZZZq"

    class Response:
        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return b"<html>"

    monkeypatch.setattr(
        name_that_ui.urllib.request, "urlopen", lambda *_args, **_kwargs: Response()
    )
    assert name_that_ui._fetch_html() == "<html>"

    invalid = r'<script>self.__next_f.push([1,"{\"slug\":}"])</script>'
    incomplete = r'<script>self.__next_f.push([1,"{\"slug\":\"x\""])</script>'
    monkeypatch.setattr(name_that_ui, "_fetch_html", lambda: invalid + incomplete)
    assert name_that_ui.fetch_catalog() == []

    records = [
        {
            "name": "Button",
            "platform": "web",
            "description": "A primary action",
            "aka": ["action control"],
            "fuzzy": ["cta"],
            "api": [{"framework": "ARIA", "symbol": "aria-label"}],
            "prompt": "  build it  ",
        },
        {"name": "Other", "platform": "macos", "api": []},
        {},
    ]
    assert name_that_ui.lookup("cta", records)
    assert name_that_ui.lookup("nothing", records) == []
    assert "aria-label" in name_that_ui.inventory_line(records[0])
    assert "-" in name_that_ui.format_matches([records[1]])
    assert name_that_ui.list_names(records, platform="web", query="cta", top=0)

    cache = tmp_path / "invalid-cache.json"
    cache.write_text("not json", encoding="utf-8")
    try:
        monkeypatch.setattr(name_that_ui, "fetch_catalog", lambda: [])
        assert name_that_ui.load_catalog(str(cache), ttl=60) == []
    finally:
        cache.unlink(missing_ok=True)


def test_name_that_ui_cache_write_failure_is_soft(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(name_that_ui, "fetch_catalog", lambda: [{"name": "Fresh"}])
    assert name_that_ui.load_catalog(str(tmp_path / "missing" / "cache.json")) == [
        {"name": "Fresh"}
    ]
