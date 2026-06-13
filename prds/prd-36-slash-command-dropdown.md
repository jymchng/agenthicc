---
title: "PRD-36: Slash-Command Dropdown — Discovery, Display, and Keyboard Navigation"
status: draft
version: 0.1.0
created: 2026-06-12
---

# PRD-36: Slash-Command Dropdown

## Context

Slash commands (`/model`, `/skills`, `/help`) and skills (`/git-summary`,
`/deploy`) are currently discoverable only by typing `/` and reading the
`/help` table output.  There is no live, navigable dropdown.  `prompt_toolkit`'s
existing `SlashCommandCompleter` already yields completions — this PRD
specifies the **visual style**, **keyboard navigation**, and **selection
behaviour** so the dropdown looks and feels like the design below:

```
❯ /
  /git-summary    Summarise recent git activity
  /deploy         Deploy the application to production
  /model          Show or switch LLM provider/model
  /models         List all available providers
  /status         Show agent turn table
  /history        Print last 20 transcript lines
  /expand         Expand tool call output
  /help           Show this help table
```

Pressing ↑/↓ highlights a row; Enter or Tab inserts the command name into the
input bar and positions the cursor after it so the user can type arguments.

---

## Goals

| ID | Goal |
|----|------|
| G1 | Typing `/` immediately opens the dropdown showing all commands + skills with descriptions |
| G2 | Typing more characters filters the list in real time (`/dep` → only `/deploy`) |
| G3 | ↑/↓ navigate, Enter or Tab selects and inserts into the input bar |
| G4 | Escape closes the dropdown without inserting |
| G5 | Each row shows `/<command>` left-aligned and description right-aligned in a dimmer colour |
| G6 | Skills discovered at session startup are included alongside built-in commands |
| G7 | The dropdown appears **below** the `❯` prompt line (same position as `@` file picker) |
| G8 | Mouse click on a row also selects it (prompt_toolkit default behaviour) |
| G9 | Row count is capped at 10 visible rows; the list is scrollable |

## Non-Goals
- Command argument hints (a separate tooltip overlay — future work)
- Remote command registry / plugin commands beyond local skills

---

## How It Works Today

`SlashCommandCompleter` in `src/agenthicc/tui/input_bar.py` already yields
`Completion` objects when the text ends with a `/`-prefixed word.
`InputBarSession` uses `CompleteStyle.MULTI_COLUMN` which shows a horizontal
multi-column menu — **not** a vertical single-column dropdown.

The two changes needed:
1. Switch `CompleteStyle` to a **single-column dropdown** style.
2. Add `display_meta` to each `Completion` so the description appears on the right.

---

## Files to Modify

**`src/agenthicc/tui/input_bar.py`** — `SlashCommandCompleter` + `InputBarSession`

---

## 1. Add `display_meta` to `SlashCommandCompleter`

```python
# src/agenthicc/tui/input_bar.py — SlashCommandCompleter.get_completions()

def get_completions(self, document, complete_event):
    text = document.text_before_cursor
    m = _SLASH_RE.search(text)
    if m is None:
        return
    partial = m.group(1)
    for cmd in self._commands:
        candidates = (cmd.name,) + cmd.aliases
        for candidate in candidates:
            if candidate.startswith(partial):
                yield Completion(
                    text=candidate[len(partial):],
                    start_position=0,
                    display=candidate,
                    display_meta=cmd.description,   # ← description on the right
                )
```

---

## 2. Switch to Single-Column Dropdown Style

```python
# src/agenthicc/tui/input_bar.py — InputBarSession.__init__()

from prompt_toolkit.shortcuts import CompleteStyle

self._session = PromptSession(
    completer=self._completer,
    complete_while_typing=True,
    complete_style=CompleteStyle.COLUMN,   # ← was MULTI_COLUMN; now vertical list
    key_bindings=kb,
    history=history,
    enable_history_search=True,
    prompt_continuation="  ",
)
```

`CompleteStyle.COLUMN` renders a single vertical column with the completion
text on the left and `display_meta` on the right — matching the target design.

---

