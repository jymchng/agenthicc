---
title: "PRD-26: Agent-Scoped Tool Plugins — Per-Agent Tool Isolation and Naming"
status: draft
version: 0.1.0
created: 2026-06-12
depends-on: prd-24-tool-plugin-discovery.md, prd-25-tool-plugin-registration.md
---

# PRD-26: Agent-Scoped Tool Plugins

## Executive Summary

Project-wide plugin tools (PRD-24/25) are visible to every agent in the
session.  Sometimes a tool is only meaningful in the context of one specific
agent — a `web_scraper` for a *researcher* agent, a `citation_formatter` for a
*writer* agent.  This PRD defines:

1. **Agent naming** — how an agent gets a stable, human-readable name used as
   the directory key.
2. **Scoped discovery** — loading from `.agenthicc/agents/<name>/tools/*.py`.
3. **Precedence rules** — agent-scoped tools shadow project-wide tools with the
   same name, which shadow built-ins.
4. **Isolation guarantee** — an agent never receives tools scoped to a
   different agent.
5. **CLI and TOML configuration** — declaring named agents and their
   capabilities in `agenthicc.toml`.

---

## Goals

| ID | Goal |
|----|------|
| G1 | `.agenthicc/agents/<name>/tools/*.py` files are loaded only for the agent named `<name>` |
| G2 | `~/.agenthicc/agents/<name>/tools/*.py` provides user-global agent-scoped tools |
| G3 | Agent names are slugs: lowercase alphanumerics and hyphens (e.g. `researcher`, `sql-analyst`) |
| G4 | Agent-scoped tools have highest precedence: `agent > project > builtin` |
| G5 | The default agent (no name configured) uses the slug `"default"` |
| G6 | `agenthicc.toml` `[agents]` section lets users declare named agents and their system prompts |
| G7 | `/agents` slash command lists configured agents and their tool counts |
| G8 | Switching agents (future `/agent <name>`) loads that agent's scoped tools for subsequent turns |

## Non-Goals
- Multi-agent parallel execution (PRD-03 covers the workflow scheduler)
- Agent persistence / state between sessions (covered by memory layers PRD-05)
- Dynamic agent creation from the LLM itself

---

## Filesystem Layout

```
.agenthicc/
└── agents/
    ├── researcher/
    │   ├── system_prompt.md         # optional custom system prompt
    │   └── tools/
    │       ├── web_scraper.py       # only the "researcher" agent sees this
    │       └── arxiv_search.py
    │
    ├── sql-analyst/
    │   └── tools/
    │       ├── query_db.py
    │       └── explain_plan.py
    │
    └── writer/
        └── tools/
            ├── style_checker.py
            └── citation_formatter.py
```

---

## Agent Naming Convention

An **agent name** is the directory name under `.agenthicc/agents/`.  Rules:

- Lowercase letters, digits, and hyphens only: `[a-z0-9-]+`
- No leading or trailing hyphens
- Maximum 64 characters
- The reserved name `"default"` refers to the agent used when no specific agent
  is selected

Validation:

```python
# src/agenthicc/plugins/agent_config.py

import re

_SLUG_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
_MAX_LEN = 64


def validate_agent_name(name: str) -> str:
    """Return the normalised name or raise ValueError."""
    name = name.strip().lower()
    if not _SLUG_RE.match(name) or len(name) > _MAX_LEN:
        raise ValueError(
            f"Invalid agent name {name!r}. "
            "Use lowercase letters, digits, and hyphens only."
        )
    return name
```

---

## AgentDef — Per-Agent Configuration

