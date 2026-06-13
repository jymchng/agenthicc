---
title: "PRD-37: Slash-Command Argument Hints and Contextual Help"
status: draft
version: 0.1.0
created: 2026-06-12
depends-on: prd-36-slash-command-dropdown.md
---

# PRD-37: Slash-Command Argument Hints

## Context

PRD-36 adds a dropdown for discovering commands.  Once a command is selected
and the user is typing arguments, there is no guidance about what arguments are
expected.  This PRD specifies an **argument hint toolbar** that appears below
the `❯` prompt whenever the cursor is positioned after a known command:

```
❯ /model anthropic _
─────────────────────────────────────────────────────────
  openai/...  │  2 turns  │  $0.004
─────────────────────────────────────────────────────────
  ↑ /model [provider] [model]  — Show or switch LLM provider/model
```

The hint is rendered in the `bottom_toolbar` of the `PromptSession` and
updates as the user types.

---

## Goals

| ID | Goal |
|----|------|
| G1 | When the input starts with `/known-command`, show its argument hint in the toolbar |
| G2 | Argument hints are declared in `CommandSpec.argument_hint` (new field) |
| G3 | Skills show their argument hint from frontmatter `argument-hint` field |
| G4 | The toolbar format is: `↑ /command [arg1] [arg2]  — description` |
| G5 | When no command is active, the toolbar shows the normal session status |
| G6 | Hints update dynamically as the user types (no Enter needed) |
| G7 | Long hints are truncated to fit the terminal width |

---

## Files to Modify

1. **`src/agenthicc/tui/input_bar.py`** — `CommandSpec`, `InputBarSession`
2. **`src/agenthicc/skills/loader.py`** — `SkillDef` (read `argument-hint` from frontmatter)

---

## 1. `CommandSpec` — Add `argument_hint`

```python
@dataclass(frozen=True)
class CommandSpec:
    name: str
    description: str
    aliases: tuple[str, ...] = ()
    argument_hint: str = ""    # ← new field; e.g. "[provider] [model]"
```

Update `BUILTIN_COMMANDS`:

```python
BUILTIN_COMMANDS: list[CommandSpec] = [
    CommandSpec("/status",   "Show running agents and their tasks"),
    CommandSpec("/model",    "Show or switch LLM provider/model",
                argument_hint="[provider] [model]"),
    CommandSpec("/models",   "List all available LLM providers"),
    CommandSpec("/approve",  "Review and approve pending HITL tool calls"),
    CommandSpec("/history",  "Browse the event log (last 20 entries)"),
    CommandSpec("/settings", "View current configuration"),
    CommandSpec("/help",     "List available commands"),
    CommandSpec("/cancel",   "Cancel the currently running intent"),
    CommandSpec("/clear",    "Clear the transcript display"),
    CommandSpec("/skills",   "List available skills"),
    CommandSpec("/expand",   "Expand tool output or @mention",
                argument_hint="[tool-id-or-@path]"),
    CommandSpec("/mcp",      "Show MCP server status or connect a new server",
                argument_hint="[connect <url> [transport]]"),
]
```

---

## 2. Skills — Read `argument-hint` from SKILL.md Frontmatter

Extend `SkillDef` with `argument_hint: str = ""`.

In `_parse_skill()`:
```python
return SkillDef(
    ...
    argument_hint=str(meta.get("argument-hint", "")),
)
```

Example SKILL.md:
```yaml
---
name: "Git Summary"
description: "Summarise recent git activity"
argument-hint: "[format]"
---
```

When registering skills as `CommandSpec`:
```python
session.register_command(CommandSpec(
    name=f"/{skill.slug}",
    description=skill.description or skill.name,
    argument_hint=skill.argument_hint,
))
```

---

## 3. Dynamic Toolbar in `InputBarSession`

Replace the static `_toolbar()` callable with one that inspects the
current buffer content:

