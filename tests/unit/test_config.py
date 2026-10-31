"""Unit tests for agenthicc.config (PRD-07)."""

from __future__ import annotations

import textwrap
import tomllib
from pathlib import Path

import pytest

from agenthicc.config import AgenthiccConfig, deep_merge, load_config
from agenthicc.kernel import SecurityPolicy, SystemSettings

pytestmark = pytest.mark.unit


def _write(path, content: str) -> None:
    path.write_text(textwrap.dedent(content))


class TestDeepMerge:
    def test_scalar_override(self):
        assert deep_merge({"a": 1, "b": 2}, {"a": 9})["a"] == 9

    def test_nested_tables_merge_recursively(self):
        base = {"execution": {"max_parallel_tasks": 2, "max_agent_turns": 4}}
        override = {"execution": {"max_agent_turns": 8}}
        merged = deep_merge(base, override)
        assert merged["execution"] == {"max_parallel_tasks": 2, "max_agent_turns": 8}

    def test_lists_are_replaced_not_appended(self):
        merged = deep_merge({"x": {"l": [1, 2]}}, {"x": {"l": [3]}})
        assert merged["x"]["l"] == [3]


class TestLoadConfig:
    def test_subagent_model_routing_settings_load_and_redact(self, tmp_path):
        project = tmp_path / "agenthicc.toml"
        _write(
            project,
            """
            [subagents]
            default_model = "anthropic/worker-model"
            default_effort = " HIGH "
            default_context = 12000
            allowed_models = ["worker-model", "anthropic/other-model"]
            allow_call_overrides = true
            """,
        )

        config = load_config(project_path=project, user_path=tmp_path / "missing.toml")

        assert config.subagents.default_model == "anthropic/worker-model"
        assert config.subagents.default_effort == "high"
        assert config.subagents.default_context == 12_000
        assert config.subagents.allowed_models == ("worker-model", "anthropic/other-model")
        assert config.subagents.allow_call_overrides is True
        assert config.redacted_dict()["subagents"]["default_model"] == "anthropic/worker-model"

    def test_subagent_governance_settings_load_and_inherit_depth_limits(self, tmp_path):
        project = tmp_path / "agenthicc.toml"
        _write(
            project,
            """
            [subagents]
            max_depth = 3
            max_tasks = 64
            max_concurrent = 16
            max_active_pools = 8

            [subagents.depth_limits."2"]
            max_tasks = 12

            [subagents.depth_limits."3"]
            max_concurrent = 4

            [subagents.depth_tiers."2".reviewer]
            allowed_tools = ["read_file"]
            """,
        )

        config = load_config(project_path=project, user_path=tmp_path / "missing.toml")
        assert config.subagents.max_depth == 3
        assert config.subagents.max_active_pools == 8
        assert config.subagents.limits_for_depth(1) == (
            64,
            16,
            "subagents.max_tasks",
            "subagents.max_concurrent",
        )
        assert config.subagents.limits_for_depth(2) == (
            12,
            16,
            "subagents.depth_limits.2.max_tasks",
            "subagents.max_concurrent",
        )
        assert config.subagents.limits_for_depth(3) == (
            12,
            4,
            "subagents.depth_limits.2.max_tasks",
            "subagents.depth_limits.3.max_concurrent",
        )
        redacted = config.redacted_dict()["subagents"]
        assert redacted["depth_limits"]["2"]["max_tasks"] == 12
        assert redacted["depth_tiers"]["2"]["reviewer"]["allowed_tools"] == ["read_file"]

    def test_subagent_governance_settings_reject_boolean_limits(self, tmp_path):
        project = tmp_path / "agenthicc.toml"
        _write(project, "[subagents]\nmax_depth = true\n")

        with pytest.raises(ValueError, match="subagents.max_depth must be an integer"):
            load_config(project_path=project, user_path=tmp_path / "missing.toml")

    def test_subagent_model_routing_settings_reject_invalid_effort(self, tmp_path):
        project = tmp_path / "agenthicc.toml"
        _write(
            project,
            """
            [subagents]
            default_effort = "turbo"
            """,
        )

        with pytest.raises(ValueError, match="subagents.default_effort must be one of"):
            load_config(project_path=project, user_path=tmp_path / "missing.toml")

    def test_project_overrides_user_global(self, tmp_path):
        """Per-project config wins over user-global config (Git-style layering)."""
        project = tmp_path / "agenthicc.toml"
        user = tmp_path / ".agenthicc.toml"
        _write(
            project,
            """
            [execution]
            max_agent_turns = 4
            max_parallel_tasks = 2
        """,
        )
        _write(
            user,
            """
            [execution]
            max_agent_turns = 16
        """,
        )
        config = load_config(project_path=project, user_path=user)
        # project value wins
        assert config.execution.max_agent_turns == 4
        # project value also present
        assert config.execution.max_parallel_tasks == 2

    def test_user_global_supplies_defaults(self, tmp_path):
        """User-global config supplies defaults when project does not override them."""
        project = tmp_path / "agenthicc.toml"
        user = tmp_path / ".agenthicc.toml"
        _write(
            project,
            """
            [execution]
            max_agent_turns = 4
        """,
        )
        _write(
            user,
            """
            [execution]
            max_agent_turns = 16
            max_parallel_tasks = 99
        """,
        )
        config = load_config(project_path=project, user_path=user)
        # project overrides user-global for this key
        assert config.execution.max_agent_turns == 4
        # user-global value used for key not set in project
        assert config.execution.max_parallel_tasks == 99

    def test_partial_override_preserves_siblings(self, tmp_path):
        project = tmp_path / "agenthicc.toml"
        user = tmp_path / ".agenthicc.toml"
        _write(
            project,
            """
            [execution]
            max_agent_turns = 4
            max_parallel_tasks = 2
            authoring_max_phase_turns = 32

            [behaviour]
            resume_transcript_turns = 9
        """,
        )
        _write(
            user,
            """
            [execution]
            max_agent_turns = 16
        """,
        )
        config = load_config(project_path=project, user_path=user)
        # project value wins
        assert config.execution.max_agent_turns == 4
        # siblings from project present
        assert config.execution.max_parallel_tasks == 2
        assert config.execution.authoring_max_phase_turns == 32
        # other table from project present
        assert config.behaviour.resume_transcript_turns == 9

    def test_defaults_when_no_files(self, tmp_path):
        config = load_config(
            project_path=tmp_path / "missing.toml",
            user_path=tmp_path / "missing_user.toml",
        )
        assert config.execution.max_agent_turns == 200
        assert config.execution.max_parallel_tasks == 4
        assert config.execution.authoring_max_generation_attempts == 20
        assert config.execution.authoring_max_phase_turns == 20
        assert config.execution.timeout_s == 3600.0
        assert config.security.allowed_paths == ["/workspace"]
        assert config.behaviour.resume_transcript_turns == 20
        assert config.tools.allowed == []
        assert config.tools.denied == []
        assert config.subagents.max_depth == 1
        assert config.subagents.max_tasks == 64
        assert config.subagents.max_concurrent == 16
        assert config.subagents.max_active_pools == 16

    def test_provider_request_timeout_default_is_overridable(self, tmp_path):
        project = tmp_path / "agenthicc.toml"
        project.write_text("[execution]\ntimeout_s = 7200\n")

        config = load_config(project_path=project, user_path=tmp_path / "missing.toml")

        assert config.execution.timeout_s == 7200.0

    def test_provider_request_timeout_is_forwarded_to_llm_client(self):
        from agenthicc.config import ExecutionSettings, build_llm_config

        execution = ExecutionSettings(
            provider="anthropic",
            model="claude-haiku-4-5",
            api_key="test-key",
        )

        assert execution.timeout_s == 3600.0
        assert build_llm_config(execution).timeout == 3600.0

    def test_authoring_generation_attempts_are_configurable(self, tmp_path):
        project = tmp_path / "agenthicc.toml"
        _write(
            project,
            """
            [execution]
            authoring_max_generation_attempts = 6
            authoring_max_phase_turns = 7
            """,
        )

        config = load_config(project_path=project, user_path=tmp_path / "missing.toml")

        assert config.execution.authoring_max_generation_attempts == 6
        assert config.execution.authoring_max_phase_turns == 7

    def test_resume_transcript_turns_are_configurable_and_nonnegative(self, tmp_path):
        project = tmp_path / "agenthicc.toml"
        _write(
            project,
            """
            [behaviour]
            resume_transcript_turns = 7
            """,
        )
        config = load_config(project_path=project, user_path=tmp_path / "missing.toml")
        assert config.behaviour.resume_transcript_turns == 7

        project.write_text("[behaviour]\nresume_transcript_turns = -3\n")
        config = load_config(project_path=project, user_path=tmp_path / "missing.toml")
        assert config.behaviour.resume_transcript_turns == 0

    def test_resume_transcript_page_limits_have_bounded_defaults_and_clamps(self, tmp_path):
        project = tmp_path / "agenthicc.toml"
        config = load_config(project_path=project, user_path=tmp_path / "missing.toml")
        assert config.behaviour.resume_transcript_max_events == 128
        assert config.behaviour.resume_transcript_max_bytes == 128 * 1024
        assert config.behaviour.resume_transcript_max_event_bytes == 16 * 1024

        project.write_text(
            """[behaviour]
resume_transcript_max_events = 5000
resume_transcript_max_bytes = 999999999
resume_transcript_max_event_bytes = 999999999
""",
            encoding="utf-8",
        )
        config = load_config(project_path=project, user_path=tmp_path / "missing.toml")
        assert config.behaviour.resume_transcript_max_events == 1024
        assert config.behaviour.resume_transcript_max_bytes == 1024 * 1024
        assert config.behaviour.resume_transcript_max_event_bytes == 128 * 1024

        project.write_text(
            """[behaviour]
resume_transcript_max_events = 0
resume_transcript_max_bytes = 1
resume_transcript_max_event_bytes = 1
""",
            encoding="utf-8",
        )
        config = load_config(project_path=project, user_path=tmp_path / "missing.toml")
        assert config.behaviour.resume_transcript_max_events == 1
        assert config.behaviour.resume_transcript_max_bytes == 1024
        assert config.behaviour.resume_transcript_max_event_bytes == 256

    def test_invalid_toml_raises(self, tmp_path):
        bad = tmp_path / "agenthicc.toml"
        bad.write_text("[execution\nmax_parallel_tasks = ")
        with pytest.raises(tomllib.TOMLDecodeError):
            load_config(project_path=bad, user_path=tmp_path / "missing.toml")

    def test_context_windows_default_empty(self, tmp_path):
        # PRD-136: live window is derived from the model window, no fixed default.
        config = load_config(
            project_path=tmp_path / "missing.toml",
            user_path=tmp_path / "missing_user.toml",
        )
        assert config.execution.context_windows == {}
        # default model (claude-opus-4-8) → usable of its 1M registry window.
        assert config.execution.effective_usable_budget() > 900_000

    def test_context_windows_from_toml(self, tmp_path):
        project = tmp_path / "agenthicc.toml"
        project.write_text(
            "[memory.context_windows]\ndefault = 1000000\ndeepseek-v4-flash = 250000\n"
        )
        config = load_config(project_path=project, user_path=tmp_path / "missing.toml")
        assert config.execution.context_windows == {
            "default": 1_000_000,
            "deepseek-v4-flash": 250_000,
        }

    def test_context_reuse_flags_default_on(self, tmp_path):
        config = load_config(
            project_path=tmp_path / "missing.toml",
            user_path=tmp_path / "missing_user.toml",
        )
        assert config.execution.prompt_cache is True
        assert config.execution.file_cache is True

    def test_context_reuse_flags_from_toml(self, tmp_path):
        project = tmp_path / "agenthicc.toml"
        project.write_text("[execution]\nprompt_cache = false\nfile_cache = false\n")
        config = load_config(project_path=project, user_path=tmp_path / "missing.toml")
        assert config.execution.prompt_cache is False
        assert config.execution.file_cache is False

    def test_newly_documented_execution_browser_and_plugin_options_load(self, tmp_path):
        project = tmp_path / "agenthicc.toml"
        project.write_text(
            """
            [execution]
            turn_timeout_s = 12.5

            [tools.playwright]
            transport = "local"

            [plugins]
            strict_cli_shadow = true
            """
        )

        config = load_config(project_path=project, user_path=tmp_path / "missing.toml")

        assert config.execution.turn_timeout_s == 12.5
        assert config.tools.playwright.transport == "local"
        assert config.plugins.strict_cli_shadow is True

    def test_prompt_cache_enables_llm_caching(self, tmp_path):
        from agenthicc.config import ExecutionSettings, build_llm_config

        on = build_llm_config(
            ExecutionSettings(
                provider="anthropic", model="claude-x", api_key="k", prompt_cache=True
            )
        )
        assert on.cache_system_prompt and on.cache_tools and on.cache_conversation
        off = build_llm_config(
            ExecutionSettings(
                provider="anthropic", model="claude-x", api_key="k", prompt_cache=False
            )
        )
        assert not off.cache_conversation

    def test_project_list_overrides_user_global_list(self, tmp_path):
        """Project list replaces user-global list entirely (lists are not merged)."""
        project = tmp_path / "agenthicc.toml"
        user = tmp_path / ".agenthicc.toml"
        _write(
            project,
            """
            [security]
            allowed_paths = ["/workspace", "/data"]
        """,
        )
        _write(
            user,
            """
            [security]
            allowed_paths = ["/home/user/project"]
        """,
        )
        config = load_config(project_path=project, user_path=user)
        # project list wins; user-global list replaced
        assert config.security.allowed_paths == ["/workspace", "/data"]

    def test_tools_section(self, tmp_path):
        project = tmp_path / "agenthicc.toml"
        _write(
            project,
            """
            [tools]
            plugins = ["myproj.tools:Plugin"]
            allowed = ["read_*"]
            denied = ["shell_exec"]

            [[tools.mcp_servers]]
            name = "filesystem"
            url = "npx server-filesystem"
        """,
        )
        config = load_config(project_path=project, user_path=tmp_path / "missing.toml")
        assert config.tools.plugins == ["myproj.tools:Plugin"]
        assert config.tools.allowed == ["read_*"]
        assert config.tools.denied == ["shell_exec"]
        mcp_entry = config.tools.mcp_servers[0]
        # mcp_servers entries are McpServerConfig objects; fall back to dict access for compat
        name = mcp_entry.name if hasattr(mcp_entry, "name") else mcp_entry["name"]
        assert name == "filesystem"


