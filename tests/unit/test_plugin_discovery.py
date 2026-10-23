"""Unit tests for PRD-24: Tool Plugin Discovery."""

from __future__ import annotations


import pytest

from agenthicc.plugins.deps import declared_dependencies, missing_requirements
from agenthicc.plugins.discovery import (
    LoadResult,
    PluginToolSet,
    _infer_missing_from_ast,
    _load_plugin_file,
    _scan_directory,
    discover_agent_tools,
    discover_project_tools,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# PRD-24 core tests (from the PRD "Tests" section)
# ---------------------------------------------------------------------------


def test_load_valid_plugin(tmp_path):
    f = tmp_path / "my_tools.py"
    f.write_text(
        "from lauren_ai._tools import tool\n"
        "@tool()\nasync def ping() -> str:\n    return 'pong'\n"
        "TOOLS = [ping]\n"
    )
    result = _load_plugin_file(f)
    assert result.ok
    assert len(result.tools) == 1
    assert result.tools[0].__name__ == "ping"


def test_load_plugin_syntax_error(tmp_path):
    f = tmp_path / "broken.py"
    f.write_text("def bad syntax!!!")
    result = _load_plugin_file(f)
    assert not result.ok
    assert "SyntaxError" in result.error


def test_load_plugin_no_tools_export(tmp_path):
    f = tmp_path / "no_export.py"
    f.write_text("x = 42\n")
    result = _load_plugin_file(f)
    assert result.ok
    assert result.tools == []


def test_scan_directory_skips_private(tmp_path):
    (tmp_path / "__init__.py").write_text("")
    (tmp_path / "_helper.py").write_text("TOOLS = []\n")
    (tmp_path / "tools.py").write_text(
        "from lauren_ai._tools import tool\n@tool()\nasync def t() -> None: pass\nTOOLS = [t]\n"
    )
    results = _scan_directory(tmp_path)
    loaded_names = [r.path.name for r in results if r.tools]
    assert "tools.py" in loaded_names
    assert "__init__.py" not in loaded_names
    assert "_helper.py" not in loaded_names


def test_scan_missing_directory_returns_empty(tmp_path):
    results = _scan_directory(tmp_path / "nonexistent")
    assert results == []


def test_load_plugin_reports_missing_declared_dependency(tmp_path):
    f = tmp_path / "needs_dep.py"
    f.write_text(
        "DEPENDENCIES = ['this-package-does-not-exist-xyz']\n"
        "from lauren_ai._tools import tool\n"
        "@tool()\nasync def t() -> None: pass\nTOOLS = [t]\n"
    )
    result = _load_plugin_file(f)
    assert not result.ok
    assert result.missing_deps  # surfaced, not a generic error


def test_load_plugin_importerror_triggers_ast_scan(tmp_path):
    f = tmp_path / "undeclared.py"
    f.write_text(
        "import this_package_does_not_exist_xyz\n"
        "from lauren_ai._tools import tool\n"
        "@tool()\nasync def t() -> None: pass\nTOOLS = [t]\n"
    )
    result = _load_plugin_file(f)
    assert not result.ok
    # AST scan should have caught "this_package_does_not_exist_xyz"
    assert "this_package_does_not_exist_xyz" in result.missing_deps


def test_sidecar_requirements_txt(tmp_path):
    req = tmp_path / "my_tools.requirements.txt"
    req.write_text("this-package-does-not-exist-xyz\n")
    f = tmp_path / "my_tools.py"
    f.write_text("TOOLS = []\n")
    result = _load_plugin_file(f)
    assert not result.ok
    assert result.missing_deps


# ---------------------------------------------------------------------------
# Additional dep-checking tests
# ---------------------------------------------------------------------------


def test_missing_requirements_returns_only_absent_packages():
    """missing_requirements reports distributions that are not installed."""
    # "pytest" is definitely installed in this environment
    # "this-pkg-does-not-exist-agenthicc-test" is definitely not
    present = ["pytest>=7"]
    absent = ["this-pkg-does-not-exist-agenthicc-test"]
    missing = missing_requirements(present + absent)
    assert missing == absent


def test_declared_dependencies_falls_back_to_the_sidecar(tmp_path):
    """The sidecar is parsed when DEPENDENCIES is absent; comments and blanks are skipped."""
    py_file = tmp_path / "myplugin.py"
    py_file.write_text("TOOLS = []\n")
    req_file = tmp_path / "myplugin.requirements.txt"
    req_file.write_text("# this is a comment\n\nhttpx>=0.27\nrequests\n")
    assert declared_dependencies(py_file) == ["httpx>=0.27", "requests"]


def test_declared_dependencies_prefers_the_literal(tmp_path):
    py_file = tmp_path / "myplugin.py"
    py_file.write_text("DEPENDENCIES: list[str] = ['pyyaml']\n")
    (tmp_path / "myplugin.requirements.txt").write_text("requests\n")
    assert declared_dependencies(py_file) == ["pyyaml"]


def test_declared_dependencies_without_a_declaration_is_empty(tmp_path):
    py_file = tmp_path / "noreqs.py"
    py_file.write_text("TOOLS = []\n")
    assert declared_dependencies(py_file) == []


def test_infer_missing_from_ast_skips_stdlib_and_installed(tmp_path):
    """_infer_missing_from_ast does not flag stdlib or installed packages."""
    f = tmp_path / "check_imports.py"
    # "os" and "sys" are stdlib; "pytest" is installed; the made-up name is not
    f.write_text(
        "import os\nimport sys\nimport pytest\nimport this_totally_made_up_pkg_xyz_agenthicc\n"
    )
    missing = _infer_missing_from_ast(f)
    assert "os" not in missing
    assert "sys" not in missing
    assert "pytest" not in missing
    assert "this_totally_made_up_pkg_xyz_agenthicc" in missing


def test_plugin_tool_set_aggregates_tools(tmp_path):
    """PluginToolSet.all_tools flattens tools across all LoadResults."""

    def tool_a(): ...
    def tool_b(): ...
    def tool_c(): ...

    r1 = LoadResult(path=tmp_path / "a.py", tools=[tool_a, tool_b])
    r2 = LoadResult(path=tmp_path / "b.py", tools=[tool_c])
    r3 = LoadResult(path=tmp_path / "c.py", error="boom")

    ts = PluginToolSet(results=[r1, r2, r3])
    assert ts.all_tools == [tool_a, tool_b, tool_c]
    assert ts.failed == [r3]


# ---------------------------------------------------------------------------
# discover_project_tools / discover_agent_tools smoke tests
# ---------------------------------------------------------------------------


def test_discover_project_tools_empty_dirs(tmp_path):
    """discover_project_tools on empty dirs returns empty PluginToolSet."""
    ts = discover_project_tools(project_dir=tmp_path, user_dir=tmp_path)
    assert isinstance(ts, PluginToolSet)
    assert ts.all_tools == []


def test_discover_agent_tools_empty_dirs(tmp_path):
    """discover_agent_tools on empty dirs returns empty PluginToolSet."""
    ts = discover_agent_tools("myagent", project_dir=tmp_path, user_dir=tmp_path)
    assert isinstance(ts, PluginToolSet)
    assert ts.all_tools == []


def test_discover_project_tools_loads_valid_file(tmp_path):
    """discover_project_tools finds and loads a valid tool file."""
    tools_dir = tmp_path / ".agenthicc" / "tools"
    tools_dir.mkdir(parents=True)
    (tools_dir / "greet.py").write_text(
        "from lauren_ai._tools import tool\n"
        "@tool()\nasync def greet() -> str:\n    return 'hi'\n"
        "TOOLS = [greet]\n"
    )
    ts = discover_project_tools(project_dir=tmp_path / ".agenthicc", user_dir=tmp_path)
    assert len(ts.all_tools) == 1
    assert ts.all_tools[0].__name__ == "greet"


def test_scan_directory_continues_after_unexpected_plugin_failure(tmp_path, monkeypatch):
    """One loader exception must not prevent later plugin files from loading."""
    from agenthicc.plugins import discovery

    first = tmp_path / "01_broken.py"
    second = tmp_path / "02_usable.py"
    first.write_text("# broken\n")
    second.write_text("# usable\n")

    def load(path):
        if path == first:
            raise RuntimeError("loader exploded")
        return LoadResult(path=path, tools=[lambda: None])

    monkeypatch.setattr(discovery, "_load_plugin_file", load)

    results = discovery._scan_directory(tmp_path)

    assert len(results) == 2
    assert results[0].error == "RuntimeError: loader exploded"
    assert len(results[1].tools) == 1


def test_discover_agent_tools_loads_valid_file(tmp_path):
    """discover_agent_tools finds tools scoped to the named agent."""
    agent_tools_dir = tmp_path / ".agenthicc" / "agents" / "writer" / "tools"
    agent_tools_dir.mkdir(parents=True)
    (agent_tools_dir / "spell.py").write_text(
        "from lauren_ai._tools import tool\n"
        "@tool()\nasync def check_spelling() -> bool:\n    return True\n"
        "TOOLS = [check_spelling]\n"
    )
    ts = discover_agent_tools(
        "writer",
        project_dir=tmp_path / ".agenthicc",
        user_dir=tmp_path,
    )
    assert len(ts.all_tools) == 1
    assert ts.all_tools[0].__name__ == "check_spelling"


# ---------------------------------------------------------------------------
# PRD-250 D-05: plugins execute exactly once; dependencies are read statically
# ---------------------------------------------------------------------------


_COUNTING_TOOL = """
import os
from pathlib import Path

counter = Path(os.environ["AGENTHICC_TEST_COUNTER"])
counter.write_text(counter.read_text() + "x")

DEPENDENCIES = []

from lauren_ai._tools import tool


@tool()
async def counted() -> None:
    pass


TOOLS = [counted]
"""


def test_a_tool_plugin_module_executes_exactly_once(tmp_path, monkeypatch):
    counter = tmp_path / "counter"
    counter.write_text("")
    monkeypatch.setenv("AGENTHICC_TEST_COUNTER", str(counter))
    plugin = tmp_path / "counting.py"
    plugin.write_text(_COUNTING_TOOL)

    result = _load_plugin_file(plugin)

    assert result.ok and len(result.tools) == 1
    assert counter.read_text() == "x"


def test_a_plugin_with_missing_dependencies_is_never_executed(tmp_path, monkeypatch):
    counter = tmp_path / "counter"
    counter.write_text("")
    monkeypatch.setenv("AGENTHICC_TEST_COUNTER", str(counter))
    plugin = tmp_path / "needs_dep.py"
    plugin.write_text(
        _COUNTING_TOOL.replace("DEPENDENCIES = []", "DEPENDENCIES = ['no-such-dist-xyz>=1']")
    )

    result = _load_plugin_file(plugin)

    assert result.missing_deps == ["no-such-dist-xyz>=1"]
    assert counter.read_text() == ""


def test_non_literal_dependencies_are_an_explicit_error(tmp_path):
    plugin = tmp_path / "computed.py"
    plugin.write_text("REQS = ['a']\nDEPENDENCIES = REQS + ['b']\nTOOLS = []\n")

    result = _load_plugin_file(plugin)

    assert result.error is not None
    assert "DEPENDENCIES must be a literal list" in result.error


def test_a_command_plugin_module_executes_exactly_once(tmp_path, monkeypatch):
    from agenthicc.commands.plugin_loader import _load_command_file

    counter = tmp_path / "counter"
    counter.write_text("")
    monkeypatch.setenv("AGENTHICC_TEST_COUNTER", str(counter))
    plugin = tmp_path / "counted_cmd.py"
    plugin.write_text(
        "import os\n"
        "from pathlib import Path\n"
        "counter = Path(os.environ['AGENTHICC_TEST_COUNTER'])\n"
        "counter.write_text(counter.read_text() + 'x')\n"
        "DEPENDENCIES = ()\n"
        "from agenthicc.commands.command import Command\n"
        "COMMAND = Command(name='/counted', description='Counted.', handler=lambda ctx: True)\n"
    )

    result = _load_command_file(plugin)

    assert result.ok, result.error
    assert [command.name for command in result.commands] == ["/counted"]
    assert counter.read_text() == "x"