```python
# src/agenthicc/plugins/agent_config.py  (continued)

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class AgentDef:
    """Configuration for a named agent."""

    name: str                                # validated slug
    system_prompt: str = ""                  # custom system prompt (may be empty)
    tool_plugin_paths: list[Path] = field(default_factory=list)  # discovered tool files

    @classmethod
    def from_directory(
        cls,
        agent_dir: Path,
        *,
        user_agent_dir: Path | None = None,
    ) -> "AgentDef":
        """Build an AgentDef by scanning the agent directory tree."""
        name = validate_agent_name(agent_dir.name)

        # System prompt: read system_prompt.md if present
        prompt = ""
        for base in filter(None, [user_agent_dir, agent_dir]):
            sp = base / "system_prompt.md"
            if sp.exists():
                prompt = sp.read_text(encoding="utf-8").strip()
                # project-local takes precedence over user-global

        return cls(name=name, system_prompt=prompt)


def discover_agents(
    project_dir: Path | None = None,
    user_dir: Path | None = None,
) -> dict[str, AgentDef]:
    """Scan for all named agent directories; return dict keyed by slug."""
    user_agents_root    = (user_dir    or Path.home() / ".agenthicc") / "agents"
    project_agents_root = (project_dir or Path(".agenthicc"))         / "agents"

    agents: dict[str, AgentDef] = {}

    for root in (user_agents_root, project_agents_root):
        if not root.is_dir():
            continue
        for entry in sorted(root.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            try:
                slug = validate_agent_name(entry.name)
            except ValueError:
                continue
            user_entry = user_agents_root / entry.name if root == project_agents_root else None
            agents[slug] = AgentDef.from_directory(entry, user_agent_dir=user_entry)

    return agents
```

---

## Tool Precedence (Full Stack)

```
Agent-scoped tools    .agenthicc/agents/<name>/tools/     ← highest
         +
~/.agenthicc/agents/<name>/tools/

Project-wide plugins  .agenthicc/tools/
         +
~/.agenthicc/tools/

Built-in tools        agenthicc.agent_tools.AGENT_TOOLS   ← lowest
```

`build_registry(agent_name=name, ...)` in PRD-25 already implements this order.

---

## `agenthicc.toml` Agent Declaration

Users can pre-declare agents in config to add metadata (display name, model
override, description) without needing a directory:

```toml
[agents.researcher]
description = "Deep research and web-scraping agent"
model = "claude-opus-4-8"
max_turns = 50

[agents.sql-analyst]
description = "Writes and explains SQL queries against the project database"
model = "claude-sonnet-4-6"
```

These entries are merged with any directories found under `.agenthicc/agents/`.
The directory scan always wins for `system_prompt` and tool discovery; TOML
provides supplementary metadata.

### `AgentsSettings` dataclass

```python
# src/agenthicc/config.py  (addition)

from dataclasses import dataclass, field


@dataclass
class AgentSettings:
    description: str = ""
    model: str = ""
    max_turns: int = 200


@dataclass
class AgentsSettings:
    """[agents] section — keyed by agent slug."""
    agents: dict[str, AgentSettings] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "AgentsSettings":
        return cls(agents={
            name: AgentSettings(**{
                k: v for k, v in cfg.items()
                if k in AgentSettings.__dataclass_fields__
            })
            for name, cfg in d.items()
        })
```

---

## Active Agent State

The session tracks which agent is currently active via `renderer._active_agent`:

```python
# In _run_tui_session() after renderer is created:
renderer._active_agent = "default"

# In _run_agent_turn(), read it:
_agent_name = getattr(renderer, "_active_agent", "default")
_registry = build_registry(
    agent_name=_agent_name,
    project_plugin_tools=getattr(renderer, "_project_plugin_tools", None),
)
```

---

## `/agents` Slash Command

`SlashCommandHandler._agents()` prints a table:

```
┌────────────────┬──────────────────────────────────────────┬────────────┐
│ Agent          │ Description                              │ Tools      │
├────────────────┼──────────────────────────────────────────┼────────────┤
│ default        │ General-purpose assistant                │ 11 builtin │
│ researcher  *  │ Deep research and web-scraping agent     │ 13 total   │
│ sql-analyst    │ SQL queries against the project database │ 12 total   │
└────────────────┴──────────────────────────────────────────┴────────────┘
  * = active agent
```

`*` marks the currently active agent.

---

## `/agent <name>` Slash Command (Future)

```
/agent researcher
# → switches renderer._active_agent to "researcher"
# → subsequent turns use researcher's scoped tools
# → prints confirmation: "Switched to agent: researcher (13 tools)"
```

For v1, the active agent is fixed at `"default"` for the session duration.
`/agent` switching is planned for v2.

---

## Supporting File: `system_prompt.md`

When `.agenthicc/agents/<name>/system_prompt.md` exists, its content replaces
(not appends to) the base system prompt for that agent.  If the file is empty
or absent, the default system prompt is used.

