---
title: "PRD-34: @Mention UI — Autocomplete Polish, Transcript Display, and URL Fetching"
status: draft
version: 0.1.0
created: 2026-06-12
depends-on: prd-32-at-mention-parser.md, prd-33-at-mention-content-injection.md
---

# PRD-34: @Mention UI and Transcript Display

## Context

The autocomplete dropdown (`AtMentionCompleter`) already works.
This PRD covers the remaining UI surfaces:
1. **Autocomplete polish** — show file type icons, sizes, and kind hints in the dropdown.
2. **Transcript display** — after a message with @mentions is submitted, show
   the resolved files as collapsible chips in the transcript (not raw XML blocks).
3. **URL fetching UX** — `@https://…` triggers a spinner, shows domain name.
4. **Inline error chips** — `@missing.txt (not found)` shown inline in red.
5. **`/expand @path`** — slash command to toggle expanded view of an injected file.

---

## Goals

| ID | Goal |
|----|------|
| G1 | Autocomplete shows file size and extension hint in the dropdown `display_meta` |
| G2 | After submission, injected files are shown as `  ⎿ @auth.py  1.2 KB  ✓` chips in the transcript |
| G3 | URL mentions show `  ⎿ @https://…  fetching…` then `  ✓  2.3 KB` on completion |
| G4 | Unresolved mentions show `  ⎿ @ghost.txt  ✗  not found` in red |
| G5 | `/expand @path` or `/expand @glob` toggles the full injected content block in the transcript |
| G6 | The `@mention` tokens in the user message displayed in the transcript are styled (bold/cyan) |
| G7 | Glob expansions show `  ⎿ @src/**/*.py  → 12 files` then the expanded list |

---

## 1. Autocomplete Polish — `AtMentionCompleter`

### Current state
The completer returns `Completion(text=remaining, display=f"@{path}")` with no metadata.

### Update: add `display_meta`

```python
# src/agenthicc/tui/input_bar.py — AtMentionCompleter.get_completions()

import stat as _stat

def _entry_meta(entry: Path) -> str:
    """Return a short metadata string for the dropdown right column."""
    try:
        s = entry.stat()
        if entry.is_dir():
            return "dir"
        size_kb = s.st_size / 1024
        if size_kb < 1:
            return f"{s.st_size} B"
        elif size_kb < 1024:
            return f"{size_kb:.0f} KB"
        else:
            return f"{size_kb / 1024:.1f} MB"
    except OSError:
        return ""

# In yield Completion(...):
yield Completion(
    text=remaining,
    start_position=0,
    display=f"@{display_path}",
    display_meta=_entry_meta(entry),   # ← new
)
```

### Update: show type icons in display

```python
def _entry_icon(entry: Path) -> str:
    if entry.is_dir():
        return "📁 "
    ext = entry.suffix.lower()
    icons = {
        ".py": "🐍 ", ".js": "📜 ", ".ts": "📜 ", ".md": "📝 ",
        ".json": "🔧 ", ".toml": "🔧 ", ".yaml": "🔧 ", ".yml": "🔧 ",
        ".png": "🖼 ", ".jpg": "🖼 ", ".gif": "🖼 ",
        ".sh": "⚙ ", ".txt": "📄 ",
    }
    return icons.get(ext, "📄 ")

# display=f"{_entry_icon(entry)}@{display_path}"
```

---

## 2. Transcript Chips — `TranscriptModel` + `_flush_new_lines`

After `build_context_prefix` resolves the mentions, the `InjectedContent` list
is passed to the transcript model so it can render chips.

### New `add_mention_chip()` method on `TranscriptModel`

