"""Component state classes for the TUI.

Each class owns the reactive state for one visual region.
Property setters call ``_notify()`` automatically (via ReactiveProperty or
explicit ``@property`` setters) so the LivePanel redraws whenever state changes.
"""
from __future__ import annotations

import time

from agenthicc.tui.reactive import ReactiveProperty, _Observable


# ── StatusBarState ────────────────────────────────────────────────────────────

class StatusBarState(_Observable):
    """Reactive state for the agent status bar.

    Implements :class:`~agenthicc.tui.protocols.StatusState`.
    """

    _STATE_COLOR: dict[str, str] = {
        "idle": "dim", "thinking": "yellow", "running": "cyan",
        "approval": "yellow", "error": "red", "complete": "green",
    }

    # Flower icons that cycle on each tick while the agent is active.
    _FLOWERS: tuple[str, ...] = ("✿", "❀", "❁", "❃", "✾", "❋", "✽", "❊")
    _THINKING_WORD = "Thinking"

    def __init__(self) -> None:
        super().__init__()
        self._state = "idle"
        self._tool = ""
        self._input_tokens = 0
        self._output_tokens = 0
        self._cost_usd = 0.0
        self._elapsed = 0.0
        self._start_time: float = 0.0
        self._session_id = ""
        self._completed_agents = 0
        self._thinking_frame = 0   # scanner position for the bold-char wave
        self._flower_frame = 0     # index into _FLOWERS

    # ── reactive properties ───────────────────────────────────────────────────

    @property
    def state(self) -> str:
        return self._state

    @state.setter
    def state(self, v: str) -> None:
        if v != self._state:
            self._state = v
            self._notify()

    @property
    def tool(self) -> str:
        return self._tool

    @tool.setter
    def tool(self, v: str) -> None:
        if v != self._tool:
            self._tool = v
            self._notify()

    @property
    def session_id(self) -> str:
        return self._session_id

    @session_id.setter
    def session_id(self, v: str) -> None:
        self._session_id = v
        self._notify()

    # ── bulk update helpers ───────────────────────────────────────────────────

    def add_tokens(self, inp: int, out: int, cost: float) -> None:
        self._input_tokens += inp
        self._output_tokens += out
        self._cost_usd += cost
        self._notify()

    def start_run(self) -> None:
        self._start_time = time.monotonic()
        self._input_tokens = 0
        self._output_tokens = 0
        self._elapsed = 0.0
        self._thinking_frame = 0
        self._flower_frame = 0
        self.state = "thinking"

    def finish_run(self) -> None:
        self._completed_agents += 1
        self.state = "idle"
        self.tool = ""

    def tick(self) -> None:
        if self._start_time and self._state not in ("idle", "complete"):
            elapsed = time.monotonic() - self._start_time
            if abs(elapsed - self._elapsed) >= 0.1:
                self._elapsed = elapsed
                self._thinking_frame += 1
                self._flower_frame = (self._flower_frame + 1) % len(self._FLOWERS)
                self._notify()

    def _thinking_markup(self) -> str:
        """Return Rich markup for the thinking word with one bold char bouncing."""
        word = self._THINKING_WORD
        n = len(word)
        # Bounce: 0→n-1→0 with period 2*(n-1)
        cycle = 2 * (n - 1)
        frame = self._thinking_frame % cycle if cycle > 0 else 0
        pos = frame if frame < n else cycle - frame
        return "".join(
            f"[bold]{ch}[/bold]" if i == pos else ch
            for i, ch in enumerate(word)
        )

    def height(self, cols: int) -> int:
        """Number of rows: 2 when a model name is present, else 1."""
        return 2 if self._session_id else 1

    def render(self, cols: int = 80) -> str:
        """Rich markup — potentially two lines when a model name is set.

        Layout::

            {flower} {state_animation} │ Runtime: mm:ss │ {tool}
            {model_name} │ Tokens: xxk │ $0.xxxx
        """
        from agenthicc.tui.rendering import visible_len, fit  # noqa: PLC0415
        from rich.markup import escape as _e  # noqa: PLC0415

        flower = self._FLOWERS[self._flower_frame % len(self._FLOWERS)]
        color = self._STATE_COLOR.get(self._state, "dim")

        if self._state in ("thinking", "running"):
            state_text = self._thinking_markup()
        else:
            state_text = self._state.title()

        # ── Line 1: state animation + runtime + active tool ───────────────────
        line1_parts: list[str] = [f"{flower} [{color}]{state_text}[/{color}]"]
        if self._elapsed > 0:
            m, s2 = divmod(int(self._elapsed), 60)
            line1_parts.append(f"[dim] │ Runtime:[/dim] {m:02d}:{s2:02d}")
        if self._tool:
            line1_parts.append(f"[dim] │[/dim] [bold]{_e(self._tool)}[/bold]")
        # Truncate line 1 if it's still too wide.
        while len(line1_parts) > 1 and visible_len("".join(line1_parts)) > cols:
            line1_parts.pop()
        line1 = "".join(line1_parts)
        if visible_len(line1) > cols:
            line1 = fit(line1, cols)

        if not self._session_id:
            return line1

        # ── Line 2: model name + tokens + cost ───────────────────────────────
        line2_parts: list[str] = [f"[dim]{_e(self._session_id)}[/dim]"]
        tok = self._input_tokens + self._output_tokens
        if tok:
            s = f"{tok / 1000:.0f}k" if tok >= 1000 else str(tok)
            line2_parts.append(f"[dim] │ Tokens:[/dim] {s}")
        if self._cost_usd:
            line2_parts.append(f"[dim] │ ${self._cost_usd:.4f}[/dim]")
        # Drop low-priority parts from line 2 until it fits.
        while len(line2_parts) > 1 and visible_len("".join(line2_parts)) > cols:
            line2_parts.pop()
        line2 = "".join(line2_parts)
        if visible_len(line2) > cols:
            line2 = fit(line2, cols)

        return line1 + "\n" + line2


