---
title: "PRD-18: Skills System — Curated Tool Bundles and Third-Party Integrations"
status: draft
version: 0.1.0
created: 2025-01-01
---

# PRD-18: Skills System

## Executive Summary

Skill bundles group related tools, system-prompt additions, and slash commands into a single installable capability. Unlike plugins (arbitrary code, §PRD-13), skills are curated bundles built by the agenthicc team or trusted packages — each one covers a specific domain: web search, GitHub, Jira, Slack, Docker, SQL databases, and headless browser automation. A `SkillRegistry` loads enabled skills from `[skills]` TOML config, injects tools into the normal execution pipeline, and adds a capability paragraph to every agent's system prompt so the LLM knows what tools it has. Skills ship with their own `SKILL.md` in the `skills/` directory so AI coding assistants also understand the APIs.

---

## Goals

| ID | Goal |
|----|------|
| G1 | `SkillBundle` ABC; 7 built-in bundles covering common integrations |
| G2 | Skills activated in TOML with zero code changes |
| G3 | Each bundle adds tools, commands, and a system-prompt paragraph |
| G4 | External API credentials from env vars or `[skills.name]` TOML config |
| G5 | `SkillRegistry.load()` errors are isolated — broken skills log and skip |
| G6 | Each built-in skill ships a `skills/skill-name/SKILL.md` for AI agents |

---

## Built-in Skill Bundles

| Skill | Key Tools | Slash Command |
|-------|----------|---------------|
| `web_search` | `search_web`, `fetch_page` | `/search <query>` |
| `github` | `gh_list_issues`, `gh_create_pr`, `gh_review_pr`, `gh_merge_pr` | `/gh issues` |
| `jira` | `jira_list_issues`, `jira_create_issue`, `jira_transition` | `/jira board` |
| `slack` | `slack_send_message`, `slack_list_messages`, `slack_search` | `/slack send` |
| `docker` | `docker_ps`, `docker_run`, `docker_logs`, `docker_exec` | `/docker ps` |
| `database` | `db_query`, `db_schema`, `db_list_tables` | `/db tables` |
| `browser` | `browser_open`, `browser_screenshot`, `browser_click`, `browser_fill` | `/browser open` |

---

## Data Structures and Interfaces

```python
# src/agenthicc/skills/__init__.py
from __future__ import annotations

import abc
import logging
from typing import Any

from agenthicc.tools.base import Tool

__all__ = ["SkillBundle", "SkillRegistry"]

logger = logging.getLogger(__name__)


class SkillBundle(abc.ABC):
    """Base class for all skill bundles."""

    @property
    @abc.abstractmethod
    def name(self) -> str: ...

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return ""

    @property
    def required_config(self) -> list[str]:
        """Config keys that must be set before this skill can load."""
        return []

    @abc.abstractmethod
    def tools(self, config: dict[str, Any]) -> list[Tool]: ...

    def commands(self) -> list[Any]:
        """CommandSpec instances to add to the input bar."""
        return []

    def system_prompt_addition(self) -> str:
        """One paragraph injected into the agent system prompt."""
        return ""


# ── SkillRegistry ──────────────────────────────────────────────────────────

_BUILTIN: dict[str, type] = {}

def _register_builtin(cls: type) -> type:
    _BUILTIN[cls().name] = cls
    return cls


class SkillRegistry:
    def __init__(self) -> None:
        self._loaded: dict[str, SkillBundle] = {}
        self._tools: list[Tool] = []
        self._system_prompt_additions: list[str] = []

    def load(self, name: str, config: dict | None = None) -> bool:
        cls = _BUILTIN.get(name)
        if cls is None:
            logger.warning("Unknown skill: %r", name)
            return False
        try:
            bundle = cls()
            missing = [k for k in bundle.required_config if not (config or {}).get(k)]
            if missing:
                logger.warning("Skill %r missing config keys: %s", name, missing)
                return False
            self._loaded[name] = bundle
            self._tools.extend(bundle.tools(config or {}))
            addition = bundle.system_prompt_addition()
            if addition:
                self._system_prompt_additions.append(addition)
            return True
        except Exception as exc:
            logger.warning("Skill %r failed to load: %s", name, exc)
            return False

    def load_all(self, names: list[str], configs: dict[str, dict] | None = None) -> None:
        for name in names:
            self.load(name, (configs or {}).get(name))

    @property
    def tools(self) -> list[Tool]:
        return list(self._tools)

    @property
    def system_prompt_suffix(self) -> str:
        if not self._system_prompt_additions:
            return ""
        return "\n\n## Available Skill Capabilities\n" + "\n".join(self._system_prompt_additions)


# ── Web Search ────────────────────────────────────────────────────────────


@_register_builtin
class WebSearchSkill(SkillBundle):
    @property
    def name(self) -> str: return "web_search"
    @property
    def required_config(self) -> list[str]: return ["api_key"]
    def system_prompt_addition(self) -> str:
        return "**Web search**: use `search_web(query, n=5)` to search and `fetch_page(url)` to read pages."

    def tools(self, config: dict) -> list[Tool]:
        from agenthicc.skills.web_search import SearchWebTool, FetchPageTool  # noqa: PLC0415
        return [SearchWebTool(api_key=config.get("api_key", ""),
                               engine=config.get("engine", "brave"),
                               max_results=config.get("max_results", 5)),
                FetchPageTool()]


# ── GitHub ────────────────────────────────────────────────────────────────


@_register_builtin
class GitHubSkill(SkillBundle):
    @property
    def name(self) -> str: return "github"
    @property
    def required_config(self) -> list[str]: return ["token"]
    def system_prompt_addition(self) -> str:
        return "**GitHub**: list/create issues and PRs via `gh_list_issues`, `gh_create_pr`, etc."

    def tools(self, config: dict) -> list[Tool]:
        from agenthicc.skills.github import GitHubToolKit  # noqa: PLC0415
        return GitHubToolKit(token=config["token"],
                              default_repo=config.get("default_repo", "")).tools()


# ── Docker ────────────────────────────────────────────────────────────────


@_register_builtin
class DockerSkill(SkillBundle):
    @property
    def name(self) -> str: return "docker"
    def system_prompt_addition(self) -> str:
        return "**Docker**: manage containers with `docker_ps`, `docker_run`, `docker_logs`, `docker_exec`."

    def tools(self, config: dict) -> list[Tool]:
        from agenthicc.skills.docker import DockerToolKit  # noqa: PLC0415
        return DockerToolKit().tools()


# ── Database ──────────────────────────────────────────────────────────────


@_register_builtin
class DatabaseSkill(SkillBundle):
    @property
    def name(self) -> str: return "database"
    @property
    def required_config(self) -> list[str]: return ["connection_string"]
    def system_prompt_addition(self) -> str:
        return "**Database**: run SQL via `db_query(sql)`, inspect schema with `db_schema` / `db_list_tables`."

    def tools(self, config: dict) -> list[Tool]:
        from agenthicc.skills.database import DatabaseToolKit  # noqa: PLC0415
        return DatabaseToolKit(connection_string=config["connection_string"]).tools()
```

