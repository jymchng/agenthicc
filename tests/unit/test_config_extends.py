"""Tests for configuration inheritance via extends (PRD-113)."""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest

from agenthicc.config import (
    ConfigExtendsCycleError,
    _resolve_extends,
    _load_toml_with_extends,
    load_config,
)


# ── helpers ───────────────────────────────────────────────────────────────────


def write_toml(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content), encoding="utf-8")
    return path


# ── _resolve_extends — basic inheritance ──────────────────────────────────────


@pytest.mark.unit
def test_resolve_no_extends_returns_file_as_is(tmp_path: Path) -> None:
    f = write_toml(
        tmp_path / "base.toml",
        """
        [execution]
        model = "claude-opus"
    """,
    )
    data = _resolve_extends(f)
    assert data["execution"]["model"] == "claude-opus"
    assert "extends" not in data


@pytest.mark.unit
def test_resolve_child_overrides_parent(tmp_path: Path) -> None:
    write_toml(
        tmp_path / "base.toml",
        """
        [execution]
        model = "claude-opus"
        provider = "anthropic"
    """,
    )
    child = write_toml(
        tmp_path / "dev.toml",
        """
        extends = "base.toml"

        [execution]
        model = "claude-haiku"
    """,
    )
    data = _resolve_extends(child)
    assert data["execution"]["model"] == "claude-haiku"  # overridden
    assert data["execution"]["provider"] == "anthropic"  # inherited
    assert "extends" not in data


@pytest.mark.unit
def test_resolve_extends_stripped_from_result(tmp_path: Path) -> None:
    write_toml(tmp_path / "base.toml", "[execution]\nmodel = 'x'\n")
    child = write_toml(tmp_path / "child.toml", "extends = 'base.toml'\n")
    data = _resolve_extends(child)
    assert "extends" not in data


@pytest.mark.unit
def test_resolve_list_of_parents_merged_left_to_right(tmp_path: Path) -> None:
    write_toml(tmp_path / "a.toml", "[execution]\nmodel = 'model-a'\nprovider = 'openai'\n")
    write_toml(tmp_path / "b.toml", "[execution]\nmodel = 'model-b'\n")
    child = write_toml(tmp_path / "child.toml", 'extends = ["a.toml", "b.toml"]\n')
    data = _resolve_extends(child)
    assert data["execution"]["model"] == "model-b"  # b overrides a
    assert data["execution"]["provider"] == "openai"  # from a, not overridden by b


@pytest.mark.unit
def test_resolve_child_wins_over_all_parents(tmp_path: Path) -> None:
    write_toml(tmp_path / "a.toml", "[execution]\nmodel = 'model-a'\n")
    write_toml(tmp_path / "b.toml", "[execution]\nmodel = 'model-b'\n")
    child = write_toml(
        tmp_path / "child.toml",
        'extends = ["a.toml", "b.toml"]\n[execution]\nmodel = "model-child"\n',
    )
    data = _resolve_extends(child)
    assert data["execution"]["model"] == "model-child"


# ── deep merge through extends ────────────────────────────────────────────────


@pytest.mark.unit
def test_resolve_deep_merge_does_not_wipe_parent_keys(tmp_path: Path) -> None:
    write_toml(
        tmp_path / "base.toml",
        """
        [tools]
        http_timeout_s = 30
        max_live_tool_calls = 5
    """,
    )
    child = write_toml(
        tmp_path / "dev.toml",
        """
        extends = "base.toml"

        [tools]
        http_timeout_s = 60
    """,
    )
    data = _resolve_extends(child)
    assert data["tools"]["http_timeout_s"] == 60  # overridden
    assert data["tools"]["max_live_tool_calls"] == 5  # inherited


# ── chained extends ───────────────────────────────────────────────────────────


@pytest.mark.unit
def test_resolve_chained_extends(tmp_path: Path) -> None:
    write_toml(tmp_path / "grandparent.toml", "[execution]\nprovider = 'anthropic'\n")
    write_toml(
        tmp_path / "parent.toml",
        "extends = 'grandparent.toml'\n[execution]\nmodel = 'claude-opus'\n",
    )
    child = write_toml(
        tmp_path / "child.toml", "extends = 'parent.toml'\n[execution]\nmodel = 'claude-haiku'\n"
    )
    data = _resolve_extends(child)
    assert data["execution"]["provider"] == "anthropic"  # from grandparent
    assert data["execution"]["model"] == "claude-haiku"  # overridden at child


# ── relative path resolution ──────────────────────────────────────────────────


@pytest.mark.unit
def test_resolve_path_relative_to_file_not_cwd(tmp_path: Path) -> None:
    subdir = tmp_path / "sub"
    subdir.mkdir()
    write_toml(subdir / "base.toml", "[execution]\nmodel = 'base-model'\n")
    child = write_toml(subdir / "child.toml", "extends = 'base.toml'\n")
    # Run from tmp_path (not subdir) — path must be relative to child.toml, not cwd
    orig = os.getcwd()
    try:
        os.chdir(tmp_path)
        data = _resolve_extends(child)
        assert data["execution"]["model"] == "base-model"
    finally:
        os.chdir(orig)


