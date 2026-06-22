"""Unit tests for agenthicc.config (PRD-07)."""

from __future__ import annotations

import textwrap
import tomllib

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
        base = {"execution": {"max_parallel_tasks": 2, "agent_pool_size": 4}}
        override = {"execution": {"agent_pool_size": 8}}
        merged = deep_merge(base, override)
        assert merged["execution"] == {"max_parallel_tasks": 2, "agent_pool_size": 8}

    def test_lists_are_replaced_not_appended(self):
        merged = deep_merge({"x": {"l": [1, 2]}}, {"x": {"l": [3]}})
        assert merged["x"]["l"] == [3]


class TestLoadConfig:
    def test_project_overrides_user_global(self, tmp_path):
        """Per-project config wins over user-global config (Git-style layering)."""
        project = tmp_path / "agenthicc.toml"
        user = tmp_path / ".agenthicc.toml"
        _write(project, """
            [execution]
            max_concurrent_intents = 4
            max_parallel_tasks = 2
        """)
        _write(user, """
            [execution]
            max_concurrent_intents = 16
        """)
        config = load_config(project_path=project, user_path=user)
        # project value wins
        assert config.execution.max_concurrent_intents == 4
        # project value also present
        assert config.execution.max_parallel_tasks == 2

    def test_user_global_supplies_defaults(self, tmp_path):
        """User-global config supplies defaults when project does not override them."""
        project = tmp_path / "agenthicc.toml"
        user = tmp_path / ".agenthicc.toml"
        _write(project, """
            [execution]
            max_concurrent_intents = 4
        """)
        _write(user, """
            [execution]
            max_concurrent_intents = 16
            max_parallel_tasks = 99
        """)
        config = load_config(project_path=project, user_path=user)
        # project overrides user-global for this key
        assert config.execution.max_concurrent_intents == 4
        # user-global value used for key not set in project
        assert config.execution.max_parallel_tasks == 99

    def test_partial_override_preserves_siblings(self, tmp_path):
        project = tmp_path / "agenthicc.toml"
        user = tmp_path / ".agenthicc.toml"
        _write(project, """
            [execution]
            max_concurrent_intents = 4
            max_parallel_tasks = 2
            agent_pool_size = 32

            [api]
            port = 9000
        """)
        _write(user, """
            [execution]
            max_concurrent_intents = 16
        """)
        config = load_config(project_path=project, user_path=user)
        # project value wins
        assert config.execution.max_concurrent_intents == 4
        # siblings from project present
        assert config.execution.max_parallel_tasks == 2
        assert config.execution.agent_pool_size == 32
        # other table from project present
        assert config.api.port == 9000

    def test_defaults_when_no_files(self, tmp_path):
        config = load_config(
            project_path=tmp_path / "missing.toml",
            user_path=tmp_path / "missing_user.toml",
        )
        assert config.execution.max_concurrent_intents == 8
        assert config.execution.max_parallel_tasks == 4
        assert config.execution.agent_pool_size == 16
        assert config.security.sandbox_mode is True
        assert config.security.allowed_paths == ["/workspace"]
        assert config.security.max_tool_cpu_seconds == 30
        assert config.security.max_tool_memory_mb == 512
        assert config.api.host == "127.0.0.1"
        assert config.api.port == 8000
        assert config.api.api_key_env == "AGENTHICC_API_KEY"
        assert config.memory.vector_db == "sqlite-vec"
        assert config.memory.session_ttl_seconds == 86400
        assert config.hooks == {}
        assert config.tools.allowed == []
        assert config.tools.denied == []

    def test_invalid_toml_raises(self, tmp_path):
        bad = tmp_path / "agenthicc.toml"
        bad.write_text("[execution\nmax_parallel_tasks = ")
        with pytest.raises(tomllib.TOMLDecodeError):
            load_config(project_path=bad, user_path=tmp_path / "missing.toml")

    def test_session_memory_max_tokens_default(self, tmp_path):
        config = load_config(
            project_path=tmp_path / "missing.toml",
            user_path=tmp_path / "missing_user.toml",
        )
        assert config.execution.session_memory_max_tokens == 32_000

    def test_session_memory_max_tokens_from_toml(self, tmp_path):
        project = tmp_path / "agenthicc.toml"
        project.write_text("[execution]\nsession_memory_max_tokens = 64000\n")
        config = load_config(project_path=project, user_path=tmp_path / "missing.toml")
        assert config.execution.session_memory_max_tokens == 64_000

    def test_session_memory_max_tokens_cli_override_wins(self, tmp_path):
        project = tmp_path / "agenthicc.toml"
        project.write_text("[execution]\nsession_memory_max_tokens = 64000\n")
        config = load_config(
            project_path=project,
            user_path=tmp_path / "missing.toml",
            cli_overrides=["execution.session_memory_max_tokens=128000"],
        )
        assert config.execution.session_memory_max_tokens == 128_000

    def test_project_list_overrides_user_global_list(self, tmp_path):
        """Project list replaces user-global list entirely (lists are not merged)."""
        project = tmp_path / "agenthicc.toml"
        user = tmp_path / ".agenthicc.toml"
        _write(project, """
            [security]
            allowed_paths = ["/workspace", "/data"]
        """)
        _write(user, """
            [security]
            allowed_paths = ["/home/user/project"]
        """)
        config = load_config(project_path=project, user_path=user)
        # project list wins; user-global list replaced
        assert config.security.allowed_paths == ["/workspace", "/data"]

    def test_hooks_and_tools_sections(self, tmp_path):
        project = tmp_path / "agenthicc.toml"
        _write(project, """
            [hooks]
            "intent.pre_validate" = ["myproj.hooks.audit"]
            tool.post_execute = ["myproj.hooks.log_tool"]

            [tools]
            plugins = ["myproj.tools:Plugin"]
            allowed = ["read_*"]
            denied = ["shell_exec"]

            [[tools.mcp_servers]]
            name = "filesystem"
            url = "npx server-filesystem"
        """)
        config = load_config(
            project_path=project, user_path=tmp_path / "missing.toml"
        )
        assert config.hooks["intent.pre_validate"] == ["myproj.hooks.audit"]
        assert config.hooks["tool.post_execute"] == ["myproj.hooks.log_tool"]
        assert config.tools.plugins == ["myproj.tools:Plugin"]
        assert config.tools.allowed == ["read_*"]
        assert config.tools.denied == ["shell_exec"]
        mcp_entry = config.tools.mcp_servers[0]
        # mcp_servers entries are McpServerConfig objects; fall back to dict access for compat
        name = mcp_entry.name if hasattr(mcp_entry, "name") else mcp_entry["name"]
        assert name == "filesystem"


class TestConverters:
    def test_to_system_settings(self):
        config = AgenthiccConfig()
        config.execution.max_concurrent_intents = 3
        config.execution.max_parallel_tasks = 7
        config.execution.agent_pool_size = 11
        settings = config.to_system_settings()
        assert isinstance(settings, SystemSettings)
        assert settings.max_concurrent_intents == 3
        assert settings.max_parallel_tasks == 7
        assert settings.agent_pool_size == 11

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
        _write(project, """
            [tools]
            allowed = ["fs_*"]
            denied = ["fs_delete"]
        """)
        config = load_config(project_path=project, user_path=tmp_path / "missing.toml")
        policy = config.to_security_policy()
        from agenthicc.security import PermissionChecker

        checker = PermissionChecker(policy)
        assert checker.check("fs_read") == "allow"
        assert checker.check("fs_delete") == "deny"
        assert checker.check("unrelated_tool") == "deny"