```python
# src/agenthicc/plugins/agent_config.py

def load_agent_system_prompt(
    agent_name: str,
    base_prompt: str,
    project_dir: Path | None = None,
    user_dir: Path | None = None,
) -> str:
    """Return the effective system prompt for a named agent."""
    project_root = (project_dir or Path(".agenthicc")) / "agents" / agent_name
    user_root    = (user_dir or Path.home() / ".agenthicc") / "agents" / agent_name

    for root in (project_root, user_root):  # project wins
        sp = root / "system_prompt.md"
        if sp.exists():
            content = sp.read_text(encoding="utf-8").strip()
            if content:
                return content

    return base_prompt
```

In `_run_agent_turn()`:

```python
from agenthicc.plugins.agent_config import load_agent_system_prompt

effective_system = load_agent_system_prompt(
    agent_name=_agent_name,
    base_prompt=BASE_SYSTEM_PROMPT + _registry.describe(),
)
```

---

## Tests

```python
# tests/unit/test_agent_config.py

import pytest
from pathlib import Path
from agenthicc.plugins.agent_config import validate_agent_name, discover_agents, AgentDef

pytestmark = pytest.mark.unit


def test_validate_agent_name_ok():
    assert validate_agent_name("researcher") == "researcher"
    assert validate_agent_name("sql-analyst") == "sql-analyst"
    assert validate_agent_name("r2d2") == "r2d2"


def test_validate_agent_name_rejects_uppercase():
    with pytest.raises(ValueError):
        validate_agent_name("Researcher")


def test_validate_agent_name_rejects_leading_hyphen():
    with pytest.raises(ValueError):
        validate_agent_name("-bad")


def test_validate_agent_name_rejects_spaces():
    with pytest.raises(ValueError):
        validate_agent_name("my agent")


def test_discover_agents_finds_directories(tmp_path):
    (tmp_path / "agents" / "researcher" / "tools").mkdir(parents=True)
    (tmp_path / "agents" / "writer" / "tools").mkdir(parents=True)
    agents = discover_agents(project_dir=tmp_path)
    assert "researcher" in agents
    assert "writer" in agents


def test_discover_agents_reads_system_prompt(tmp_path):
    agent_dir = tmp_path / "agents" / "researcher"
    agent_dir.mkdir(parents=True)
    (agent_dir / "system_prompt.md").write_text("You are a deep researcher.")
    agents = discover_agents(project_dir=tmp_path)
    assert agents["researcher"].system_prompt == "You are a deep researcher."


def test_discover_agents_skips_invalid_names(tmp_path):
    (tmp_path / "agents" / "Bad Agent").mkdir(parents=True)
    agents = discover_agents(project_dir=tmp_path)
    assert "Bad Agent" not in agents


def test_discover_agents_project_overrides_user(tmp_path):
    user_dir = tmp_path / "user"
    proj_dir = tmp_path / "proj"
    (user_dir / "agents" / "bot").mkdir(parents=True)
    (proj_dir / "agents" / "bot").mkdir(parents=True)
    (user_dir / "agents" / "bot" / "system_prompt.md").write_text("User prompt.")
    (proj_dir / "agents" / "bot" / "system_prompt.md").write_text("Project prompt.")
    agents = discover_agents(project_dir=proj_dir, user_dir=user_dir)
    assert agents["bot"].system_prompt == "Project prompt."
```

---

## Verification

```bash
# Set up a named agent with custom tools
mkdir -p .agenthicc/agents/researcher/tools

cat > .agenthicc/agents/researcher/system_prompt.md << 'EOF'
You are a deep research assistant. When asked to research a topic, always
search multiple sources and synthesise the findings with citations.
EOF

cat > .agenthicc/agents/researcher/tools/arxiv_search.py << 'EOF'
from lauren_ai._tools import tool

@tool()
async def search_arxiv(query: str, max_results: int = 5) -> list[dict]:
    """Search arXiv for academic papers matching a query."""
    # stub — real implementation uses httpx + arXiv API
    return [{"title": f"Paper about {query}", "id": "2401.00001"}]

TOOLS = [search_arxiv]
EOF

uv run agenthicc
# /agents          → shows researcher with 12 tools (11 builtin + 1 arxiv_search)
# "search arxiv for attention mechanisms" → agent calls search_arxiv(...)

# Verify a different agent does NOT see researcher's tools:
mkdir -p .agenthicc/agents/writer/tools
# (no tools here — writer only has builtins + project plugins)
# researcher's arxiv_search is NOT in writer's registry
```