```python
class InputBarSession:
    def __init__(self, ...) -> None:
        ...
        self._command_index: dict[str, CommandSpec] = {
            cmd.name: cmd for cmd in (commands or BUILTIN_COMMANDS)
        }

    def _get_toolbar(self, status_fn) -> Any:
        """Return a callable toolbar that shows hints or session status."""
        from prompt_toolkit.formatted_text import FormattedText  # noqa: PLC0415

        def _toolbar() -> FormattedText:
            import shutil  # noqa: PLC0415
            cols = shutil.get_terminal_size((80, 24)).columns

            # Check current buffer for a slash command
            buf = self._session.default_buffer.text.lstrip()
            if buf.startswith("/"):
                first_token = buf.split()[0] if buf.split() else buf
                cmd = self._command_index.get(first_token)
                if cmd:
                    hint = f"/{cmd.name.lstrip('/')}"
                    if cmd.argument_hint:
                        hint += f" {cmd.argument_hint}"
                    hint += f"  — {cmd.description}"
                    hint = hint[:cols - 4]
                    return FormattedText([
                        ("class:bottom-toolbar", f"  ↑ {hint}"),
                    ])

            # Default: session status from caller
            return status_fn()

        return _toolbar

    async def prompt_async(self, prefix, status_fn=None) -> str:
        toolbar = self._get_toolbar(status_fn) if status_fn else None
        result = await self._session.prompt_async(
            prefix,
            bottom_toolbar=toolbar,
        )
        return result or ""
```

---

## 4. `InlineRenderer.run()` — pass status function

In `InlineRenderer.run()`, when calling `session.prompt_async()`, pass the
existing `_toolbar()` function as `status_fn`:

```python
def _toolbar_fn():
    import shutil  # noqa: PLC0415
    cols = shutil.get_terminal_size((80, 24)).columns
    s = self._status
    sid = s.session_id or "session"
    turns = s.completed_agents
    status = f"  {sid}  │  {turns} turn{'s' if turns != 1 else ''}  │  ${s.session_cost_usd:.3f}"
    from prompt_toolkit.formatted_text import FormattedText
    return FormattedText([("class:bottom-toolbar", "─" * cols + "\n" + status)])

text = await session.prompt_async(_prompt, status_fn=_toolbar_fn)
```

---

## Visual Result

```
❯ /model anthr_
─────────────────────────────────────────────────────────────────────
  ↑ /model [provider] [model]  — Show or switch LLM provider/model
```

When the input is cleared or doesn't start with `/`:
```
❯ _
─────────────────────────────────────────────────────────────────────
  openai/...  │  3 turns  │  $0.012
```

---

## Tests

```python
# tests/unit/test_slash_hints.py

import pytest
from agenthicc.tui.input_bar import CommandSpec, SlashCommandCompleter, BUILTIN_COMMANDS

pytestmark = pytest.mark.unit


def test_command_spec_has_argument_hint():
    cmd = CommandSpec("/model", "Switch model", argument_hint="[provider] [model]")
    assert cmd.argument_hint == "[provider] [model]"


def test_builtin_model_has_hint():
    model_cmd = next(c for c in BUILTIN_COMMANDS if c.name == "/model")
    assert model_cmd.argument_hint  # non-empty


def test_completion_display_includes_command_name():
    c = SlashCommandCompleter(BUILTIN_COMMANDS)
    from prompt_toolkit.document import Document
    from prompt_toolkit.completion import CompleteEvent
    comps = list(c.get_completions(Document("/mod"), CompleteEvent()))
    assert any("/model" in str(c.display) for c in comps)
```

---

## Verification

```bash
PYTHONPATH=src .venv/bin/pytest tests/unit/test_slash_hints.py -v

uv run agenthicc
# Type /model  → toolbar shows: ↑ /model [provider] [model] — Show or switch…
# Type /git-summary  → toolbar shows skill's argument-hint from SKILL.md
# Clear input  → toolbar shows normal session status again
```