@pytest.mark.unit
def test_resolve_parent_in_parent_directory(tmp_path: Path) -> None:
    write_toml(tmp_path / "base.toml", "[execution]\nmodel = 'shared'\n")
    subdir = tmp_path / "envs"
    subdir.mkdir()
    child = write_toml(subdir / "dev.toml", "extends = '../base.toml'\n")
    data = _resolve_extends(child)
    assert data["execution"]["model"] == "shared"


# ── cycle detection ───────────────────────────────────────────────────────────


@pytest.mark.unit
def test_resolve_self_cycle_raises(tmp_path: Path) -> None:
    f = write_toml(tmp_path / "self.toml", "extends = 'self.toml'\n")
    with pytest.raises(ConfigExtendsCycleError):
        _resolve_extends(f)


@pytest.mark.unit
def test_resolve_mutual_cycle_raises(tmp_path: Path) -> None:
    write_toml(tmp_path / "a.toml", "extends = 'b.toml'\n")
    write_toml(tmp_path / "b.toml", "extends = 'a.toml'\n")
    with pytest.raises(ConfigExtendsCycleError):
        _resolve_extends(tmp_path / "a.toml")


@pytest.mark.unit
def test_resolve_three_node_cycle_raises(tmp_path: Path) -> None:
    write_toml(tmp_path / "a.toml", "extends = 'b.toml'\n")
    write_toml(tmp_path / "b.toml", "extends = 'c.toml'\n")
    write_toml(tmp_path / "c.toml", "extends = 'a.toml'\n")
    with pytest.raises(ConfigExtendsCycleError):
        _resolve_extends(tmp_path / "a.toml")


# ── missing parent error ──────────────────────────────────────────────────────


@pytest.mark.unit
def test_resolve_missing_parent_raises_file_not_found(tmp_path: Path) -> None:
    child = write_toml(tmp_path / "child.toml", "extends = 'nonexistent.toml'\n")
    with pytest.raises(FileNotFoundError, match="nonexistent.toml"):
        _resolve_extends(child)


# ── _load_toml_with_extends — safe variant ────────────────────────────────────


@pytest.mark.unit
def test_load_with_extends_safe_returns_empty_for_missing_file(tmp_path: Path) -> None:
    result = _load_toml_with_extends(tmp_path / "does_not_exist.toml")
    assert result == {}


@pytest.mark.unit
def test_load_with_extends_safe_resolves_chain(tmp_path: Path) -> None:
    write_toml(tmp_path / "base.toml", "[execution]\nmodel = 'x'\n")
    child = write_toml(tmp_path / "child.toml", "extends = 'base.toml'\n")
    result = _load_toml_with_extends(child)
    assert result["execution"]["model"] == "x"


@pytest.mark.unit
def test_load_with_extends_safe_propagates_cycle_error(tmp_path: Path) -> None:
    f = write_toml(tmp_path / "loop.toml", "extends = 'loop.toml'\n")
    with pytest.raises(ConfigExtendsCycleError):
        _load_toml_with_extends(f)


# ── load_config integration ───────────────────────────────────────────────────


@pytest.mark.unit
def test_load_config_config_path_overrides_project_file(tmp_path: Path) -> None:
    write_toml(tmp_path / "base.toml", "[execution]\nmodel = 'base-model'\n")
    dev = write_toml(
        tmp_path / "dev.toml", "extends = 'base.toml'\n[execution]\nmodel = 'dev-model'\n"
    )
    cfg = load_config(config_path=dev, env_overrides=False)
    assert cfg.execution.model == "dev-model"


@pytest.mark.unit
def test_load_config_config_path_inherits_from_parent(tmp_path: Path) -> None:
    write_toml(tmp_path / "base.toml", "[execution]\nprovider = 'anthropic'\nmodel = 'base'\n")
    dev = write_toml(tmp_path / "dev.toml", "extends = 'base.toml'\n[execution]\nmodel = 'haiku'\n")
    cfg = load_config(config_path=dev, env_overrides=False)
    assert cfg.execution.provider == "anthropic"  # from base
    assert cfg.execution.model == "haiku"  # from dev


@pytest.mark.unit
def test_load_config_env_var_agenthicc_config(tmp_path: Path, monkeypatch) -> None:
    dev = write_toml(tmp_path / "dev.toml", "[execution]\nmodel = 'env-model'\n")
    monkeypatch.setenv("AGENTHICC_CONFIG", str(dev))
    cfg = load_config(env_overrides=True)
    assert cfg.execution.model == "env-model"


@pytest.mark.unit
def test_load_config_explicit_config_path_beats_env_var(tmp_path, monkeypatch) -> None:
    env_file = write_toml(tmp_path / "env.toml", "[execution]\nmodel = 'env'\n")
    flag_file = write_toml(tmp_path / "flag.toml", "[execution]\nmodel = 'flag'\n")
    monkeypatch.setenv("AGENTHICC_CONFIG", str(env_file))
    cfg = load_config(config_path=flag_file, env_overrides=True)
    assert cfg.execution.model == "flag"  # --config wins


@pytest.mark.unit
def test_load_config_extends_key_not_in_final_config(tmp_path: Path) -> None:
    write_toml(tmp_path / "base.toml", "[execution]\nmodel = 'x'\n")
    dev = write_toml(tmp_path / "dev.toml", "extends = 'base.toml'\n")
    cfg = load_config(config_path=dev, env_overrides=False)
    # AgenthiccConfig must not have any 'extends' artifact
    assert not hasattr(cfg, "extends")
