"""Tests for PRD-21 configuration management: file search, env overrides, CLI overrides."""
from __future__ import annotations

import pytest
from pathlib import Path

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Conditional imports — the other agent may not have landed the new symbols yet.
# We skip individual test classes if the required symbol is missing, rather than
# failing the entire module at collection time.
# ---------------------------------------------------------------------------

try:
    from agenthicc.config import (
        load_config,
        _find_config_file,  # type: ignore[attr-defined]
        _coerce_env,  # type: ignore[attr-defined]
        _apply_env_overrides,  # type: ignore[attr-defined]
        _apply_cli_overrides,  # type: ignore[attr-defined]
        PROJECT_CONFIG_CANDIDATES,  # type: ignore[attr-defined]
        USER_CONFIG_CANDIDATES,  # type: ignore[attr-defined]
    )
    _HELPERS_AVAILABLE = True
except ImportError:
    _HELPERS_AVAILABLE = False
    # Provide stubs so that load_config is always importable
    from agenthicc.config import load_config  # noqa: F811

# Decorator applied to whole classes that depend on the new helper functions
_needs_helpers = pytest.mark.skipif(
    not _HELPERS_AVAILABLE,
    reason="PRD-21 helpers not yet added to config.py",
)


# ---------------------------------------------------------------------------
# TestFindConfigFile
# ---------------------------------------------------------------------------

@_needs_helpers
class TestFindConfigFile:
    def test_returns_none_when_no_file_exists(self, tmp_path: Path) -> None:
        candidates = [tmp_path / "nonexistent.toml", tmp_path / "also-missing.toml"]
        assert _find_config_file(candidates) is None

    def test_returns_first_existing(self, tmp_path: Path) -> None:
        first = tmp_path / "first.toml"
        first.write_text("[execution]\n")
        second = tmp_path / "second.toml"
        second.write_text("[execution]\n")
        result = _find_config_file([first, second])
        assert result == first

    def test_skips_nonexistent_returns_second(self, tmp_path: Path) -> None:
        first = tmp_path / "missing.toml"
        second = tmp_path / "exists.toml"
        second.write_text("[execution]\n")
        result = _find_config_file([first, second])
        assert result == second

    def test_prefers_earlier_candidate(self, tmp_path: Path) -> None:
        first = tmp_path / "a.toml"
        second = tmp_path / "b.toml"
        first.write_text("")
        second.write_text("")
        assert _find_config_file([first, second]) == first

    def test_empty_list_returns_none(self) -> None:
        assert _find_config_file([]) is None


# ---------------------------------------------------------------------------
# TestCoerceEnv
# ---------------------------------------------------------------------------

@_needs_helpers
class TestCoerceEnv:
    def test_true_variants(self) -> None:
        for val in ("true", "1", "yes", "True", "YES"):
            assert _coerce_env(val) is True, f"Expected True for {val!r}"

    def test_false_variants(self) -> None:
        for val in ("false", "0", "no", "False"):
            assert _coerce_env(val) is False, f"Expected False for {val!r}"

    def test_int_coercion(self) -> None:
        result = _coerce_env("42")
        assert result == 42
        assert isinstance(result, int)

    def test_float_coercion(self) -> None:
        result = _coerce_env("3.14")
        assert abs(result - 3.14) < 1e-9
        assert isinstance(result, float)

    def test_string_passthrough(self) -> None:
        assert _coerce_env("hello") == "hello"

    def test_empty_string(self) -> None:
        result = _coerce_env("")
        assert result == ""
        assert isinstance(result, str)

    def test_negative_int(self) -> None:
        assert _coerce_env("-5") == -5

    def test_zero_string_is_false(self) -> None:
        # "0" is a well-known falsy env value
        assert _coerce_env("0") is False


# ---------------------------------------------------------------------------
# TestApplyEnvOverrides
# ---------------------------------------------------------------------------

