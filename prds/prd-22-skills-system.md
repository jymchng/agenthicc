---
title: "PRD-22: Skills System — Custom Commands and Reusable Workflows"
status: draft
version: 0.1.0
created: 2026-06-12
---

# PRD-22: Skills System

## Executive Summary

Skills are agenthicc's primary extensibility mechanism for users and teams.
A **skill** is a directory containing a `SKILL.md` file with YAML frontmatter
and markdown instructions. Skills can be invoked explicitly with `/skill-name`
in the input bar, or loaded automatically by the agent when the conversation
context matches the skill's description.

Skills follow the [Agent Skills open standard](https://agentskills.io) for
cross-tool compatibility and live at two scopes: user-global
(`~/.agenthicc/skills/`) and project-local (`.agenthicc/skills/`).

---

## Goals

| ID | Goal |
|----|------|
| G1 | A skill is a directory named after the command; `SKILL.md` inside it defines the skill |
| G2 | YAML frontmatter in `SKILL.md` configures name, description, allowed tools, and behavior |
| G3 | Skills are discovered from `~/.agenthicc/skills/` (personal) and `.agenthicc/skills/` (project) |
| G4 | Project skills override personal skills with the same name |
| G5 | `/skill-name` in the input bar invokes a skill directly |
| G6 | `/skills` lists all available skills with their descriptions |
| G7 | Skills are lazy-loaded — only `SKILL.md` frontmatter is read at startup; full content loads on invocation |
| G8 | `disallowAutoTriggering: true` prevents a skill from being auto-loaded by the agent |

## Non-Goals
- GUI skill editor (out of scope for v1)
- Skill publishing / marketplace
- Enterprise-managed skills (future work)

---

## Skill Directory Structure

```
~/.agenthicc/skills/
└── deploy/
    ├── SKILL.md        ← required; contains frontmatter + instructions
    ├── template.md     ← optional; structured output template
    ├── sample.md       ← optional; example output
    └── reference.md    ← optional; detailed reference, loaded on demand

.agenthicc/skills/
└── code-review/
    └── SKILL.md
```

---

## SKILL.md Format

```markdown
---
name: "Deploy Application"
description: "Deploy the application to production with testing and verification"
author: "alice"
tags: ["deploy", "production"]
suggestedTopics: ["deploy", "ship", "release"]
disallowAutoTriggering: false
tools: ["Bash", "Read", "Write"]
disabledTools: []
maxTurnDepth: 10
model: "claude-sonnet-4-6"
---

# Deploy Application

Deploy this application to production:

1. Run `pytest` to verify code quality
2. Build the project
3. Deploy to the production server
4. Verify the deployment

Use `!`git log --oneline -5`` to see recent commits before deploying.
```

### Frontmatter Reference

| Field | Type | Default | Purpose |
|-------|------|---------|---------|
| `name` | string | directory name | Display name in `/skills` listing |
| `description` | string | `""` | What the skill does — injected into agent system prompt for auto-invocation (max 512 chars) |
| `author` | string | `""` | Skill creator |
| `tags` | list | `[]` | Categories for organizing skills |
| `suggestedTopics` | list | `[]` | Keywords that trigger auto-loading |
| `disallowAutoTriggering` | bool | `false` | When `true`, skill can only be invoked manually via `/skill-name` |
| `tools` | list | all | Tools the agent may use when this skill is active |
| `disabledTools` | list | `[]` | Tools blocked during skill execution |
| `maxTurnDepth` | int | `200` | Max agent turns for this skill |
| `model` | string | session default | Override model for this skill's execution |

---

## Data Structures

```python
# src/agenthicc/skills/loader.py

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class SkillDef:
    """Parsed representation of a single skill."""
    name: str                          # display name (frontmatter or dir name)
    slug: str                          # directory name used for /invocation
    path: Path                         # path to the skill directory
    description: str = ""
    author: str = ""
    tags: list[str] = field(default_factory=list)
    suggested_topics: list[str] = field(default_factory=list)
    disallow_auto_triggering: bool = False
    tools: list[str] = field(default_factory=list)   # empty = all allowed
    disabled_tools: list[str] = field(default_factory=list)
    max_turn_depth: int = 200
    model: str = ""
    # raw SKILL.md body (below frontmatter) — populated on first invocation
    _body: str | None = None

    @property
    def body(self) -> str:
        """Lazy-load the SKILL.md body on first access."""
        if self._body is None:
            skill_md = self.path / "SKILL.md"
            raw = skill_md.read_text(encoding="utf-8")
            # Strip frontmatter
            if raw.startswith("---"):
                parts = raw.split("---", 2)
                self._body = parts[2].strip() if len(parts) >= 3 else raw
            else:
                self._body = raw
        return self._body
```

```python
# src/agenthicc/skills/loader.py  (continued)

import yaml
from pathlib import Path


def _parse_skill(skill_dir: Path) -> SkillDef | None:
    """Parse a skill directory; return None if SKILL.md is missing or malformed."""
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return None
    try:
        raw = skill_md.read_text(encoding="utf-8")
        meta: dict[str, Any] = {}
        if raw.startswith("---"):
            parts = raw.split("---", 2)
            if len(parts) >= 3:
                meta = yaml.safe_load(parts[1]) or {}
        return SkillDef(
            name=str(meta.get("name", skill_dir.name)),
            slug=skill_dir.name,
            path=skill_dir,
            description=str(meta.get("description", "")),
            author=str(meta.get("author", "")),
            tags=list(meta.get("tags", [])),
            suggested_topics=list(meta.get("suggestedTopics", [])),
            disallow_auto_triggering=bool(meta.get("disallowAutoTriggering", False)),
            tools=list(meta.get("tools", [])),
            disabled_tools=list(meta.get("disabledTools", [])),
            max_turn_depth=int(meta.get("maxTurnDepth", 200)),
            model=str(meta.get("model", "")),
        )
    except Exception:
        return None


def discover_skills(
    project_dir: Path | None = None,
    user_dir: Path | None = None,
) -> dict[str, SkillDef]:
    """Discover and return all skills; project skills override personal ones.

    Returns a dict keyed by slug (directory name).
    """
    personal_root = (user_dir or Path.home() / ".agenthicc") / "skills"
    project_root = (project_dir or Path(".agenthicc")) / "skills"

    skills: dict[str, SkillDef] = {}

    for root in (personal_root, project_root):   # project overrides personal
        if not root.is_dir():
            continue
        for entry in sorted(root.iterdir()):
            if entry.is_dir() and not entry.name.startswith("."):
                skill = _parse_skill(entry)
                if skill is not None:
                    skills[skill.slug] = skill

    return skills
```

---

## Integration Points

### 1. `SlashCommandHandler` — `/skills` listing and `/skill-name` invocation

`src/agenthicc/tui/app.py` — `SlashCommandHandler.handle()`:

- `first == "/skills"` → print a Rich table of all skills (slug, name, description)
- `first.startswith("/")` and `first[1:] in known_skills` → invoke the skill:
  1. Load `skill.body` (lazy-reads SKILL.md body)
  2. Apply argument substitution and context injection (PRD-23)
  3. Submit the processed body as the agent's input text

`SlashCommandHandler` needs access to the live `dict[str, SkillDef]`; pass it via `renderer.skills` or a module-level singleton loaded at session startup.

### 2. Session startup — `_run_tui_session()` in `__main__.py`

```python
from agenthicc.skills.loader import discover_skills

_skills = discover_skills(
    project_dir=Path(".agenthicc"),
    user_dir=Path.home() / ".agenthicc",
)
renderer._skills = _skills
```

### 3. `input_bar.py` — slash-command completion

`SlashCommandCompleter` already yields completions for registered `CommandSpec` objects.
At startup, convert each `SkillDef` into a `CommandSpec` and register it:

```python
for skill in _skills.values():
    session.register_command(CommandSpec(
        name=f"/{skill.slug}",
        description=skill.description or skill.name,
    ))
```

### 4. `SLASH_HELP` and `MENU_COMMANDS` dicts in `app.py`

Dynamically extend both dicts with discovered skills so `/help` and tab completion include them.

---

## Hot Reloading

Changes to existing `SKILL.md` files take effect on the next invocation because `SkillDef.body` is lazy-loaded on each call.  Adding a brand-new top-level `skills/` directory requires restarting agenthicc (the `discover_skills()` call only runs at session startup).

---

## Tests

```python
# tests/unit/test_skills_loader.py

import pytest
from pathlib import Path
from agenthicc.skills.loader import discover_skills, _parse_skill

pytestmark = pytest.mark.unit


def test_parse_skill_with_frontmatter(tmp_path):
    d = tmp_path / "my-skill"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\nname: My Skill\ndescription: Does things\n---\n# Instructions\n"
    )
    skill = _parse_skill(d)
    assert skill is not None
    assert skill.name == "My Skill"
    assert skill.slug == "my-skill"
    assert skill.description == "Does things"


def test_parse_skill_without_frontmatter(tmp_path):
    d = tmp_path / "bare"
    d.mkdir()
    (d / "SKILL.md").write_text("# Just instructions\n")
    skill = _parse_skill(d)
    assert skill is not None
    assert skill.slug == "bare"
    assert skill.name == "bare"   # falls back to dir name


def test_parse_skill_missing_skill_md(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    assert _parse_skill(d) is None


def test_discover_project_overrides_personal(tmp_path):
    personal = tmp_path / "personal" / "skills" / "deploy"
    project = tmp_path / "project" / "skills" / "deploy"
    personal.mkdir(parents=True)
    project.mkdir(parents=True)
    (personal / "SKILL.md").write_text("---\nname: Personal Deploy\n---\n")
    (project / "SKILL.md").write_text("---\nname: Project Deploy\n---\n")

    skills = discover_skills(
        project_dir=tmp_path / "project" / "..",   # project root
        user_dir=tmp_path / "personal",
    )
    # project skills take precedence
    # (depends on discover_skills using project_dir/.agenthicc/skills)
    assert "deploy" in skills


def test_lazy_body_loading(tmp_path):
    d = tmp_path / "lazy"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: Lazy\n---\n# Do things\n")
    skill = _parse_skill(d)
    assert skill._body is None          # not loaded yet
    body = skill.body
    assert "Do things" in body
    assert skill._body is not None      # now cached
```

---

## Verification

```bash
# Install
pip install -e .

# Create a test skill
mkdir -p .agenthicc/skills/hello
cat > .agenthicc/skills/hello/SKILL.md << 'EOF'
---
name: "Hello World"
description: "Greet the user"
---
Say hello to the user and ask what they'd like to do today.
EOF

# Run agenthicc
uv run agenthicc

# In the TUI:
# /skills          → shows "hello" in the table
# /hello           → agent receives the skill body and greets the user
# tab after /hel   → auto-completes to /hello
```