# ── FooterState ───────────────────────────────────────────────────────────────

class FooterState(_Observable):
    """Reactive state for the footer: mode line + context-sensitive key hints.

    Renders as two rows:
      Row 1 — mode string (e.g. "⏵⏵ Auto  (shift+tab to cycle)  │  ctrl+j = ↵")
      Row 2 — key hints for the current mode (e.g. "Esc Interrupt  │  Ctrl+Z …")

    Implements :class:`~agenthicc.tui.protocols.FooterStateProtocol`.
    """

    _DEFAULT_MODE_STR = "⏵⏵ Auto  (shift+tab to cycle)  │  ctrl+j = ↵"

    HINTS: dict[str, str] = {
        "idle":     "Enter Submit  Ctrl+J Newline  /cmd  @Mention  Ctrl+L Clear",
        "thinking": "Esc Interrupt",
        "running":  "Esc Interrupt  Ctrl+Z Background",
        "approval": "Y Approve  N Reject  A Approve All  Esc Cancel",
        "error":    "R Retry  L View Logs  Esc Dismiss",
        "complete": "Enter New Task  F2 History  Ctrl+L Clear",
    }

    def __init__(self) -> None:
        super().__init__()
        self._mode = "idle"
        self._notification: str | None = None
        self._mode_str = self._DEFAULT_MODE_STR

    @property
    def mode(self) -> str:
        return self._mode

    @mode.setter
    def mode(self, v: str) -> None:
        if v != self._mode:
            self._mode = v
            self._notify()

    @property
    def mode_str(self) -> str:
        return self._mode_str

    @mode_str.setter
    def mode_str(self, v: str) -> None:
        if v != self._mode_str:
            self._mode_str = v
            self._notify()

    def notify_text(self, text: str | None) -> None:
        self._notification = text
        self._notify()

    def height(self, cols: int) -> int:  # noqa: ARG002
        """Footer is always exactly 2 rows: mode line + hints line."""
        return 2

    def _hints_line(self, cols: int) -> str:
        """Build the key-hints line for the current mode, fitting *cols*."""
        from agenthicc.tui.rendering import visible_len, fit  # noqa: PLC0415
        if self._notification:
            notif = f"[dim]{self._notification}[/dim]"
            return fit(notif, cols) if visible_len(notif) > cols else notif
        raw = self.HINTS.get(self._mode, self.HINTS["idle"])
        parts = [h.strip() for h in raw.split("  ") if h.strip()]
        segs: list[str] = []
        for p in parts:
            words = p.split()
            if len(words) >= 2:
                segs.append(
                    f"[bold]{words[0]}[/bold] [dim]{' '.join(words[1:])}[/dim]"
                )
            else:
                segs.append(f"[dim]{p}[/dim]")
        sep = "  [dim]│[/dim]  "
        while len(segs) > 1 and visible_len(sep.join(segs)) > cols:
            segs.pop()
        result = sep.join(segs)
        if visible_len(result) > cols:
            return fit(result, cols)
        return result

    def render(self, cols: int = 80) -> str:
        """Return a two-line Rich markup string: mode str then key hints."""
        from agenthicc.tui.rendering import visible_len, fit  # noqa: PLC0415
        mode_line_raw = f"  [dim]{self._mode_str}[/dim]"
        mode_line = fit(mode_line_raw, cols) if visible_len(mode_line_raw) > cols else mode_line_raw
        hints_line = self._hints_line(cols)
        return mode_line + "\n" + hints_line


