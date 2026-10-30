"""Unit tests for PRD-124 Phase 5 — plugin ecosystem."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agenthicc.agents.definitions import AgentSpec, DefinitionError, parse_definition
from agenthicc.agents.registry import AgentsRegistry, CatalogEntry
from agenthicc.runners.agent_turn import AgentTurnRunner

from agenthicc.subagents.types import (
    SubagentAggregator,
    SubagentTypeRegistry,
    DEFAULT_REGISTRY,
)
from agenthicc.subagents.pool import SubagentResult, _aggregate

pytestmark = pytest.mark.unit


def _build_definition_registry(
    agents: AgentsRegistry,
    agent_config: object | None = None,
) -> tuple[SubagentTypeRegistry, dict[str, str]]:
    runner = object.__new__(AgentTurnRunner)
    runner._ctx = SimpleNamespace(
        agents_registry=agents,
        subagent_registry=DEFAULT_REGISTRY.copy(),
        agent_config=agent_config,
        max_agent_turns=10,
        workspace_access=None,
    )
    runner._definition_agent_errors = {}
    registry = runner._build_definition_subagent_registry(SimpleNamespace(), [], None)
    return registry, runner._definition_agent_errors


def _project_definition_registry(*, override: bool) -> tuple[SubagentTypeRegistry, dict[str, str]]:
    agents = AgentsRegistry()
    agents.register(
        AgentSpec(
            name="explorer",
            description="Project explorer",
            prompt="Project-specific instructions.",
            mode="subagent",
            source="project",
            path=Path(".agenthicc/agents/explorer.md"),
            override=override,
        )
    )
    return _build_definition_registry(agents)


def test_invalid_project_definition_does_not_fall_back_to_builtin_type() -> None:
    agents = AgentsRegistry()
    agents.register(
        AgentSpec(
            name="explorer",
            description="Built-in explorer",
            mode="subagent",
            source="builtin",
        )
    )
    path = Path(".agenthicc/agents/explorer.md")
    agents.catalog = (
        CatalogEntry(
            name="explorer",
            source="project",
            state="invalid",
            path=path,
            error=DefinitionError(path, 2, "unknown key"),
        ),
    )

    registry, errors = _build_definition_registry(agents)

    assert registry.get("explorer") is None
    assert "unknown key" in errors["explorer"]


def test_project_definition_must_opt_in_to_shadow_builtin_subagent() -> None:
    registry, errors = _project_definition_registry(override=False)

    assert registry.get("explorer") is DEFAULT_REGISTRY.get("explorer")
    assert "built-in subagent type 'explorer'" in errors["explorer"]
    assert "override: true" in errors["explorer"]


def test_definition_model_effort_and_context_reach_registered_type() -> None:
    from agenthicc.config import AgenthiccConfig

    agents = AgentsRegistry()
    agents.register(
        AgentSpec(
            name="routed_worker",
            description="Configured worker",
            mode="subagent",
            model="inherit",
            effort="high",
            context=12_000,
            source="project",
        )
    )

    registry, errors = _build_definition_registry(agents, AgenthiccConfig())
    spec = registry.get("routed_worker")

    assert errors == {}
    assert spec is not None
    assert spec.model == "inherit"
    assert spec.effort == "high"
    assert spec.context == 12_000


def test_definition_output_schema_reaches_registered_subagent_type() -> None:
    output_schema = {
        "type": "object",
        "properties": {"summary": {"type": "string"}},
        "required": ["summary"],
        "additionalProperties": False,
    }
    agents = AgentsRegistry()
    agents.register(
        AgentSpec(
            name="reporter",
            description="Returns structured reports",
            prompt="Return a report.",
            mode="subagent",
            output_schema=output_schema,
            source="project",
        )
    )

    registry, errors = _build_definition_registry(agents)

    spec = registry.get("reporter")
    assert errors == {}
    assert spec is not None
    assert spec.output_schema == output_schema


def test_invalid_output_schema_is_reported_and_not_registered(tmp_path: Path) -> None:
    path = tmp_path / "reporter.md"
    with pytest.raises(DefinitionError, match="api_token") as error:
        parse_definition(
            "---\nname: reporter\ndescription: Returns reports\nmode: subagent\n"
            'output: {"type":"object","properties":{"api_token":{"type":"string"}}}\n'
            "---\nReturn a report.",
            path=path,
            source="project",
        )

    agents = AgentsRegistry()
    agents.catalog = (
        CatalogEntry(
            name="reporter",
            source="project",
            state="invalid",
            path=path,
            error=error.value,
        ),
    )

    registry, errors = _build_definition_registry(agents)

    assert registry.get("reporter") is None
    assert str(path) in errors["reporter"]
    assert "api_token" in errors["reporter"]


def test_project_definition_can_opt_in_to_shadow_builtin_subagent() -> None:
    registry, errors = _project_definition_registry(override=True)
    spec = registry.get("explorer")

    assert spec is not None
    assert spec is not DEFAULT_REGISTRY.get("explorer")
    assert "Project-specific instructions." in spec.system_prompt
    assert errors == {}


# ── SubagentAggregator ────────────────────────────────────────────────────────


class TestSubagentAggregator:
    def test_is_abstract_base_class(self) -> None:
        with pytest.raises((NotImplementedError, TypeError)):
            agg = SubagentAggregator()
            agg.aggregate([])

    def test_subclass_can_be_instantiated(self) -> None:
        class MyAgg(SubagentAggregator):
            agent_type = "my_type"

            def aggregate(self, results: list) -> str:
                return "custom output"

        agg = MyAgg()
        assert agg.agent_type == "my_type"
        assert agg.aggregate([]) == "custom output"


# ── SubagentTypeRegistry aggregator support ───────────────────────────────────


class TestRegistryAggregatorSupport:
    def test_register_and_retrieve_aggregator(self) -> None:
        reg = SubagentTypeRegistry()

        class Agg(SubagentAggregator):
            agent_type = "custom"

            def aggregate(self, results: list) -> str:
                return "aggregated"

        reg.register_aggregator(Agg())
        retrieved = reg.get_aggregator("custom")
        assert retrieved is not None
        assert retrieved.aggregate([]) == "aggregated"

    def test_get_aggregator_returns_none_for_unknown(self) -> None:
        reg = SubagentTypeRegistry()
        assert reg.get_aggregator("no_such_type") is None

    def test_registering_aggregator_replaces_existing(self) -> None:
        reg = SubagentTypeRegistry()

        class Agg1(SubagentAggregator):
            agent_type = "x"

            def aggregate(self, results: list) -> str:
                return "v1"

        class Agg2(SubagentAggregator):
            agent_type = "x"

            def aggregate(self, results: list) -> str:
                return "v2"

        reg.register_aggregator(Agg1())
        reg.register_aggregator(Agg2())
        assert reg.get_aggregator("x").aggregate([]) == "v2"


# ── Custom aggregator used in _aggregate ─────────────────────────────────────


class TestCustomAggregatorInAggregate:
    def _result(self, label: str, ok: bool, text: str = "output") -> SubagentResult:
        return SubagentResult("t1", "security_checker", label, ok, text)

    def test_custom_aggregator_called_for_matching_type(self) -> None:
        reg = SubagentTypeRegistry()

        class SecurityAgg(SubagentAggregator):
            agent_type = "security_checker"

            def aggregate(self, results: list) -> str:
                return f"SECURITY: {len(results)} files reviewed"

        reg.register_aggregator(SecurityAgg())
        results = [
            self._result("security_checker #1", True, "APPROVED"),
            self._result("security_checker #2", True, "NEEDS CHANGES"),
        ]
        agg = _aggregate("pool-1", results, reg)
        assert "SECURITY:" in agg.text
        assert "2 files reviewed" in agg.text

    def test_default_aggregator_used_when_no_custom(self) -> None:
        reg = SubagentTypeRegistry()  # no custom aggregator registered
        results = [self._result("explorer #1", True, "Found 3 files")]
        agg = _aggregate("pool-1", results, reg)
        # Default format: labelled sections
        assert "=== explorer #1" in agg.text
        assert "Found 3 files" in agg.text


# ── Plugin discovery: SUBAGENT_TYPES loading ─────────────────────────────────


class TestPluginSubagentTypesLoading:
    def test_load_plugin_file_with_subagent_types(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """A plugin file with SUBAGENT_TYPES registers types into DEFAULT_REGISTRY."""
        # Write a minimal plugin file
        plugin = tmp_path / "my_plugin.py"
        plugin.write_text(
            "from agenthicc.subagents.types import SubagentTypeSpec\n"
            "TOOLS = []\n"
            "SUBAGENT_TYPES = [\n"
            "    SubagentTypeSpec(\n"
            "        name='test_plugin_type_unique_xyz',\n"
            "        allowed_tools=frozenset({'read_file'}),\n"
            "        max_turns=5,\n"
            "        system_prompt='You are a test agent.',\n"
            "    )\n"
            "]\n"
        )
        from agenthicc.plugins.discovery import _load_plugin_file

        result = _load_plugin_file(plugin)
        assert result.error is None
        # Type should now be in DEFAULT_REGISTRY
        assert "test_plugin_type_unique_xyz" in DEFAULT_REGISTRY

    def test_discovery_registers_types_only_in_session_registry(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        plugin_dir = tmp_path / ".agenthicc" / "tools"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "session_scoped_type.py").write_text(
            "from agenthicc.subagents.types import SubagentTypeSpec\n"
            "TOOLS = []\n"
            "SUBAGENT_TYPES = [SubagentTypeSpec(\n"
            "    name='session_scoped_type_unique_xyz',\n"
            "    allowed_tools=frozenset(),\n"
            "    max_turns=2,\n"
            "    system_prompt='Session scoped.',\n"
            ")]\n",
            encoding="utf-8",
        )
        from agenthicc.plugins.discovery import discover_project_tools

        registry = DEFAULT_REGISTRY.copy()
        assert "session_scoped_type_unique_xyz" not in DEFAULT_REGISTRY

        discover_project_tools(
            project_dir=tmp_path / ".agenthicc",
            user_dir=tmp_path / "user",
            subagent_registry=registry,
        )

        assert "session_scoped_type_unique_xyz" in registry
        assert "session_scoped_type_unique_xyz" not in DEFAULT_REGISTRY

    def test_load_plugin_file_non_spec_in_subagent_types_is_skipped(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        plugin = tmp_path / "bad_plugin.py"
        plugin.write_text("TOOLS = []\nSUBAGENT_TYPES = ['not_a_spec', 42]\n")
        from agenthicc.plugins.discovery import _load_plugin_file

        # Should not raise; invalid entries are skipped with a warning
        result = _load_plugin_file(plugin)
        assert result.error is None


# ── Example plugin: security_checker ─────────────────────────────────────────


_SECURITY_CHECKER_PLUGIN = """
from agenthicc.subagents.types import SubagentAggregator, SubagentTypeSpec

