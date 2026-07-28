"""Unit tests for the agenthicc docs/source self-inspection tools.

Covers documentation-root resolution, the bounded listing/reading/searching
contract, AST-based source resolution without imports, and every containment
refusal that keeps these tools from reading outside their two roots.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from agenthicc.agent_tools import (
    AGENT_TOOLS,
    BUILTIN_GROUPS,
    INTROSPECT_AGENT_TOOLS,
    INTROSPECT_GROUP,
    inspect_agenthicc_source,
    list_agenthicc_docs,
    read_agenthicc_doc,
    search_agenthicc_docs,
    search_agenthicc_source,
)
from agenthicc.tools.capabilities import ToolCapability, get_tool_capabilities
from agenthicc.tools.introspect import (
    DOCS_DIR_ENV,
    MAX_DOC_LINES,
    MAX_SEARCH_RESULTS,
    DocEntry,
    SearchHit,
    docs_root,
    docs_unavailable_result,
    extra_doc_names,
    find_doc,
    list_docs,
    package_root,
    read_doc,
    resolve_source,
    search_docs,
    search_source,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def fake_docs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point documentation discovery at a small synthetic tree."""
    root = tmp_path / "repo" / "docs"
    (root / "guides").mkdir(parents=True)
    (root / "index.md").write_text("# Index\n\nWelcome.\n", encoding="utf-8")
    (root / "guides" / "tools.md").write_text(
        "# Tools\n\nline two\nthe capability gate blocks writes\n", encoding="utf-8"
    )
    (root / "notes.txt").write_text("plain notes\n", encoding="utf-8")
    (root / "ignored.png").write_bytes(b"\x89PNG")
    # Top-level documents live beside docs/ in a checkout.
    (tmp_path / "repo" / "llms.txt").write_text("# agenthicc\n\nshort form\n", encoding="utf-8")
    (tmp_path / "repo" / "secret.md").write_text("not published\n", encoding="utf-8")
    monkeypatch.setenv(DOCS_DIR_ENV, str(root))
    _clear_cache()
    yield root
    monkeypatch.delenv(DOCS_DIR_ENV, raising=False)
    _clear_cache()


def _clear_cache() -> None:
    """Drop the memoised documentation-root lookup between tests."""
    from agenthicc.tools.introspect import _resolve_docs_root

    _resolve_docs_root.cache_clear()