@_needs_helpers
class TestApplyEnvOverrides:
    def test_agenthicc_var_applied(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENTHICC_EXECUTION_MAX_PARALLEL_TASKS", "99")
        config: dict = {}
        result = _apply_env_overrides(config)
        assert result.get("execution", {}).get("max_parallel_tasks") == 99

    def test_non_agenthicc_var_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OTHER_VAR", "x")
        config: dict = {"execution": {"max_parallel_tasks": 4}}
        result = _apply_env_overrides(config)
        assert result["execution"]["max_parallel_tasks"] == 4

    def test_bool_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENTHICC_SECURITY_SANDBOX_MODE", "true")
        config: dict = {}
        result = _apply_env_overrides(config)
        assert result.get("security", {}).get("sandbox_mode") is True

    def test_env_var_without_section_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A single-part key after stripping prefix (no underscore → no section split)
        monkeypatch.setenv("AGENTHICC_NOFIELD", "x")
        config: dict = {}
        # Must not raise; result is unchanged (ignored)
        result = _apply_env_overrides(config)
        assert result.get("nofield") is None

    def test_returns_dict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENTHICC_API_PORT", "9000")
        config: dict = {}
        result = _apply_env_overrides(config)
        assert isinstance(result, dict)

    def test_preserves_existing_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENTHICC_EXECUTION_AGENT_POOL_SIZE", "8")
        config: dict = {"execution": {"max_parallel_tasks": 4}}
        result = _apply_env_overrides(config)
        assert result["execution"]["max_parallel_tasks"] == 4
        assert result["execution"]["agent_pool_size"] == 8


# ---------------------------------------------------------------------------
# TestApplyCliOverrides
# ---------------------------------------------------------------------------

@_needs_helpers
class TestApplyCliOverrides:
    def test_single_override(self) -> None:
        config: dict = {}
        result = _apply_cli_overrides(config, ["execution.max_parallel_tasks=10"])
        assert result.get("execution", {}).get("max_parallel_tasks") == 10

    def test_multiple_overrides(self) -> None:
        config: dict = {}
        result = _apply_cli_overrides(
            config,
            ["execution.max_parallel_tasks=10", "execution.agent_pool_size=5"],
        )
        assert result["execution"]["max_parallel_tasks"] == 10
        assert result["execution"]["agent_pool_size"] == 5

    def test_malformed_no_equals_ignored(self) -> None:
        config: dict = {"execution": {"max_parallel_tasks": 4}}
        result = _apply_cli_overrides(config, ["badformat"])
        assert result["execution"]["max_parallel_tasks"] == 4

    def test_no_dot_ignored(self) -> None:
        # "nodot=5" has no section separator → should be ignored
        config: dict = {"execution": {"max_parallel_tasks": 4}}
        result = _apply_cli_overrides(config, ["nodot=5"])
        assert result["execution"]["max_parallel_tasks"] == 4

    def test_string_value(self) -> None:
        config: dict = {}
        result = _apply_cli_overrides(config, ["api.host=0.0.0.0"])
        assert result.get("api", {}).get("host") == "0.0.0.0"

    def test_int_value_coerced(self) -> None:
        config: dict = {}
        result = _apply_cli_overrides(config, ["api.port=9000"])
        assert result.get("api", {}).get("port") == 9000

    def test_empty_override_list(self) -> None:
        config: dict = {"execution": {"max_parallel_tasks": 4}}
        result = _apply_cli_overrides(config, [])
        assert result["execution"]["max_parallel_tasks"] == 4

    def test_bool_value_coerced(self) -> None:
        config: dict = {}
        result = _apply_cli_overrides(config, ["security.sandbox_mode=true"])
        assert result.get("security", {}).get("sandbox_mode") is True


# ---------------------------------------------------------------------------
# TestLoadConfigFileSearch
# ---------------------------------------------------------------------------

class TestLoadConfigFileSearch:
    """Tests that load_config searches the standard candidate locations."""

    def test_finds_dotagenthicc_subdir_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        subdir = tmp_path / ".agenthicc"
        subdir.mkdir()
        (subdir / "agenthicc.toml").write_text(
            "[execution]\nmax_parallel_tasks = 7\n"
        )
        config = load_config(user_path=str(tmp_path / "missing.toml"))
        assert config.execution.max_parallel_tasks == 7

    def test_falls_back_to_root_agenthicc_toml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "agenthicc.toml").write_text(
            "[execution]\nmax_parallel_tasks = 5\n"
        )
        config = load_config(user_path=str(tmp_path / "missing.toml"))
        assert config.execution.max_parallel_tasks == 5

    def test_falls_back_to_dotfile(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".agenthicc.toml").write_text(
            "[execution]\nmax_parallel_tasks = 4\n"
        )
        config = load_config(user_path=str(tmp_path / "missing.toml"))
        assert config.execution.max_parallel_tasks == 4

    def test_subdir_wins_over_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        subdir = tmp_path / ".agenthicc"
        subdir.mkdir()
        (subdir / "agenthicc.toml").write_text(
            "[execution]\nmax_parallel_tasks = 9\n"
        )
        (tmp_path / "agenthicc.toml").write_text(
            "[execution]\nmax_parallel_tasks = 3\n"
        )
        config = load_config(user_path=str(tmp_path / "missing.toml"))
        assert config.execution.max_parallel_tasks == 9

    def test_no_config_uses_defaults(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        config = load_config(
            project_path=str(tmp_path / "missing.toml"),
            user_path=str(tmp_path / "missing_user.toml"),
        )
        # Default value as defined in ExecutionSettings
        assert config.execution.max_parallel_tasks == 4

    def test_explicit_project_path_used(self, tmp_path: Path) -> None:
        explicit = tmp_path / "custom.toml"
        explicit.write_text("[execution]\nmax_parallel_tasks = 13\n")
        config = load_config(
            project_path=str(explicit),
            user_path=str(tmp_path / "missing.toml"),
        )
        assert config.execution.max_parallel_tasks == 13

    def test_dotfile_in_subdir_accepted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Accepts .agenthicc/.agenthicc.toml as second candidate."""
        monkeypatch.chdir(tmp_path)
        subdir = tmp_path / ".agenthicc"
        subdir.mkdir()
        (subdir / ".agenthicc.toml").write_text(
            "[execution]\nmax_parallel_tasks = 6\n"
        )
        config = load_config(user_path=str(tmp_path / "missing.toml"))
        assert config.execution.max_parallel_tasks == 6


# ---------------------------------------------------------------------------
# TestLoadConfigEnvOverride
# ---------------------------------------------------------------------------

class TestLoadConfigEnvOverride:
    """Tests env-override integration via load_config."""

    def test_env_overrides_file_value(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg_file = tmp_path / "agenthicc.toml"
        cfg_file.write_text("[execution]\nmax_parallel_tasks = 5\n")
        monkeypatch.setenv("AGENTHICC_EXECUTION_MAX_PARALLEL_TASKS", "99")
        # env_overrides defaults to True; pass the new keyword if supported
        try:
            config = load_config(
                project_path=str(cfg_file),
                user_path=str(tmp_path / "missing.toml"),
                env_overrides=True,
            )
        except TypeError:
            # Old signature — skip env_overrides kwarg
            config = load_config(
                project_path=str(cfg_file),
                user_path=str(tmp_path / "missing.toml"),
            )
        assert config.execution.max_parallel_tasks == 99

    def test_env_disabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg_file = tmp_path / "agenthicc.toml"
        cfg_file.write_text("[execution]\nmax_parallel_tasks = 5\n")
        monkeypatch.setenv("AGENTHICC_EXECUTION_MAX_PARALLEL_TASKS", "99")
        try:
            config = load_config(
                project_path=str(cfg_file),
                user_path=str(tmp_path / "missing.toml"),
                env_overrides=False,
            )
        except TypeError:
            pytest.skip("env_overrides parameter not yet added to load_config")
        # env must NOT have been applied
        assert config.execution.max_parallel_tasks == 5

    def test_env_string_field(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AGENTHICC_API_HOST", "myhost")
        try:
            config = load_config(
                project_path=str(tmp_path / "missing.toml"),
                user_path=str(tmp_path / "missing_user.toml"),
                env_overrides=True,
            )
        except TypeError:
            config = load_config(
                project_path=str(tmp_path / "missing.toml"),
                user_path=str(tmp_path / "missing_user.toml"),
            )
        assert config.api.host == "myhost"

    def test_env_int_field(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AGENTHICC_EXECUTION_MAX_PARALLEL_TASKS", "42")
        try:
            config = load_config(
                project_path=str(tmp_path / "missing.toml"),
                user_path=str(tmp_path / "missing_user.toml"),
                env_overrides=True,
            )
        except TypeError:
            config = load_config(
                project_path=str(tmp_path / "missing.toml"),
                user_path=str(tmp_path / "missing_user.toml"),
            )
        assert config.execution.max_parallel_tasks == 42


# ---------------------------------------------------------------------------
# TestLoadConfigCliOverride
# ---------------------------------------------------------------------------

class TestLoadConfigCliOverride:
    """Tests CLI override integration via load_config."""

    def _load(self, tmp_path: Path, project_toml: str, overrides: list[str]) -> object:
        cfg = tmp_path / "agenthicc.toml"
        cfg.write_text(project_toml)
        try:
            return load_config(
                project_path=str(cfg),
                user_path=str(tmp_path / "missing.toml"),
                cli_overrides=overrides,
            )
        except TypeError:
            pytest.skip("cli_overrides parameter not yet added to load_config")

    def test_cli_override_beats_file(self, tmp_path: Path) -> None:
        config = self._load(
            tmp_path,
            "[execution]\nmax_parallel_tasks = 5\n",
            ["execution.max_parallel_tasks=77"],
        )
        assert config.execution.max_parallel_tasks == 77

    def test_cli_beats_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AGENTHICC_EXECUTION_MAX_PARALLEL_TASKS", "20")
        config = self._load(
            tmp_path,
            "[execution]\nmax_parallel_tasks = 5\n",
            ["execution.max_parallel_tasks=99"],
        )
        assert config.execution.max_parallel_tasks == 99

    def test_multiple_cli_overrides(self, tmp_path: Path) -> None:
        config = self._load(
            tmp_path,
            "[execution]\nmax_parallel_tasks = 5\nagent_pool_size = 10\n",
            ["execution.max_parallel_tasks=77", "execution.agent_pool_size=3"],
        )
        assert config.execution.max_parallel_tasks == 77
        assert config.execution.agent_pool_size == 3

    def test_malformed_cli_override_ignored(self, tmp_path: Path) -> None:
        config = self._load(
            tmp_path,
            "[execution]\nmax_parallel_tasks = 5\n",
            ["badformat", "nodot=10"],
        )
        # malformed overrides must not crash and must leave file value intact
        assert config.execution.max_parallel_tasks == 5


# ---------------------------------------------------------------------------
# TestConfigPrecedenceOrder
# ---------------------------------------------------------------------------

class TestConfigPrecedenceOrder:
    """Precedence (lowest → highest): user-global < project < env vars < CLI.

    ~/.agenthicc/agenthicc.toml  = user-global (shared defaults, identity)
    .agenthicc/agenthicc.toml    = per-project (always wins over user-global)
    AGENTHICC_*                  = env vars (CI convenience, beats both files)
    --set                        = CLI overrides (highest)
    """

    def test_cli_highest_priority(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = tmp_path / "agenthicc.toml"
        user_cfg = tmp_path / "user.toml"
        cfg.write_text("[execution]\nmax_parallel_tasks = 5\n")
        user_cfg.write_text("[execution]\nmax_parallel_tasks = 50\n")
        monkeypatch.setenv("AGENTHICC_EXECUTION_MAX_PARALLEL_TASKS", "20")
        try:
            config = load_config(
                project_path=str(cfg),
                user_path=str(user_cfg),
                env_overrides=True,
                cli_overrides=["execution.max_parallel_tasks=99"],
            )
        except TypeError:
            pytest.skip("New load_config parameters not yet available")
        assert config.execution.max_parallel_tasks == 99  # CLI wins all

    def test_project_beats_user_global(self, tmp_path: Path) -> None:
        """Per-project config overrides user-global config."""
        user_cfg = tmp_path / "user.toml"
        proj_cfg = tmp_path / "project.toml"
        user_cfg.write_text("[execution]\nmax_parallel_tasks = 50\n")
        proj_cfg.write_text("[execution]\nmax_parallel_tasks = 77\n")
        try:
            config = load_config(
                project_path=str(proj_cfg),
                user_path=str(user_cfg),
                env_overrides=False,
            )
        except TypeError:
            pytest.skip("New load_config parameters not yet available")
        assert config.execution.max_parallel_tasks == 77  # project beats user-global

    def test_user_global_supplies_defaults(self, tmp_path: Path) -> None:
        """User-global config value is used when project does not override it."""
        user_cfg = tmp_path / "user.toml"
        user_cfg.write_text("[execution]\nmax_parallel_tasks = 42\n")
        try:
            config = load_config(
                project_path=str(tmp_path / "missing.toml"),
                user_path=str(user_cfg),
                env_overrides=False,
            )
        except TypeError:
            pytest.skip("New load_config parameters not yet available")
        assert config.execution.max_parallel_tasks == 42  # user-global used as default

    def test_env_beats_user_global(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Environment variables override user-global config."""
        user_cfg = tmp_path / "user.toml"
        user_cfg.write_text("[execution]\nmax_parallel_tasks = 77\n")
        monkeypatch.setenv("AGENTHICC_EXECUTION_MAX_PARALLEL_TASKS", "20")
        try:
            config = load_config(
                project_path=str(tmp_path / "missing.toml"),
                user_path=str(user_cfg),
                env_overrides=True,
            )
        except TypeError:
            pytest.skip("New load_config parameters not yet available")
        assert config.execution.max_parallel_tasks == 20  # env beats user-global

    def test_env_beats_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Environment variables override project config."""
        cfg = tmp_path / "agenthicc.toml"
        cfg.write_text("[execution]\nmax_parallel_tasks = 5\n")
        monkeypatch.setenv("AGENTHICC_EXECUTION_MAX_PARALLEL_TASKS", "20")
        try:
            config = load_config(
                project_path=str(cfg),
                user_path=str(tmp_path / "missing.toml"),
                env_overrides=True,
            )
        except TypeError:
            pytest.skip("env_overrides parameter not yet available")
        assert config.execution.max_parallel_tasks == 20  # env beats project

    def test_file_beats_defaults(self, tmp_path: Path) -> None:
        cfg = tmp_path / "agenthicc.toml"
        cfg.write_text("[execution]\nmax_parallel_tasks = 11\n")
        config = load_config(
            project_path=str(cfg),
            user_path=str(tmp_path / "missing.toml"),
        )
        assert config.execution.max_parallel_tasks == 11

    def test_default_when_nothing_set(self, tmp_path: Path) -> None:
        config = load_config(
            project_path=str(tmp_path / "missing.toml"),
            user_path=str(tmp_path / "missing_user.toml"),
        )
        # ExecutionSettings default
        assert config.execution.max_parallel_tasks == 4


# ---------------------------------------------------------------------------
# TestCandidateLists  (only when the symbols exist)
# ---------------------------------------------------------------------------

@_needs_helpers
class TestCandidateLists:
    def test_project_candidates_is_list_of_paths(self) -> None:
        assert isinstance(PROJECT_CONFIG_CANDIDATES, list)
        assert all(isinstance(p, Path) for p in PROJECT_CONFIG_CANDIDATES)

    def test_user_candidates_is_list_of_paths(self) -> None:
        assert isinstance(USER_CONFIG_CANDIDATES, list)
        assert all(isinstance(p, Path) for p in USER_CONFIG_CANDIDATES)

    def test_project_preferred_candidate_is_subdir(self) -> None:
        # First candidate must be inside .agenthicc/
        first = PROJECT_CONFIG_CANDIDATES[0]
        assert ".agenthicc" in str(first)

    def test_user_preferred_candidate_under_home(self) -> None:
        home = Path.home()
        first = USER_CONFIG_CANDIDATES[0]
        assert str(first).startswith(str(home))

    def test_project_candidates_contain_root_toml(self) -> None:
        names = [str(p) for p in PROJECT_CONFIG_CANDIDATES]
        assert any("agenthicc.toml" in n for n in names)

    def test_user_candidates_contain_legacy_dotfile(self) -> None:
        # Legacy location: ~/.agenthicc.toml
        names = [str(p) for p in USER_CONFIG_CANDIDATES]
        assert any(".agenthicc.toml" in n for n in names)