class TestSubagentLifecycleSettings:
    def test_defaults_and_toml_overrides(self, tmp_path: Path) -> None:
        missing = tmp_path / "missing.toml"
        defaults = load_config(project_path=missing, user_path=missing)

        assert defaults.subagents.max_workers == 32
        assert defaults.subagents.max_concurrent == 16
        assert defaults.subagents.max_tool_calls == 256
        assert defaults.subagents.max_iterations == 128
        assert defaults.subagents.max_tokens == 200_000
        assert defaults.subagents.max_total_time_s == 600.0
        assert defaults.subagents.on_budget_exceeded == "drain"
        assert defaults.subagents.stop_grace_s == 5.0

        project = tmp_path / "agenthicc.toml"
        _write(
            project,
            """
            [subagents]
            max_workers = 12
            max_concurrent = 3
            max_tool_calls = 48
            max_iterations = 24
            max_tokens = 50000
            max_total_time_s = 90.0
            on_budget_exceeded = "abort"
            stop_grace_s = 2.5
            """,
        )
        config = load_config(project_path=project, user_path=missing)

        assert config.subagents.max_workers == 12
        assert config.subagents.max_concurrent == 3
        assert config.subagents.max_tool_calls == 48
        assert config.subagents.max_iterations == 24
        assert config.subagents.max_tokens == 50_000
        assert config.subagents.max_total_time_s == 90.0
        assert config.subagents.on_budget_exceeded == "abort"
        assert config.subagents.stop_grace_s == 2.5

    @pytest.mark.parametrize(
        "settings",
        (
            'on_budget_exceeded = "ignore"',
            "stop_grace_s = -1.0",
            "stop_grace_s = true",
            "stop_grace_s = nan",
            "max_workers = 0",
            "max_concurrent = 4\nmax_workers = 2",
            "max_iterations = 1.5",
            "max_tokens = true",
            "max_total_time_s = inf",
        ),
    )
    def test_invalid_lifecycle_settings_are_rejected(self, tmp_path: Path, settings: str) -> None:
        project = tmp_path / "agenthicc.toml"
        _write(project, f"[subagents]\n{settings}\n")

        with pytest.raises(ValueError, match="subagents\\."):
            load_config(project_path=project, user_path=tmp_path / "missing.toml")

    def test_commented_template_contains_lifecycle_settings(self) -> None:
        from agenthicc.config_template import build_commented_config_template

        template = build_commented_config_template()

        assert "# max_workers = 32" in template
        assert "# max_concurrent = 4" in template
        assert "# max_tool_calls = 256" in template
        assert "# max_iterations = 128" in template
        assert "# max_tokens = 200000" in template
        assert "# max_total_time_s = 600.0" in template
        assert '# on_budget_exceeded = "drain"' in template
        assert "# stop_grace_s = 5.0" in template