## 3. Trigger Dropdown on `/` Alone

With `complete_while_typing=True`, the dropdown opens as soon as `/` is typed
and the `SlashCommandCompleter` returns at least one match.  No extra code
needed.

---

## 4. Cap Visible Rows + Scrolling

prompt_toolkit respects a `max_completion_items` setting on `PromptSession`:

```python
self._session = PromptSession(
    ...
    max_completion_items=10,   # ← cap visible rows (scrollable)
)
```

---

## 5. Styling the Dropdown

prompt_toolkit's completion menu can be styled via `Style`:

```python
from prompt_toolkit.styles import Style

_COMPLETION_STYLE = Style.from_dict({
    # completion menu background
    "completion-menu":                      "bg:#1e2030 fg:#cdd6f4",
    # selected row
    "completion-menu.completion.current":   "bg:#313244 fg:#cba6f7 bold",
    # the meta text (description)
    "completion-menu.meta.current":         "bg:#313244 fg:#6c7086",
    "completion-menu.meta":                 "bg:#1e2030 fg:#6c7086",
})
```

Pass this style to `PromptSession(style=...)` (merged with existing `_style`).

---

## 6. Skill Commands Registration (already wired via PRD-22)

Skills discovered at session startup are already registered as `CommandSpec`
objects via `session.register_command()` in `InlineRenderer.run()`:

```python
for slug, skill in getattr(self, "_skills", {}).items():
    session.register_command(CommandSpec(
        name=f"/{slug}",
        description=skill.description or skill.name,
    ))
```

These automatically appear in the dropdown with their descriptions.

---

## 7. Visual Design

```
❯ /dep
┌─────────────────────────────────────────────────────┐
│ /deploy    Deploy the application to production     │  ← highlighted
│ /depends   List package dependencies                │
└─────────────────────────────────────────────────────┘
```

After Enter/Tab:
```
❯ /deploy _
```

---

## Tests

```python
# tests/unit/test_slash_dropdown.py

import pytest
from prompt_toolkit.document import Document
from prompt_toolkit.completion import CompleteEvent
from agenthicc.tui.input_bar import SlashCommandCompleter, CommandSpec

pytestmark = pytest.mark.unit


def _make_completer():
    return SlashCommandCompleter([
        CommandSpec("/deploy",  "Deploy the application"),
        CommandSpec("/debug",   "Debug issues in your code"),
        CommandSpec("/status",  "Show agent turn table"),
    ])


def test_completion_has_display_meta():
    c = _make_completer()
    doc = Document("/dep")
    comps = list(c.get_completions(doc, CompleteEvent()))
    assert comps
    # Every completion should have display_meta
    for comp in comps:
        assert comp.display_meta is not None


def test_filter_by_prefix():
    c = _make_completer()
    doc = Document("/dep")
    comps = list(c.get_completions(doc, CompleteEvent()))
    names = [str(comp.display) for comp in comps]
    assert "/deploy" in names
    assert "/debug" in names
    assert "/status" not in names


def test_description_in_meta():
    c = _make_completer()
    doc = Document("/dep")
    comps = list(c.get_completions(doc, CompleteEvent()))
    deploy_comp = next(c for c in comps if str(c.display) == "/deploy")
    assert "Deploy" in str(deploy_comp.display_meta)


def test_empty_prefix_shows_all():
    c = _make_completer()
    doc = Document("/")
    comps = list(c.get_completions(doc, CompleteEvent()))
    names = {str(c.display) for c in comps}
    assert {"/deploy", "/debug", "/status"} == names


def test_no_match_returns_empty():
    c = _make_completer()
    doc = Document("/xyz")
    comps = list(c.get_completions(doc, CompleteEvent()))
    assert comps == []
```

---

## Verification

```bash
PYTHONPATH=src .venv/bin/pytest tests/unit/test_slash_dropdown.py -v

uv run agenthicc
# Type /  → dropdown opens showing all commands
# Type /dep → narrows to /deploy, /depends
# ↑/↓ → navigate rows; right column shows description
# Enter → inserts /deploy into input bar
# Esc → closes without inserting
```