# ── InputBarState ─────────────────────────────────────────────────────────────

class InputBarState(_Observable):
    """Reactive state for the ❯ prompt line in the LivePanel.

    Every public attribute is a :class:`~agenthicc.tui.reactive.ReactiveProperty`.
    Assigning any of them automatically triggers all registered ``on_change``
    callbacks — no explicit ``_notify()`` needed at each assignment site.

    Implements :class:`~agenthicc.tui.protocols.InputBarStateProtocol`.
    """

    PROMPT_CHAR = "❯"
    CURSOR_CHAR = "▌"
    _INDENT = "  "

    # ── reactive properties — every write triggers _notify() ─────────────────
    buf: list = ReactiveProperty(default_factory=list)   # type: ignore[assignment]
    cursor: int = ReactiveProperty(0)                     # type: ignore[assignment]
    paste_condensed: bool = ReactiveProperty(False)       # type: ignore[assignment]
    paste_label: str = ReactiveProperty("")               # type: ignore[assignment]
    mode_str: str = ReactiveProperty(                     # type: ignore[assignment]
        "⏵⏵ Auto  (shift+tab to cycle)  │  ctrl+j = ↵"
    )

    def __init__(self) -> None:
        super().__init__()
        object.__setattr__(self, "_rp_buf", [])
        object.__setattr__(self, "_rp_cursor", 0)
        object.__setattr__(self, "_rp_paste_condensed", False)
        object.__setattr__(self, "_rp_paste_label", "")
        object.__setattr__(self, "_rp_mode_str",
                           "⏵⏵ Auto  (shift+tab to cycle)  │  ctrl+j = ↵")

    # ── batch mutators (one notify per call) ──────────────────────────────────

    def update(
        self,
        buf: list[str],
        cursor: int,
        paste_condensed: bool = False,
        paste_label: str = "",
    ) -> None:
        object.__setattr__(self, "_rp_buf", list(buf))
        object.__setattr__(self, "_rp_cursor", cursor)
        object.__setattr__(self, "_rp_paste_condensed", paste_condensed)
        object.__setattr__(self, "_rp_paste_label", paste_label)
        self._notify()

    def clear(self) -> None:
        object.__setattr__(self, "_rp_buf", [])
        object.__setattr__(self, "_rp_cursor", 0)
        object.__setattr__(self, "_rp_paste_condensed", False)
        object.__setattr__(self, "_rp_paste_label", "")
        self._notify()

    # ── height + rendering ────────────────────────────────────────────────────

    def height(self, cols: int) -> int:
        """Number of terminal rows the prompt occupies at *cols* width.

        When paste is condensed the display is a single label line regardless
        of how many lines the original content had.  When expanded (or for
        normal typing) every logical line (split at ``\\n``) is counted plus
        any terminal wrapping caused by long lines.
        """
        if self.paste_condensed:
            # Condensed: one label line, regardless of original content size.
            return 1

        _FIRST_OVERHEAD = 2   # "❯ "
        _REST_OVERHEAD  = 2   # "  "
        logical_lines = "".join(self.buf).split("\n") if self.buf else [""]
        total = 0
        for i, line in enumerate(logical_lines):
            overhead = _FIRST_OVERHEAD if i == 0 else _REST_OVERHEAD
            usable = max(1, cols - overhead)
            # ceil(len / usable), minimum 1 row even for empty lines
            total += max(1, (len(line) + usable - 1) // usable)
        return total

    def render_prompt(self, cols: int = 80) -> str:
        """Rich markup: ❯ <typed text> ▌ at the cursor position.

        Lines that would exceed *cols* are truncated with an ellipsis so the
        Rich Live block never wraps and desynchronises its cursor tracking.
        """
        from rich.markup import escape as _e  # noqa: PLC0415
        from agenthicc.tui.rendering import fit, visible_len  # noqa: PLC0415

        display = list(self.paste_label) if self.paste_condensed else self.buf
        pos = len(display) if self.paste_condensed else self.cursor
        raw_lines: list[list[str]] = []
        cur: list[str] = []
        for ch in display:
            if ch == "\n":
                raw_lines.append(cur)
                cur = []
            else:
                cur.append(ch)
        raw_lines.append(cur)
        cumulative = 0
        cursor_line, cursor_col = len(raw_lines) - 1, len(raw_lines[-1])
        for i, ln in enumerate(raw_lines):
            if cumulative + len(ln) >= pos:
                cursor_line, cursor_col = i, pos - cumulative
                break
            cumulative += len(ln) + 1
        parts: list[str] = []
        for i, ln in enumerate(raw_lines):
            text = "".join(ln)
            prefix = f"[bold green]{self.PROMPT_CHAR}[/bold green] " if i == 0 else self._INDENT
            if i == cursor_line:
                col = cursor_col
                content = _e(text[:col]) + f"[bold]{self.CURSOR_CHAR}[/bold]" + _e(text[col:])
            else:
                content = _e(text)
            line = prefix + content
            if visible_len(line) > cols:
                line = fit(line, cols)
            parts.append(line)
        return "\n".join(parts)


# ── SpinnerState ──────────────────────────────────────────────────────────────

class SpinnerState(_Observable):
    """Reactive state for the tool-call spinner panel shown during streaming.

    Implements :class:`~agenthicc.tui.protocols.SpinnerStateProtocol`.
    """

    def __init__(self) -> None:
        super().__init__()
        self._calls: dict[str, dict] = {}
        self._streaming_text = ""
        self._frame = 0

    def add_call(self, tool_use_id: str, name: str, args: dict) -> None:
        items = list(args.items())
        if len(items) == 1:
            args_str = repr(items[0][1])[:60]
        elif items:
            args_str = ", ".join(f"{k}={repr(v)[:25]}" for k, v in items[:3])
        else:
            args_str = ""
        self._calls[tool_use_id] = {
            "name": name, "args": args_str,
            "done": False, "ok": True, "ms": None, "diff": None,
        }
        self._notify()

    def complete_call(
        self,
        tool_use_id: str,
        success: bool,
        ms: float | None,
        diff: str | None,
    ) -> None:
        if tool_use_id in self._calls:
            c = self._calls[tool_use_id]
            c["done"] = True
            c["ok"] = success
            c["ms"] = ms
            c["diff"] = diff
            self._notify()

    def set_streaming_text(self, text: str) -> None:
        self._streaming_text = text
        self._notify()

    def tick(self) -> None:
        self._frame = (self._frame + 1) % 8
        self._notify()

    def clear(self) -> None:
        self._calls.clear()
        self._streaming_text = ""
        self._frame = 0

    def height(self, cols: int) -> int:  # noqa: ARG002
        """One row per tool call plus one row for streaming text preview (if any)."""
        call_rows = sum(
            1 + (min(6, len(c.get("diff", "").splitlines())) if c.get("diff") else 0)
            for c in self._calls.values()
        )
        streaming_row = 1 if self._streaming_text else 0
        return call_rows + streaming_row

    def render_calls(self, cols: int = 80) -> list[str]:
        """Return one Rich markup string per tool call row, each fitting *cols*."""
        from agenthicc.tui.rendering import fit, visible_len  # noqa: PLC0415
        from rich.markup import escape as _e  # noqa: PLC0415

        def _safe(line: str) -> str:
            return fit(line, cols) if visible_len(line) > cols else line

        lines: list[str] = []
        for c in self._calls.values():
            name, args = c["name"], c["args"]
            if c["done"]:
                icon = "[green]✓[/green]" if c["ok"] else "[red]✗[/red]"
                ms_str = f"  [dim]{c['ms']:.0f}ms[/dim]" if c["ms"] else ""
                lines.append(_safe(
                    f"   [dim]⎿[/dim] [bold]{name}[/bold][dim]({_e(args)})[/dim]  {icon}{ms_str}"
                ))
                if c.get("diff"):
                    for dl in c["diff"].splitlines()[:6]:
                        if dl.startswith("+"):
                            lines.append(_safe(f"      [green]{_e(dl)}[/green]"))
                        elif dl.startswith("-"):
                            lines.append(_safe(f"      [red]{_e(dl)}[/red]"))
                        elif dl.startswith("@@"):
                            lines.append(_safe(f"      [dim cyan]{_e(dl)}[/dim cyan]"))
                        else:
                            lines.append(_safe(f"      [dim]{_e(dl)}[/dim]"))
            else:
                lines.append(_safe(
                    f"   [dim]⎿[/dim] [bold]{name}[/bold][dim]({_e(args)})[/dim]  [dim]…[/dim]"
                ))
        if self._streaming_text:
            preview = self._streaming_text.replace("\n", " ")
            lines.append(_safe(f"   [dim]{_e(preview)}[/dim]"))
        return lines