@pytest.fixture
def no_docs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point documentation discovery at a directory with no index.md."""
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv(DOCS_DIR_ENV, str(empty))
    monkeypatch.setattr(
        "agenthicc.tools.introspect._candidate_doc_roots",
        lambda: [empty],
    )
    _clear_cache()
    yield empty
    monkeypatch.delenv(DOCS_DIR_ENV, raising=False)
    _clear_cache()


# ── dataclasses ───────────────────────────────────────────────────────────────


def test_doc_entry_serialises_to_the_tool_shape() -> None:
    entry = DocEntry(path="guides/tools.md", title="Tools", lines=10, bytes=200)
    assert entry.as_dict() == {
        "path": "guides/tools.md",
        "title": "Tools",
        "lines": 10,
        "bytes": 200,
    }


def test_search_hit_serialises_to_the_tool_shape() -> None:
    assert SearchHit(path="a.md", line=3, text="hi").as_dict() == {
        "path": "a.md",
        "line": 3,
        "text": "hi",
    }


# ── root resolution ───────────────────────────────────────────────────────────


def test_package_root_is_the_installed_agenthicc_package() -> None:
    root = package_root()
    assert root.name == "agenthicc"
    assert (root / "__init__.py").is_file()
    assert (root / "kernel" / "reducer.py").is_file()


def test_docs_root_resolves_the_repository_checkout() -> None:
    root = docs_root()
    assert root is not None
    assert (root / "index.md").is_file()


def test_docs_root_honours_the_environment_override(fake_docs: Path) -> None:
    assert docs_root() == fake_docs.resolve()


def test_docs_root_is_none_without_a_marker_file(no_docs: Path) -> None:
    assert docs_root() is None


def test_docs_unavailable_result_names_the_override_and_the_fallback() -> None:
    result = docs_unavailable_result()
    assert result["ok"] is False
    assert DOCS_DIR_ENV in str(result["fix"])
    assert "inspect_agenthicc_source" in str(result["fix"])


def test_extra_doc_names_cover_the_llm_facing_documents() -> None:
    assert set(extra_doc_names()) == {"llms.txt", "llms-full.txt", "README.md"}


# ── listing ───────────────────────────────────────────────────────────────────


def test_list_docs_covers_the_tree_and_the_top_level_documents(fake_docs: Path) -> None:
    paths = [entry.path for entry in list_docs()]
    assert "llms.txt" in paths
    assert "index.md" in paths
    assert "guides/tools.md" in paths
    assert "notes.txt" in paths
    assert "ignored.png" not in paths  # unsupported suffix
    assert "secret.md" not in paths  # not an allowed top-level document


def test_list_docs_reads_titles_and_sizes(fake_docs: Path) -> None:
    entry = next(e for e in list_docs() if e.path == "guides/tools.md")
    assert entry.title == "Tools"
    assert entry.lines == 4
    assert entry.bytes > 0


def test_list_docs_falls_back_to_the_filename_without_a_heading(fake_docs: Path) -> None:
    entry = next(e for e in list_docs() if e.path == "notes.txt")
    assert entry.title == "notes.txt"


def test_list_docs_filters_by_section(fake_docs: Path) -> None:
    assert [e.path for e in list_docs("guides")] == ["guides/tools.md"]
    assert [e.path for e in list_docs("llms")] == ["llms.txt"]
    assert list_docs("nothing-here") == []


def test_list_docs_is_empty_without_a_documentation_tree(no_docs: Path) -> None:
    assert list_docs() == []


def test_list_docs_on_the_real_tree_finds_the_guides_and_reference() -> None:
    paths = {entry.path for entry in list_docs()}
    assert "guides/workflows.md" in paths
    assert "reference/kernel.md" in paths
    assert "llms-full.txt" in paths


# ── path containment ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path",
    [
        "../secret.md",
        "guides/../../secret.md",
        "/etc/passwd",
        "",
        "   ",
        "ignored.png",
        "missing.md",
        "guides",
    ],
)
def test_find_doc_refuses_anything_outside_the_published_surface(
    fake_docs: Path, path: str
) -> None:
    assert find_doc(path) is None


def test_find_doc_resolves_published_paths(fake_docs: Path) -> None:
    assert find_doc("guides/tools.md") == (fake_docs / "guides" / "tools.md").resolve()
    assert find_doc("/index.md") == (fake_docs / "index.md").resolve()
    assert find_doc("guides\\tools.md") == (fake_docs / "guides" / "tools.md").resolve()


def test_find_doc_serves_top_level_documents_from_beside_the_tree(fake_docs: Path) -> None:
    resolved = find_doc("llms.txt")
    assert resolved is not None
    assert resolved.name == "llms.txt"
    assert resolved.parent == fake_docs.parent


def test_find_doc_is_none_without_a_documentation_tree(no_docs: Path) -> None:
    assert find_doc("index.md") is None


# ── reading ───────────────────────────────────────────────────────────────────


def test_read_doc_returns_a_bounded_window(fake_docs: Path) -> None:
    result = read_doc("guides/tools.md", start_line=1, max_lines=2)
    assert result["ok"] is True
    assert result["title"] == "Tools"
    assert result["total_lines"] == 4
    assert result["start_line"] == 1
    assert result["end_line"] == 2
    assert result["truncated"] is True
    assert result["next_start_line"] == 3
    assert result["content"] == "# Tools\n"


def test_read_doc_pages_to_the_end(fake_docs: Path) -> None:
    first = read_doc("guides/tools.md", start_line=1, max_lines=2)
    second = read_doc("guides/tools.md", start_line=int(first["next_start_line"]), max_lines=2)
    assert second["truncated"] is False
    assert second["next_start_line"] == 0
    assert "capability gate" in str(second["content"])


def test_read_doc_clamps_the_window_to_the_hard_cap(fake_docs: Path) -> None:
    result = read_doc("index.md", start_line=1, max_lines=10 * MAX_DOC_LINES)
    assert result["ok"] is True
    assert len(str(result["content"]).splitlines()) <= MAX_DOC_LINES


def test_read_doc_clamps_a_non_positive_start(fake_docs: Path) -> None:
    result = read_doc("index.md", start_line=-5)
    assert result["start_line"] == 1


def test_read_doc_past_the_end_returns_an_empty_window(fake_docs: Path) -> None:
    result = read_doc("index.md", start_line=9999)
    assert result["ok"] is True
    assert result["content"] == ""
    assert result["truncated"] is False


def test_read_doc_refuses_an_unpublished_path(fake_docs: Path) -> None:
    result = read_doc("../secret.md")
    assert result["ok"] is False
    assert "list_agenthicc_docs()" in str(result["fix"])


def test_read_doc_reports_a_missing_documentation_tree(no_docs: Path) -> None:
    assert read_doc("index.md")["ok"] is False


# ── searching documentation ───────────────────────────────────────────────────


def test_search_docs_finds_a_plain_phrase(fake_docs: Path) -> None:
    result = search_docs("capability gate")
    assert result["ok"] is True
    matches = result["matches"]
    assert isinstance(matches, list)
    assert matches[0]["path"] == "guides/tools.md"
    assert matches[0]["line"] == 4
    assert result["files_scanned"] >= 1


def test_search_docs_accepts_a_regular_expression(fake_docs: Path) -> None:
    result = search_docs(r"^# \w+")
    paths = {match["path"] for match in result["matches"]}  # type: ignore[union-attr]
    assert {"index.md", "guides/tools.md"} <= paths


def test_search_docs_falls_back_to_substring_for_invalid_regex(fake_docs: Path) -> None:
    result = search_docs("gate (unclosed")
    assert result["ok"] is True
    assert result["match_count"] == 0


def test_search_docs_is_case_insensitive(fake_docs: Path) -> None:
    assert search_docs("CAPABILITY GATE")["match_count"] == 1


def test_search_docs_caps_and_flags_truncation(fake_docs: Path) -> None:
    result = search_docs(".", max_results=2)
    assert result["match_count"] == 2
    assert result["truncated"] is True


def test_search_docs_clamps_max_results_to_the_hard_cap(fake_docs: Path) -> None:
    result = search_docs(".", max_results=10 * MAX_SEARCH_RESULTS)
    assert result["match_count"] <= MAX_SEARCH_RESULTS


def test_search_docs_can_be_restricted_to_a_section(fake_docs: Path) -> None:
    result = search_docs("#", section="guides")
    assert {match["path"] for match in result["matches"]} == {"guides/tools.md"}  # type: ignore[union-attr]


def test_search_docs_rejects_an_empty_query(fake_docs: Path) -> None:
    result = search_docs("   ")
    assert result["ok"] is False
    assert "non-empty query" in str(result["error"])


def test_search_docs_reports_a_missing_documentation_tree(no_docs: Path) -> None:
    assert search_docs("anything")["ok"] is False


# ── source resolution ─────────────────────────────────────────────────────────


def test_resolve_source_reads_a_module_with_an_outline() -> None:
    result = resolve_source("agenthicc.kernel.reducer", include_source=False)
    assert result["ok"] is True
    assert result["kind"] == "module"
    assert result["path"] == "agenthicc/kernel/reducer.py"
    assert result["docstring"]
    assert "source" not in result
    names = {member["name"] for member in result["members"]}  # type: ignore[union-attr]
    assert "root_reducer" in names


def test_resolve_source_includes_bounded_module_source() -> None:
    result = resolve_source("agenthicc.workflows.base_runner")
    assert result["ok"] is True
    assert "class BaseWorkflowRunner" in str(result["source"])
    assert result["truncated"] is False


def test_resolve_source_reads_a_function_with_decorators() -> None:
    result = resolve_source("agenthicc.kernel.reducer:root_reducer")
    assert result["ok"] is True
    assert result["kind"] == "function"
    assert result["signature"].startswith("def root_reducer(")  # type: ignore[union-attr]
    assert "def root_reducer" in str(result["source"])
    assert result["lineno"] > 1
    assert result["end_lineno"] >= result["lineno"]  # type: ignore[operator]


def test_resolve_source_reads_a_class_and_lists_its_members() -> None:
    result = resolve_source("agenthicc.workflows.plugin:PhaseSpec", include_source=False)
    assert result["ok"] is True
    assert result["kind"] == "class"
    assert result["signature"] == "class PhaseSpec"
    names = [member["name"] for member in result["members"]]  # type: ignore[union-attr]
    assert "name" in names
    assert "max_turns" in names


def test_resolve_source_reads_a_method_through_its_class() -> None:
    result = resolve_source("agenthicc.workflows.plugin:WorkflowPlugin.build_runner")
    assert result["ok"] is True
    assert result["kind"] == "function"
    assert "build_runner" in str(result["signature"])
    assert "@classmethod" in str(result["source"])


def test_resolve_source_accepts_the_all_dots_form() -> None:
    colon = resolve_source("agenthicc.kernel.reducer:root_reducer", include_source=False)
    dots = resolve_source("agenthicc.kernel.reducer.root_reducer", include_source=False)
    assert dots["ok"] is True
    assert dots["lineno"] == colon["lineno"]
    assert dots["module"] == "agenthicc.kernel.reducer"
    assert dots["symbol"] == "root_reducer"


def test_resolve_source_reaches_private_symbols() -> None:
    result = resolve_source("agenthicc.workflows.plugin:_parse_output_schema")
    assert result["ok"] is True
    assert result["kind"] == "function"


def test_resolve_source_reads_a_package_init() -> None:
    result = resolve_source("agenthicc.workflows", include_source=False)
    assert result["ok"] is True
    assert result["path"] == "agenthicc/workflows/__init__.py"


def test_resolve_source_reads_module_level_variables() -> None:
    result = resolve_source("agenthicc.agent_tools:BUILTIN_GROUPS", include_source=False)
    assert result["ok"] is True
    assert result["kind"] == "variable"


@pytest.mark.parametrize(
    ("target", "fragment"),
    [
        ("", "A target is required"),
        ("   ", "A target is required"),
        ("os.path", "not part of the agenthicc package"),
        ("json", "not part of the agenthicc package"),
        ("agenthicc.kernel.reducer:nope", "no symbol named"),
        ("agenthicc.does.not.exist", "no symbol named"),
        ("agenthicc/../../etc/passwd", "No agenthicc module matches"),
    ],
)
def test_resolve_source_refuses_bad_targets(target: str, fragment: str) -> None:
    result = resolve_source(target)
    assert result["ok"] is False
    assert fragment in str(result["error"])
    assert result["fix"]


def test_resolve_source_never_imports_the_target_module() -> None:
    """Path-and-AST resolution keeps modules with optional deps inspectable."""
    import sys

    module = "agenthicc.tools.outlook.win32_backend"
    sys.modules.pop(module, None)
    result = resolve_source(module, include_source=False)
    assert result["ok"] is True
    assert module not in sys.modules


def test_resolve_source_missing_symbol_lists_available_names() -> None:
    result = resolve_source("agenthicc.workflows.base_runner:Nope")
    assert result["ok"] is False
    assert "BaseWorkflowRunner" in str(result["fix"])


def test_resolve_source_source_matches_the_file_lines() -> None:
    result = resolve_source("agenthicc.workflows.base_runner:BaseWorkflowRunner")
    path = package_root().parent / str(result["path"])
    lines = path.read_text(encoding="utf-8").splitlines()
    start = int(result["lineno"])
    end = int(result["end_lineno"])
    assert str(result["source"]) == "\n".join(lines[start - 1 : end])


def test_every_module_in_the_package_parses_through_the_resolver() -> None:
    """The outline path must work for every shipped module, not just easy ones."""
    root = package_root()
    checked = 0
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).with_suffix("")
        parts = [part for part in rel.parts if part != "__init__"]
        module = ".".join(["agenthicc", *parts])
        result = resolve_source(module, include_source=False)
        assert result["ok"] is True, f"{module}: {result.get('error')}"
        checked += 1
    assert checked > 100


# ── searching source ──────────────────────────────────────────────────────────


def test_search_source_finds_a_definition() -> None:
    result = search_source(r"^def root_reducer", max_results=5)
    assert result["ok"] is True
    matches = result["matches"]
    assert isinstance(matches, list)
    assert matches[0]["path"] == "agenthicc/kernel/reducer.py"


def test_search_source_can_be_restricted_to_a_subpackage() -> None:
    result = search_source("class ", max_results=50, module="agenthicc.kernel")
    paths = {match["path"] for match in result["matches"]}  # type: ignore[union-attr]
    assert paths
    assert all(path.startswith("agenthicc/kernel/") for path in paths)


def test_search_source_can_be_restricted_to_one_module() -> None:
    result = search_source("def ", max_results=50, module="agenthicc.workflows.base_runner")
    paths = {match["path"] for match in result["matches"]}  # type: ignore[union-attr]
    assert paths == {"agenthicc/workflows/base_runner.py"}


def test_search_source_rejects_an_unknown_module_filter() -> None:
    result = search_source("def ", module="agenthicc.not_a_module")
    assert result["ok"] is False
    assert "No agenthicc module or subpackage matches" in str(result["error"])


def test_search_source_rejects_a_non_agenthicc_module_filter() -> None:
    assert search_source("def ", module="os")["ok"] is False


def test_search_source_rejects_an_empty_query() -> None:
    result = search_source("  ")
    assert result["ok"] is False
    assert "non-empty query" in str(result["error"])


def test_search_source_caps_results() -> None:
    result = search_source("e", max_results=3)
    assert result["match_count"] == 3
    assert result["truncated"] is True


def test_search_source_clips_very_long_lines() -> None:
    result = search_source("e", max_results=MAX_SEARCH_RESULTS)
    assert all(len(str(match["text"])) <= 401 for match in result["matches"])  # type: ignore[union-attr]


# ── tool wrappers ─────────────────────────────────────────────────────────────


def test_the_five_tools_are_exported_in_order() -> None:
    assert [getattr(tool, "__name__", "") for tool in INTROSPECT_AGENT_TOOLS] == [
        "list_agenthicc_docs",
        "read_agenthicc_doc",
        "search_agenthicc_docs",
        "inspect_agenthicc_source",
        "search_agenthicc_source",
    ]


def test_tools_are_read_only_so_they_survive_restricted_modes() -> None:
    write_like = {
        ToolCapability.WRITE,
        ToolCapability.EXECUTE,
        ToolCapability.NETWORK,
        ToolCapability.GIT_WRITE,
    }
    for tool in INTROSPECT_AGENT_TOOLS:
        caps = get_tool_capabilities(tool)
        assert caps, getattr(tool, "__name__", "")
        assert ToolCapability.READ in caps
        assert not (caps & write_like)


def test_search_tools_declare_the_search_capability() -> None:
    assert ToolCapability.SEARCH in get_tool_capabilities(search_agenthicc_docs)
    assert ToolCapability.SEARCH in get_tool_capabilities(search_agenthicc_source)
    assert ToolCapability.SEARCH not in get_tool_capabilities(read_agenthicc_doc)


def test_the_group_is_registered_among_the_builtins() -> None:
    assert INTROSPECT_GROUP in BUILTIN_GROUPS
    assert INTROSPECT_GROUP.name == "introspect"
    assert INTROSPECT_GROUP.label
    assert INTROSPECT_GROUP.description
    assert len(INTROSPECT_GROUP.tools) == 5


def test_group_names_stay_unique_across_the_builtins() -> None:
    names = [group.name for group in BUILTIN_GROUPS]
    assert len(names) == len(set(names))


def test_the_tools_are_part_of_agent_tools() -> None:
    names = {getattr(tool, "__name__", "") for tool in AGENT_TOOLS}
    assert {
        "list_agenthicc_docs",
        "read_agenthicc_doc",
        "search_agenthicc_docs",
        "inspect_agenthicc_source",
        "search_agenthicc_source",
    } <= names


def test_every_tool_has_a_model_facing_docstring() -> None:
    for tool in INTROSPECT_AGENT_TOOLS:
        doc = (tool.__doc__ or "").strip()
        assert doc, getattr(tool, "__name__", "")
        assert doc.splitlines()[0].endswith("."), getattr(tool, "__name__", "")


async def test_list_tool_returns_the_document_index() -> None:
    result = await list_agenthicc_docs()
    assert result["ok"] is True
    assert result["count"] == len(result["documents"])  # type: ignore[arg-type]
    paths = {doc["path"] for doc in result["documents"]}  # type: ignore[union-attr]
    assert "llms-full.txt" in paths


async def test_list_tool_filters_by_section() -> None:
    result = await list_agenthicc_docs("guides")
    assert result["ok"] is True
    assert all(str(doc["path"]).startswith("guides/") for doc in result["documents"])  # type: ignore[union-attr]


async def test_list_tool_reports_a_missing_tree(no_docs: Path) -> None:
    result = await list_agenthicc_docs()
    assert result["ok"] is False
    assert DOCS_DIR_ENV in str(result["fix"])


async def test_read_tool_reads_a_real_guide() -> None:
    result = await read_agenthicc_doc("guides/workflows.md", 1, 3)
    assert result["ok"] is True
    assert result["title"] == "Workflows"
    assert len(str(result["content"]).splitlines()) <= 3


async def test_search_docs_tool_finds_a_real_phrase() -> None:
    result = await search_agenthicc_docs("PhaseSpec", 5)
    assert result["ok"] is True
    assert result["match_count"] >= 1


async def test_inspect_source_tool_reads_a_real_symbol() -> None:
    result = await inspect_agenthicc_source("agenthicc.workflows.plugin:PhaseSpec", False)
    assert result["ok"] is True
    assert result["kind"] == "class"


async def test_search_source_tool_finds_a_real_symbol() -> None:
    result = await search_agenthicc_source("class ToolCapability", 3)
    assert result["ok"] is True
    assert result["match_count"] >= 1


async def test_tool_results_are_json_serialisable() -> None:
    import json

    for result in (
        await list_agenthicc_docs("reference"),
        await read_agenthicc_doc("index.md", 1, 5),
        await search_agenthicc_docs("workflow", 3),
        await inspect_agenthicc_source("agenthicc.workflows.base_runner", True),
        await search_agenthicc_source("def ", 3),
    ):
        json.dumps(result)


def test_the_tool_module_avoids_the_future_import() -> None:
    """@tool() needs real annotations, so the wrappers must not defer them."""
    source = (package_root() / "tools" / "introspect" / "agent_tools.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    deferred = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module == "__future__"
        and any(alias.name == "annotations" for alias in node.names)
    ]
    assert deferred == []
