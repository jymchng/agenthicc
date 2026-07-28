"""Integration tests for the agenthicc docs/source inspection tools.

These wire real collaborators rather than stubs:

* the real tool registry and the system-prompt section it renders;
* the real :class:`~agenthicc.tools.capability_gate.ToolCapabilityGate` under a
  restricted mode;
* the real mode registry, to confirm the tools survive Plan mode;
* a synthetic *installed* layout, to confirm packaged-docs discovery works and
  matches what ``pyproject.toml`` actually ships.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from agenthicc.agent_tools import BUILTIN_GROUPS, INTROSPECT_AGENT_TOOLS
from agenthicc.plugins.registry import build_registry
from agenthicc.tools.capabilities import ToolCapability, get_tool_capabilities
from agenthicc.tools.capability_gate import ToolCapabilityGate
from agenthicc.tools.introspect import (
    DOCS_DIR_ENV,
    docs_root,
    extra_doc_names,
    list_docs,
    package_root,
    read_doc,
    resolve_source,
)
from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.runtime.mode_manager import RuntimeMode, build_default_registry

pytestmark = pytest.mark.integration

_TOOL_NAMES = frozenset(
    {
        "list_agenthicc_docs",
        "read_agenthicc_doc",
        "search_agenthicc_docs",
        "inspect_agenthicc_source",
        "search_agenthicc_source",
    }
)


def _clear_cache() -> None:
    from agenthicc.tools.introspect import _resolve_docs_root

    _resolve_docs_root.cache_clear()


# ── real tool registry ────────────────────────────────────────────────────────


def test_registry_exposes_every_inspection_tool() -> None:
    registry = build_registry(agent_name="auto", project_plugin_tools=[])
    assert _TOOL_NAMES <= set(registry.names)


def test_system_prompt_section_advertises_the_group() -> None:
    registry = build_registry(agent_name="auto", project_plugin_tools=[])
    described = registry.describe()
    assert "### agenthicc Docs & Source (5 tools)" in described
    for name in _TOOL_NAMES:
        assert f"**{name}**" in described


def test_group_is_rendered_after_the_primary_domains() -> None:
    registry = build_registry(agent_name="auto", project_plugin_tools=[])
    described = registry.describe()
    assert described.index("### File System") < described.index("### agenthicc Docs & Source")


def test_glob_expansion_resolves_the_group_namespace() -> None:
    registry = build_registry(agent_name="auto", project_plugin_tools=[])
    assert registry.glob_expand("introspect.*") == _TOOL_NAMES


def test_no_builtin_group_claims_the_same_tool_twice() -> None:
    seen: dict[str, str] = {}
    for group in BUILTIN_GROUPS:
        for tool in group.tools:
            name = getattr(tool, "__name__", "")
            assert name not in seen, f"{name} in both {seen.get(name)} and {group.name}"
            seen[name] = group.name


# ── real capability gate ──────────────────────────────────────────────────────


async def _gate_blocks(app_state: AppState, tool: object) -> bool:
    """Run the real capability gate for *tool* and report whether it aborted."""
    from lauren_ai import ToolCallContext

    from agenthicc.tools.capabilities import CAPABILITIES_KEY

    gate = ToolCapabilityGate(app_state)
    ctx = ToolCallContext(
        agent_context=None,
        tool_use_id="gate-1",
        turn=0,
        tool_name=getattr(tool, "__name__", ""),
        tool_input={},
        metadata={CAPABILITIES_KEY: frozenset(get_tool_capabilities(tool))},
    )
    decision = await gate.before_tool_call(ctx)
    return bool(getattr(decision, "_aborted", False))


async def test_inspection_tools_pass_the_gate_in_a_restricted_mode() -> None:
    """Plan mode blocks write/execute/network — reading agenthicc must still work."""
    app = AppState.create()
    registry = build_default_registry()
    plan = registry.get("Plan")
    assert plan is not None
    assert plan.blocked_capabilities  # the mode really is restricted
    app.active_mode.set(plan)

    for tool in INTROSPECT_AGENT_TOOLS:
        assert not await _gate_blocks(app, tool), getattr(tool, "__name__", "")


async def test_a_mode_blocking_reads_does_stop_them() -> None:
    """The tools are honestly tagged: blocking READ blocks all five."""
    app = AppState.create()
    app.active_mode.set(
        RuntimeMode(name="NoReads", blocked_capabilities=frozenset({ToolCapability.READ}))
    )
    for tool in INTROSPECT_AGENT_TOOLS:
        assert await _gate_blocks(app, tool), getattr(tool, "__name__", "")


# ── real documentation tree ───────────────────────────────────────────────────


def test_the_checkout_documentation_tree_is_discovered() -> None:
    root = docs_root()
    assert root is not None
    assert root.name == "docs"
    assert (root / "guides" / "workflows.md").is_file()


def test_every_listed_document_is_readable() -> None:
    for entry in list_docs():
        result = read_doc(entry.path, start_line=1, max_lines=5)
        assert result["ok"] is True, entry.path
        assert result["total_lines"] == entry.lines


def test_the_guides_and_reference_pages_are_all_published() -> None:
    published = {entry.path for entry in list_docs()}
    root = docs_root()
    assert root is not None
    for path in sorted(root.rglob("*.md")):
        assert path.relative_to(root).as_posix() in published


def test_the_llm_documentation_is_published_and_readable() -> None:
    published = {entry.path for entry in list_docs()}
    assert {"llms.txt", "llms-full.txt", "README.md"} <= published
    result = read_doc("llms-full.txt", start_line=1, max_lines=3)
    assert result["ok"] is True
    assert result["total_lines"] > 100


# ── installed-distribution layout ─────────────────────────────────────────────


def test_packaged_docs_layout_is_discovered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wheel puts everything flat under <prefix>/share/agenthicc/docs."""
    prefix = tmp_path / "venv"
    packaged = prefix / "share" / "agenthicc" / "docs"
    (packaged / "guides").mkdir(parents=True)
    (packaged / "index.md").write_text("# Index\n", encoding="utf-8")
    (packaged / "guides" / "tools.md").write_text("# Tools\n", encoding="utf-8")
    for name in extra_doc_names():
        (packaged / name).write_text(f"# {name}\n", encoding="utf-8")

    monkeypatch.delenv(DOCS_DIR_ENV, raising=False)
    monkeypatch.setattr("agenthicc.tools.introspect.sys.prefix", str(prefix))
    monkeypatch.setattr(
        "agenthicc.tools.introspect.package_root",
        lambda: tmp_path / "site-packages" / "agenthicc",
    )
    _clear_cache()
    try:
        assert docs_root() == packaged.resolve()
        published = {entry.path for entry in list_docs()}
        assert {"index.md", "guides/tools.md", *extra_doc_names()} <= published
        assert read_doc("llms-full.txt")["ok"] is True
    finally:
        _clear_cache()