```python
# src/agenthicc/tui/transcript.py

@dataclass
class MentionChip:
    """Compact representation of a resolved @mention for transcript display."""
    raw: str                # "@src/auth.py"
    kind: str               # "file" | "dir" | "glob" | "url" | "unresolved"
    display_size: str       # "1.2 KB" or "12 files" or ""
    ok: bool                # False → red error chip
    error: str | None = None
    expanded: bool = False  # toggle via /expand


class TranscriptModel:
    ...
    def add_mention_chips(
        self, agent_id: str, chips: list[MentionChip]
    ) -> None:
        turn = self._turn_for(agent_id)
        if not hasattr(turn, "mention_chips"):
            turn.mention_chips = []
        turn.mention_chips.extend(chips)
```

### Rendering chips in `AgentTurnEntry.render()`

Before the first tool call line, render mention chips:

```python
# In TranscriptModel.render():
for tc in turn.tool_calls:
    ...

# NEW: render mention chips if present
for chip in getattr(turn, "mention_chips", []):
    if chip.ok:
        meta = f"  [dim]{chip.display_size}[/dim]" if chip.display_size else ""
        line = (
            f"  [dim]⎿[/dim] [bold cyan]{chip.raw}[/bold cyan]"
            f"  [green]✓[/green]{meta}"
        )
    else:
        line = (
            f"  [dim]⎿[/dim] [bold red]{chip.raw}[/bold red]"
            f"  [red]✗[/red]  [dim]{chip.error}[/dim]"
        )
    out.append(line)
```

---

## 3. URL Mention UX

URL mentions show a spinner while fetching, then update on completion:

```python
# In _run_agent_turn(), after build_context_prefix resolves:
for r in _injected:
    if r.mention.kind == MentionKind.URL:
        chip = MentionChip(
            raw=r.mention.raw,
            kind="url",
            display_size=f"{r.chars_used:,} chars" if r.ok else "",
            ok=r.ok,
            error=r.error,
        )
    elif r.mention.kind == MentionKind.FILE:
        size_kb = r.chars_used / 1024
        chip = MentionChip(
            raw=r.mention.raw,
            kind="file",
            display_size=f"{size_kb:.1f} KB",
            ok=r.ok,
            error=r.error,
        )
    elif r.mention.kind == MentionKind.UNRESOLVED:
        chip = MentionChip(
            raw=r.mention.raw,
            kind="unresolved",
            display_size="",
            ok=False,
            error="not found",
        )
    elif r.mention.kind == MentionKind.GLOB:
        count = r.block.count("<file ")
        chip = MentionChip(
            raw=r.mention.raw,
            kind="glob",
            display_size=f"→ {count} file{'s' if count != 1 else ''}",
            ok=r.ok,
            error=r.error,
        )
    else:
        chip = MentionChip(
            raw=r.mention.raw, kind=r.mention.kind.value,
            display_size="", ok=r.ok, error=r.error,
        )
    transcript.add_mention_chips(agent_id, [chip])
```

---

## 4. `/expand @path` Slash Command

Extend `SlashCommandHandler._expand()` to also toggle mention chips:

```python
def _expand(self, cmd: str, model: TranscriptModel, console: Any) -> None:
    parts = cmd.split()
    prefix = parts[1] if len(parts) > 1 else ""
    found = 0

    # Existing: expand tool call output
    for turn in model.turns:
        for tc in turn.tool_calls:
            if not prefix or tc.tool_use_id.startswith(prefix):
                tc.expanded = True
                found += 1

    # NEW: expand @mention chips
    if prefix.startswith("@"):
        for turn in model.turns:
            for chip in getattr(turn, "mention_chips", []):
                if chip.raw.startswith(prefix) or chip.raw == prefix:
                    chip.expanded = True
                    found += 1

    if found:
        console.print(f"[dim]Expanded {found} item{'s' if found > 1 else ''}.[/dim]")
    else:
        console.print(f"[dim]No item found matching {prefix!r}[/dim]")
```

When `chip.expanded = True`, the render shows the full injected block
(stored separately in the turn):