class TestConverters:
    def test_to_system_settings(self):
        config = AgenthiccConfig()
        config.execution.max_parallel_tasks = 7
        settings = config.to_system_settings()
        assert isinstance(settings, SystemSettings)
        assert settings.max_parallel_tasks == 7

    def test_to_security_policy_fail_closed(self):
        config = AgenthiccConfig()
        config.tools.allowed = ["read_*"]
        config.tools.denied = ["shell_exec", "network_raw"]
        policy = config.to_security_policy()
        assert isinstance(policy, SecurityPolicy)
        assert policy.default_action == "deny"
        # deny rules come first so deny wins over a broader allow
        actions = [(r.tool_pattern, r.action) for r in policy.permission_rules]
        assert actions == [
            ("shell_exec", "deny"),
            ("network_raw", "deny"),
            ("read_*", "allow"),
        ]

    def test_to_security_policy_from_loaded_toml(self, tmp_path):
        project = tmp_path / "agenthicc.toml"
        _write(
            project,
            """
            [tools]
            allowed = ["fs_*"]
            denied = ["fs_delete"]
        """,
        )
        config = load_config(project_path=project, user_path=tmp_path / "missing.toml")
        policy = config.to_security_policy()
        from agenthicc.security import PermissionChecker

        checker = PermissionChecker(policy)
        assert checker.check("fs_read") == "allow"
        assert checker.check("fs_delete") == "deny"
        assert checker.check("unrelated_tool") == "deny"
