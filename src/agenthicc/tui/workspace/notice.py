"""One notice component for every error, warning, info and success line (TUX-08).

```text
 ✗ No API key for anthropic.
   Set ANTHROPIC_API_KEY, or run /model to switch provider.

 ! MCP server "github" is unavailable; continuing without its tools.   /mcp

 · Compacted 84 messages into 1 summary.
```

A :class:`Notice` is a Rich renderable. Its marker is the severity's glyph in
the severity's theme token (TUX-01): ``error`` → ``danger`` ✗, ``warning`` →
``warning`` !, ``info`` → ``muted`` ·, ``success`` → ``success`` ✓. The title
wraps under itself, never under the marker, and the body sits below it in the
``muted`` token. With ``words=True`` (screen-reader mode) the marker is a word
("Error:"), so the severity never depends on a glyph or a colour (BEAU-4 §5).

A ``literal`` notice is an accessibility announcement: its text is printed
exactly, with no marker, style or markup (BEAU-4 §4.3).

Notices are *persistent* when printed into the scrollback, and *transient*
when shown on the status line through
:meth:`agenthicc.tui.conversation_store.ConversationStore.notify`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from rich.segment import Segment
from rich.text import Text

from agenthicc.tui.theme import glyphs, style

if TYPE_CHECKING:
    from rich.console import Console, ConsoleOptions, RenderResult

__all__ = ["SEVERITIES", "Notice", "Severity", "severity_of"]

Severity = Literal["error", "warning", "info", "success"]

#: Every severity, most urgent first.
SEVERITIES: tuple[Severity, ...] = ("error", "warning", "info", "success")

_TOKEN: dict[Severity, str] = {
    "error": "danger",
    "warning": "warning",
    "info": "muted",
    "success": "success",
}

_WORD: dict[Severity, str] = {
    "error": "Error:",
    "warning": "Warning:",
    "info": "Note:",
    "success": "Done:",
}


def severity_of(value: object, default: Severity = "info") -> Severity:
    """*value* as a severity when it names one, else *default*."""
    for severity in SEVERITIES:
        if value == severity:
            return severity
    return default


@dataclass(frozen=True)
class Notice:
    """A severity, a title, an optional body and the commands it suggests."""

    severity: Severity
    title: str
    body: str = ""
    #: Commands shown after the title (``/mcp``); each is literal text.
    actions: tuple[str, ...] = ()
    #: Print *title* exactly: no marker, style or markup (announcements).
    literal: bool = False
    #: Spell the severity as a word instead of a glyph (screen readers).
    words: bool = False

    def marker(self) -> str:
        """The severity's glyph, or its word with ``words=True``."""
        if self.words:
            return _WORD[self.severity]
        g = glyphs()
        return {"error": g.fail, "warning": g.warn, "info": g.info, "success": g.ok}[self.severity]

    def plain(self) -> str:
        """One line of plain text (the status line, logs and tests)."""
        if self.literal:
            return self.title
        actions = f"   {'  '.join(self.actions)}" if self.actions else ""
        return f"{self.marker()} {self.title}{actions}"

    def content(self) -> Text:
        """The title, actions and body, without the marker."""
        text = Text(self.title)
        if self.actions:
            text.append("   ")
            text.append("  ".join(self.actions), style=style("accent"))
        if self.body:
            text.append("\n")
            text.append(self.body, style=style("muted"))
        return text

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        if self.literal:
            yield Text(self.title)
            return
        marker = self.marker()
        lead = Segment(" ")
        indent = 1 + len(marker) + 1
        width = max(1, options.max_width - indent)
        lines = console.render_lines(
            self.content(), options.update(width=width, height=None), pad=False
        )
        gap = Segment(" " * indent)
        for index, line in enumerate(lines):
            if index == 0:
                yield lead
                yield Segment(marker, style(_TOKEN[self.severity], bold=True))
                yield Segment(" ")
            else:
                yield gap
            yield from _rstrip(line)
            yield Segment.line()


def _rstrip(line: list[Segment]) -> list[Segment]:
    """*line* without the trailing spaces a wrap leaves (plain captures stay clean)."""
    segments = list(line)
    while segments and not segments[-1].control and not segments[-1].text.strip():
        segments.pop()
    if segments and not segments[-1].control:
        last = segments[-1]
        segments[-1] = Segment(last.text.rstrip(), last.style)
    return segments