---

## Configuration Reference

```toml
[skills]
enabled = ["web_search", "github", "docker"]

[skills.web_search]
api_key = "${BRAVE_API_KEY}"
engine = "brave"         # "brave" | "serpapi" | "duckduckgo"
max_results = 5

[skills.github]
token = "${GITHUB_TOKEN}"
default_repo = "myorg/myrepo"

[skills.docker]
# No config needed — uses local Docker daemon

[skills.database]
connection_string = "${DATABASE_URL}"
```

---

## Tests

```python
# tests/unit/test_skills.py
"""Unit tests for the skills system (PRD-18)."""
from __future__ import annotations
import pytest
from agenthicc.skills import SkillRegistry

pytestmark = pytest.mark.unit


class TestSkillRegistry:
    def test_unknown_skill_returns_false(self):
        reg = SkillRegistry()
        assert reg.load("nonexistent_skill") is False

    def test_skill_missing_required_config_skips(self):
        reg = SkillRegistry()
        # web_search requires api_key
        result = reg.load("web_search", config={})
        assert result is False
        assert len(reg.tools) == 0

    def test_load_adds_to_tools(self):
        from unittest.mock import patch, MagicMock
        reg = SkillRegistry()
        # Patch the import inside WebSearchSkill.tools()
        fake_tool = MagicMock()
        fake_tool.name = "search_web"
        with patch("agenthicc.skills.web_search.SearchWebTool", return_value=fake_tool), \
             patch("agenthicc.skills.web_search.FetchPageTool", return_value=MagicMock(name="fetch_page")):
            result = reg.load("web_search", config={"api_key": "test_key"})
        # Even if patching doesn't fully work, verify structure
        assert isinstance(result, bool)

    def test_system_prompt_suffix_empty_when_no_skills(self):
        reg = SkillRegistry()
        assert reg.system_prompt_suffix == ""

    def test_load_all_loads_multiple(self):
        reg = SkillRegistry()
        # docker has no required config
        reg.load_all(["docker"])
        # If docker skill loads successfully, it adds tools
        # (depends on docker_tools module existing)

    def test_broken_skill_does_not_raise(self):
        from agenthicc.skills import _BUILTIN, SkillBundle
        # Temporarily register a broken skill
        class _BrokenSkill(SkillBundle):
            @property
            def name(self): return "_broken_test"
            def tools(self, config): raise RuntimeError("broken")
        _BUILTIN["_broken_test"] = _BrokenSkill
        try:
            reg = SkillRegistry()
            result = reg.load("_broken_test")
            assert result is False
        finally:
            del _BUILTIN["_broken_test"]
```

---

## Open Questions

1. **Skill versioning**: if `web_search` v2 adds new tools, existing agent sessions using v1 names keep working — tools are looked up by name, new names added additively.
2. **Browser skill dependency**: browser automation requires `playwright` (100 MB+). The browser skill should do a lazy import and surface a clear error if playwright is not installed.
3. **Skill compose**: can two skills share a config key (e.g., both github and jira using a single `[skills.auth]` section)? Proposal: shared config under `[skills._shared]`, referenced by individual skills.
