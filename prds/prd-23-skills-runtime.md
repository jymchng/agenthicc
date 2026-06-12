---
title: "PRD-23: Skills Runtime — Context Injection, Arguments, Auto-Triggering, and Supporting Files"
status: draft
version: 0.1.0
created: 2026-06-12
depends-on: prd-22-skills-system.md
---

# PRD-23: Skills Runtime

## Executive Summary

Once a skill is discovered and invoked (PRD-22), its `SKILL.md` body must be
processed before being sent to the agent.  This PRD covers the three runtime
transformations applied to skill content:

1. **Dynamic context injection** — `` !`shell command` `` placeholders are
   executed and their stdout is inserted inline.
2. **Argument substitution** — `{0}`, `{1}`, `{n}` and named placeholders
   like `{session}` and `{effort}` are replaced with runtime values.
3. **Auto-triggering** — skills whose description or `suggestedTopics` match
   the user's message are appended to the agent's system prompt automatically.

Additionally, skills may reference **supporting files** (`template.md`,
`reference.md`, `helper.py`) that are included or executed alongside the main
`SKILL.md` body.

---

## Goals

| ID | Goal |
|----|------|
| G1 | `` !`cmd` `` in SKILL.md body is executed synchronously; stdout replaces the placeholder |
| G2 | `{0}`, `{1}` … are replaced by positional arguments from `/skill-name arg0 arg1` |
| G3 | `{session}` is replaced by the current session ID |
| G4 | `{effort}` is replaced by the configured effort level (low / medium / high) |
| G5 | Skills with matching descriptions are appended to the agent system prompt automatically |
| G6 | `disallowAutoTriggering: true` fully prevents G5 for that skill |
| G7 | `template.md` in the skill directory is appended to the body when it exists |
| G8 | `reference.md` is appended only when `{reference}` appears in the body |
| G9 | `helper.py` is made available as an executable the agent can invoke |

## Non-Goals
- Sandboxed execution of `` !`cmd` `` (uses the same permissions as the user session)
- Recursive skill invocation (a skill cannot invoke another skill in v1)

---

## 1. Dynamic Context Injection

### Syntax

Any occurrence of `` !`<shell command>` `` in the SKILL.md body is treated as
a dynamic context block.  The command is executed before the skill body is sent
to the agent; its trimmed stdout replaces the backtick expression.

### Examples

```markdown
## Current git log
!`git log --oneline -10`

## Project structure
!`find . -name "*.py" | head -20`
```

Becomes (after injection):

```markdown
## Current git log
abc1234 fix login bug
def5678 add tests
...

## Project structure
./src/main.py
./src/auth.py
...
```

### Implementation

