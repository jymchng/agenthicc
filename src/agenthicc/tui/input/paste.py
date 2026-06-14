"""PasteState — manages bracketed paste condensation.

Large pastes are "condensed" to a single label line so they don't flood
the input bar.  Ctrl+V expands back to the full content.  Backspace on a
condensed paste deletes the entire paste block cleanly.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from agenthicc.tui.input.buffer import InputBuffer

_CONDENSE_LINES = 3   # condense if paste has more logical lines than this


@dataclass
class PasteState:
    condensed: bool = False
    label: str = ""
    start: int = 0
    end: int = 0
    count: int = field(default=0, repr=False)

    def apply(self, buf: InputBuffer, text: str, cols: int) -> None:
        """Insert *text* at the current cursor; condense if large."""
        start, end = buf.insert_many(list(text))
        self.start = start
        self.end = end
        n_lines = text.count("\n") + 1
        should_condense = n_lines > _CONDENSE_LINES or len(text) > max(cols - 4, 40)
        if should_condense:
            self.count += 1
            suffix = (
                f"+{n_lines} lines" if n_lines > 1 else f"{len(text)} chars"
            )
            self.label = f"Pasted text #{self.count} {suffix}"
            self.condensed = True

    def expand(self) -> None:
        """Ctrl+V — show full paste content."""
        self.condensed = False

    def backspace(self, buf: InputBuffer) -> None:
        """Delete the entire paste block and exit condensed mode."""
        buf.delete_range(self.start, self.end)
        self.condensed = False