def test_pyproject_ships_every_top_level_document() -> None:
    """The extra doc names must actually be in the wheel, not just the checkout."""
    pyproject = package_root().parents[1] / "pyproject.toml"
    if not pyproject.is_file():  # installed without the checkout
        pytest.skip("pyproject.toml is not available in this installation")
    data_files = tomllib.loads(pyproject.read_text(encoding="utf-8"))["tool"]["setuptools"][
        "data-files"
    ]
    shipped = set(data_files["share/agenthicc/docs"])
    assert set(extra_doc_names()) <= shipped


def test_pyproject_ships_the_guides_and_reference_trees() -> None:
    pyproject = package_root().parents[1] / "pyproject.toml"
    if not pyproject.is_file():
        pytest.skip("pyproject.toml is not available in this installation")
    data_files = tomllib.loads(pyproject.read_text(encoding="utf-8"))["tool"]["setuptools"][
        "data-files"
    ]
    assert "docs/guides/*.md" in data_files["share/agenthicc/docs/guides"]
    assert "docs/reference/*.md" in data_files["share/agenthicc/docs/reference"]


# ── source inspection against the real package ────────────────────────────────


def test_documented_kernel_symbols_are_all_inspectable() -> None:
    """Every symbol the kernel exports publicly must resolve through the tool."""
    import agenthicc.kernel as kernel

    for name in kernel.__all__:
        result = resolve_source(f"agenthicc.kernel:{name}", include_source=False)
        if result["ok"]:
            continue
        # Re-exported from a submodule — resolve it where it is defined.
        found = any(
            resolve_source(f"agenthicc.kernel.{module}:{name}", include_source=False)["ok"]
            for module in ("state", "events", "reducer", "processor")
        )
        assert found, name


def test_inspecting_the_inspection_tools_themselves_works() -> None:
    result = resolve_source(
        "agenthicc.tools.introspect.agent_tools:inspect_agenthicc_source",
        include_source=True,
    )
    assert result["ok"] is True
    assert "resolve_source" in str(result["source"])


def test_source_resolution_agrees_with_the_real_file_layout() -> None:
    for module, expected in (
        ("agenthicc.kernel.reducer", "agenthicc/kernel/reducer.py"),
        ("agenthicc.workflows", "agenthicc/workflows/__init__.py"),
        ("agenthicc.tools.introspect", "agenthicc/tools/introspect/__init__.py"),
    ):
        result = resolve_source(module, include_source=False)
        assert result["ok"] is True
        assert result["path"] == expected
        assert (package_root().parent / expected).is_file()