security_checker_spec = SubagentTypeSpec(
    name="security_checker",
    allowed_tools=frozenset({"read_file", "grep_files", "read_lines", "file_exists"}),
    max_turns=8,
    system_prompt="Review the code for security issues.",
    max_turn_time_s=90.0,
)


class SecurityCheckerAggregator(SubagentAggregator):
    agent_type = "security_checker"

    def aggregate(self, results: list) -> str:
        return "## Security Review Summary"


TOOLS = []
SUBAGENT_TYPES = [security_checker_spec]

from agenthicc.subagents.types import DEFAULT_REGISTRY  # noqa: E402

DEFAULT_REGISTRY.register_aggregator(SecurityCheckerAggregator())
"""


class TestExampleSecurityCheckerPlugin:
    """A tool plugin can register a read-only subagent type and its aggregator."""

    @staticmethod
    def _load(tmp_path):  # type: ignore[no-untyped-def]
        from agenthicc.plugins.discovery import _load_plugin_file

        plugin_path = tmp_path / "security_checker.py"
        plugin_path.write_text(_SECURITY_CHECKER_PLUGIN, encoding="utf-8")
        return _load_plugin_file(plugin_path)

    def test_security_checker_type_registered_after_load(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        result = self._load(tmp_path)
        assert result.ok, result.error
        assert "security_checker" in DEFAULT_REGISTRY
        spec = DEFAULT_REGISTRY.get("security_checker")
        assert spec is not None
        assert "write_file" not in spec.allowed_tools  # read-only type

    def test_security_checker_aggregator_registered(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        self._load(tmp_path)
        agg = DEFAULT_REGISTRY.get_aggregator("security_checker")
        assert agg is not None

    def test_session_discovery_captures_legacy_aggregator_registration(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        from agenthicc.plugins.discovery import discover_project_tools

        plugin_dir = tmp_path / ".agenthicc" / "tools"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "session_aggregator.py").write_text(
            "from agenthicc.subagents.types import (\n"
            "    DEFAULT_REGISTRY, SubagentAggregator, SubagentTypeSpec\n"
            ")\n"
            "SUBAGENT_TYPES = [SubagentTypeSpec(\n"
            "    name='session_aggregator_unique_xyz',\n"
            "    allowed_tools=frozenset(), max_turns=2, system_prompt='Review.'\n"
            ")]\n"
            "class Aggregator(SubagentAggregator):\n"
            "    agent_type = 'session_aggregator_unique_xyz'\n"
            "    def aggregate(self, results: list) -> str:\n"
            "        return 'session aggregate'\n"
            "DEFAULT_REGISTRY.register_aggregator(Aggregator())\n",
            encoding="utf-8",
        )
        registry = DEFAULT_REGISTRY.copy()

        discover_project_tools(
            project_dir=tmp_path / ".agenthicc",
            user_dir=tmp_path / "user",
            subagent_registry=registry,
        )

        assert registry.get_aggregator("session_aggregator_unique_xyz") is not None
        assert DEFAULT_REGISTRY.get_aggregator("session_aggregator_unique_xyz") is None


def test_depth_tier_narrowing_is_accepted_and_widening_is_rejected() -> None:
    from agenthicc.config import SubagentDepthTier, SubagentSettings
    from agenthicc.subagents.tool import make_spawn_subagents_tool
    from agenthicc.subagents.types import SubagentTypeSpec

    registry = SubagentTypeRegistry()
    registry.register(
        SubagentTypeSpec(
            name="tiered_worker",
            allowed_tools=frozenset({"read_file", "write_file"}),
            max_turns=1,
            system_prompt="Work within the configured tier.",
            model="inherit",
        )
    )
    narrowed = SubagentSettings(
        allowed_models=("model-a", "model-b"),
        depth_tiers={
            2: {
                "tiered_worker": SubagentDepthTier(
                    allowed_tools=("read_file",),
                    allowed_models=("model-a",),
                )
            }
        },
    )

    assert callable(
        make_spawn_subagents_tool(
            object(), "model-a", [], registry=registry, subagent_settings=narrowed
        )
    )

    widened_tools = SubagentSettings(
        depth_tiers={2: {"tiered_worker": SubagentDepthTier(allowed_tools=("shell",))}}
    )
    with pytest.raises(ValueError, match="grants tools outside the type or inherited profile"):
        make_spawn_subagents_tool(
            object(), "model-a", [], registry=registry, subagent_settings=widened_tools
        )

    widened_models = SubagentSettings(
        allowed_models=("model-a",),
        depth_tiers={2: {"tiered_worker": SubagentDepthTier(allowed_models=("model-b",))}},
    )
    with pytest.raises(ValueError, match="grants models outside the type or inherited profile"):
        make_spawn_subagents_tool(
            object(), "model-a", [], registry=registry, subagent_settings=widened_models
        )