```python
# src/agenthicc/skills/runner.py

import re
import subprocess
import shlex
from pathlib import Path

_INJECT_RE = re.compile(r"!`([^`]+)`")


def _run_cmd(cmd: str, cwd: Path) -> str:
    """Execute a shell command and return its trimmed stdout."""
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=15,
        )
        return result.stdout.strip()
    except Exception as exc:
        return f"[context injection failed: {exc}]"


def inject_context(body: str, cwd: Path) -> str:
    """Replace all !`cmd` placeholders with live command output."""
    def _replace(m: re.Match) -> str:
        return _run_cmd(m.group(1), cwd)
    return _INJECT_RE.sub(_replace, body)
```

---

## 2. Argument Substitution

### Invocation Syntax

```
/skill-name                  # no arguments
/skill-name foo bar baz      # three positional arguments
```

### Placeholders

| Placeholder | Replaced with |
|-------------|---------------|
| `{0}` | First argument (or empty string if not provided) |
| `{1}` | Second argument |
| `{n}` | nth argument (0-indexed) |
| `{session}` | Current session UUID |
| `{effort}` | Effort level: `"low"`, `"medium"`, or `"high"` |

### Example

`SKILL.md`:
```markdown
Analyze the diff in {0} against the {1} branch.
Session: {session}
```

Invocation: `/analyze-diff src/ main`

Result sent to agent:
```
Analyze the diff in src/ against the main branch.
Session: 8f3a9c2d...
```

### Implementation

```python
# src/agenthicc/skills/runner.py  (continued)

import re


def substitute_args(
    body: str,
    args: list[str],
    session_id: str = "",
    effort: str = "medium",
) -> str:
    """Replace {n}, {session}, and {effort} placeholders."""
    # Named placeholders first
    body = body.replace("{session}", session_id)
    body = body.replace("{effort}", effort)

    # Positional: {0}, {1}, {2}, ...
    def _sub_positional(m: re.Match) -> str:
        idx = int(m.group(1))
        return args[idx] if idx < len(args) else ""

    body = re.sub(r"\{(\d+)\}", _sub_positional, body)
    return body
```

---

## 3. Supporting Files

### `template.md`
When `template.md` exists in the skill directory, it is appended to the
processed body with a `---` separator so the agent knows what output format
is expected:

```python
def load_template(skill_dir: Path) -> str:
    template = skill_dir / "template.md"
    return f"\n\n---\n\n{template.read_text(encoding='utf-8').strip()}" if template.exists() else ""
```

### `reference.md`
Appended only when the placeholder `{reference}` appears in the body:

```python
def maybe_load_reference(body: str, skill_dir: Path) -> str:
    if "{reference}" not in body:
        return body
    ref = skill_dir / "reference.md"
    if ref.exists():
        body = body.replace("{reference}", ref.read_text(encoding="utf-8").strip())
    else:
        body = body.replace("{reference}", "[reference.md not found]")
    return body
```

### `helper.py`
Not injected into the prompt.  The agent can run it via the `shell` / `run_python`
tools.  When the skill is invoked, agenthicc prints a note to the transcript:

```
  [dim]helper.py available at .agenthicc/skills/deploy/helper.py[/dim]
```

---

## 4. Auto-Triggering

When the agent is about to respond to a user message, skills whose description
or `suggestedTopics` semantically match the message are loaded and prepended to
the agent system prompt automatically.

### Simple matching (v1)

For v1, matching is **keyword-based**: any `suggestedTopics` keyword that
appears (case-insensitive) as a whole word in the user message triggers the skill.

```python
# src/agenthicc/skills/runner.py  (continued)

import re


def find_matching_skills(
    user_message: str,
    skills: dict[str, "SkillDef"],
) -> list["SkillDef"]:
    """Return skills whose suggestedTopics appear in the user message."""
    words = set(re.findall(r"\w+", user_message.lower()))
    matched: list["SkillDef"] = []
    for skill in skills.values():
        if skill.disallow_auto_triggering:
            continue
        for topic in skill.suggested_topics:
            if topic.lower() in words:
                matched.append(skill)
                break
    return matched
```

### Injecting into system prompt

In `_run_agent_turn()` in `__main__.py`, before calling `_active_runner.run()`:

```python
from agenthicc.skills.runner import find_matching_skills, process_skill_body

matched = find_matching_skills(text, renderer._skills or {})
if matched:
    skill_addenda = "\n\n".join(
        f"## Skill: {s.name}\n{process_skill_body(s, args=[], cwd=Path('.'))}"
        for s in matched
    )
    system_suffix = f"\n\n---\n\n{skill_addenda}"
else:
    system_suffix = ""
```

Then pass `system_suffix` when constructing `_AgenthiccAgent`:

```python
@agent_decorator(
    model=model_id,
    system=(BASE_SYSTEM_PROMPT + system_suffix),
)
@use_tools(*AGENT_TOOLS)
class _AgenthiccAgent: ...
```

---

## 5. Full Processing Pipeline

```python
# src/agenthicc/skills/runner.py  (continued)

from pathlib import Path


def process_skill_body(
    skill: "SkillDef",
    args: list[str],
    cwd: Path,
    session_id: str = "",
    effort: str = "medium",
) -> str:
    """Apply the full runtime transformation pipeline to a skill body."""
    body = skill.body                            # lazy-loads SKILL.md body
    body = inject_context(body, cwd=cwd)         # !`cmd` injection
    body = substitute_args(body, args, session_id=session_id, effort=effort)
    body = maybe_load_reference(body, skill.path)
    body += load_template(skill.path)            # append template if present
    return body
```

---

## 6. Invocation Flow (end-to-end)

```
User types: /deploy src/ production
              │
              ▼
SlashCommandHandler.handle()
  - slug = "deploy", args = ["src/", "production"]
  - skill = renderer._skills["deploy"]
  - body = process_skill_body(skill, args, cwd=Path("."), session_id=...)
  - submit body as the user's intent text
              │
              ▼
_run_agent_turn(text=body, ...)
  - agent sees the processed skill instructions
  - executes tools listed in skill.tools (if restricted)
```

---

## 7. Tests

```python
# tests/unit/test_skills_runner.py

import pytest
from pathlib import Path
from unittest.mock import patch

from agenthicc.skills.runner import inject_context, substitute_args, find_matching_skills
from agenthicc.skills.loader import SkillDef

pytestmark = pytest.mark.unit


def test_inject_context_replaces_cmd(tmp_path):
    body = "Current dir:\n!`echo hello`"
    result = inject_context(body, cwd=tmp_path)
    assert "hello" in result
    assert "!`" not in result


def test_inject_context_failed_cmd(tmp_path):
    body = "!`nonexistent_command_xyz_abc`"
    result = inject_context(body, cwd=tmp_path)
    assert "context injection failed" in result or result == ""


def test_substitute_positional_args():
    body = "Analyze {0} against {1}."
    result = substitute_args(body, ["src/", "main"])
    assert result == "Analyze src/ against main."


def test_substitute_missing_arg_gives_empty():
    body = "File: {0}, Branch: {1}"
    result = substitute_args(body, ["only_one"])
    assert result == "File: only_one, Branch: "


def test_substitute_session_and_effort():
    body = "session={session} effort={effort}"
    result = substitute_args(body, [], session_id="abc123", effort="high")
    assert result == "session=abc123 effort=high"


def _make_skill(slug: str, topics: list[str], disallow: bool = False) -> SkillDef:
    return SkillDef(
        name=slug, slug=slug, path=Path("."),
        suggested_topics=topics,
        disallow_auto_triggering=disallow,
    )


def test_find_matching_skills_by_topic():
    skills = {
        "deploy": _make_skill("deploy", ["deploy", "release"]),
        "debug": _make_skill("debug", ["debug", "fix"]),
    }
    matched = find_matching_skills("please deploy to production", skills)
    assert any(s.slug == "deploy" for s in matched)
    assert all(s.slug != "debug" for s in matched)


def test_disallow_auto_triggering_prevents_match():
    skills = {
        "secret": _make_skill("secret", ["deploy"], disallow=True),
    }
    matched = find_matching_skills("deploy now", skills)
    assert matched == []
```

---

## Verification

```bash
# Run tests
PYTHONPATH=src .venv/bin/pytest tests/unit/test_skills_loader.py \
                                 tests/unit/test_skills_runner.py -v

# Create a skill with context injection and args
mkdir -p .agenthicc/skills/git-summary
cat > .agenthicc/skills/git-summary/SKILL.md << 'EOF'
---
name: "Git Summary"
description: "Summarise recent git activity"
suggestedTopics: ["git", "commits", "log"]
---

## Recent commits
!`git log --oneline -10`

Summarise the last 10 commits in {0} format.
EOF

uv run agenthicc
# /git-summary bullet-points   → injects live git log, substitutes {0}="bullet-points"
# Type "show me the git history" → auto-triggers git-summary skill
```
