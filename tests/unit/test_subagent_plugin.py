"""Unit tests for PRD-124 Phase 5 — plugin ecosystem."""

from __future__ import annotations

import pytest

from agenthicc.subagents.types import (
    SubagentAggregator,
    SubagentTypeRegistry,
    DEFAULT_REGISTRY,
)
from agenthicc.subagents.pool import SubagentResult, _aggregate

pytestmark = pytest.mark.unit


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
        from agenthicc.plugins.discovery import _load_plugin_file  # noqa: PLC0415

        result = _load_plugin_file(plugin)
        assert result.error is None
        # Type should now be in DEFAULT_REGISTRY
        assert "test_plugin_type_unique_xyz" in DEFAULT_REGISTRY

    def test_load_plugin_file_non_spec_in_subagent_types_is_skipped(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        plugin = tmp_path / "bad_plugin.py"
        plugin.write_text("TOOLS = []\nSUBAGENT_TYPES = ['not_a_spec', 42]\n")
        from agenthicc.plugins.discovery import _load_plugin_file  # noqa: PLC0415

        # Should not raise; invalid entries are skipped with a warning
        result = _load_plugin_file(plugin)
        assert result.error is None


# ── Example plugin: security_checker ─────────────────────────────────────────


class TestExampleSecurityCheckerPlugin:
    def test_security_checker_type_registered_after_load(self) -> None:
        """Load the example plugin and verify the type is registered."""
        from pathlib import Path  # noqa: PLC0415
        from agenthicc.plugins.discovery import _load_plugin_file  # noqa: PLC0415

        plugin_path = Path(
            "/root/python_projects/python-password-generator/.agenthicc/tools/security_checker.py"
        )
        if not plugin_path.exists():
            pytest.skip("Example plugin not found")

        result = _load_plugin_file(plugin_path)
        assert result.error is None
        assert "security_checker" in DEFAULT_REGISTRY
        spec = DEFAULT_REGISTRY.get("security_checker")
        assert spec is not None
        assert "write_file" not in spec.allowed_tools  # read-only type

    def test_security_checker_aggregator_registered(self) -> None:
        from pathlib import Path  # noqa: PLC0415
        from agenthicc.plugins.discovery import _load_plugin_file  # noqa: PLC0415

        plugin_path = Path(
            "/root/python_projects/python-password-generator/.agenthicc/tools/security_checker.py"
        )
        if not plugin_path.exists():
            pytest.skip("Example plugin not found")

        _load_plugin_file(plugin_path)
        agg = DEFAULT_REGISTRY.get_aggregator("security_checker")
        assert agg is not None