```python
# Extended render for expanded chips:
if chip.expanded and hasattr(turn, "mention_content"):
    content = turn.mention_content.get(chip.raw, "")
    if content:
        for ln in content.splitlines()[:50]:
            out.append(f"    [dim]{ln[:120]}[/dim]")
        if len(content.splitlines()) > 50:
            out.append(f"    [dim](… /expand {chip.raw} to see less)[/dim]")
```

---

## 5. Styled @mentions in Transcript

In `_run_agent_turn()`, when appending the user's original message to the
transcript, highlight `@mention` tokens:

```python
from agenthicc.mentions.parser import parse_mentions  # noqa: PLC0415

def _style_mentions(text: str, mentions) -> str:
    """Wrap @mention tokens in Rich cyan markup for transcript display."""
    result = text
    for m in sorted(mentions, key=lambda x: x.start, reverse=True):
        styled = f"[bold cyan]{m.raw}[/bold cyan]"
        result = result[: m.start] + styled + result[m.end :]
    return result

# When appending the user message to transcript:
styled_text = _style_mentions(text, _injected_mentions) if _injected else text
```

---

## Updated `SLASH_HELP`

```python
SLASH_HELP = {
    ...
    "/expand":  "Expand tool output or @mention file  (/expand abc12345 or /expand @src/auth.py)",
    ...
}
```

---

## Tests

```python
# tests/unit/test_mention_ui.py

import pytest
from agenthicc.tui.transcript import TranscriptModel, MentionChip

pytestmark = pytest.mark.unit


def test_mention_chip_ok_renders_green(tmp_path):
    m = TranscriptModel()
    m.append_turn("a1", "assistant", 0.0)
    m.add_mention_chips("a1", [
        MentionChip(raw="@auth.py", kind="file", display_size="1.2 KB", ok=True)
    ])
    lines = m.render()
    chip_line = next((l for l in lines if "@auth.py" in l), None)
    assert chip_line is not None
    assert "✓" in chip_line


def test_mention_chip_error_renders_red(tmp_path):
    m = TranscriptModel()
    m.append_turn("a1", "assistant", 0.0)
    m.add_mention_chips("a1", [
        MentionChip(raw="@ghost.txt", kind="unresolved", display_size="",
                    ok=False, error="not found")
    ])
    lines = m.render()
    chip_line = next((l for l in lines if "@ghost.txt" in l), None)
    assert chip_line is not None
    assert "✗" in chip_line
    assert "not found" in chip_line


def test_multiple_chips_all_rendered():
    m = TranscriptModel()
    m.append_turn("a1", "agent", 0.0)
    chips = [
        MentionChip(raw="@a.py", kind="file", display_size="1 KB", ok=True),
        MentionChip(raw="@b.py", kind="file", display_size="2 KB", ok=True),
    ]
    m.add_mention_chips("a1", chips)
    lines = m.render()
    assert any("@a.py" in l for l in lines)
    assert any("@b.py" in l for l in lines)


# tests/unit/test_at_mention_completer.py (additions)

def test_completer_display_meta_has_size(tmp_path):
    from agenthicc.tui.input_bar import AtMentionCompleter
    from prompt_toolkit.document import Document
    from prompt_toolkit.completion import CompleteEvent

    (tmp_path / "big.py").write_bytes(b"x" * 2048)
    completer = AtMentionCompleter(base_path=tmp_path)
    doc = Document("@big")
    completions = list(completer.get_completions(doc, CompleteEvent()))
    assert completions
    meta = completions[0].display_meta
    assert meta  # non-empty size string
```

---

## Verification

```bash
PYTHONPATH=src .venv/bin/pytest tests/unit/test_mention_ui.py tests/unit/test_at_mention_completer.py -v

uv run agenthicc
# Type "@src/" → see dropdown with file sizes in right column
# Submit "Review @src/auth.py" →
#   transcript shows:  ⎿ @src/auth.py  3.4 KB  ✓
#   agent has the file content injected before the message
# /expand @src/auth.py → shows first 50 lines of injected content
```
