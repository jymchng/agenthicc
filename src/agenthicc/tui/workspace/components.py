"""StatusComponent, ComposerComponent, FooterComponent (PRD-60 §4-6)."""
from __future__ import annotations

import os
from typing import Any

# Flower icons that cycle during agent runs
_FLOWERS = ("✿", "❀", "❁", "❃", "✾", "❋", "✽", "❊")
_THINKING = "Thinking"

# Hint strings per agent state.
# During thinking/running the user can still type (to queue a message), use
# @-mentions and /commands — so the idle hints remain accurate and we keep
# the mode line consistent between idle and streaming.
_IDLE_HINTS = "Enter Submit  Ctrl+J Newline  /cmd  @Mention"
_HINTS: dict[str, str] = {
    "idle":       _IDLE_HINTS,
    "thinking":   _IDLE_HINTS,   # same — streaming input accepts all these keys
    "running":    _IDLE_HINTS,   # same
    "recovering": "ESC Cancel  (LLM responding to tool error)",
    "error":      "R Retry  Esc Dismiss",
    "complete":   "Enter New Task  Ctrl+L Clear",
}


def _get_cols() -> int:
    try:
        return os.get_terminal_size().columns
    except OSError:
        return 80


def _fmt_elapsed(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    return f"{s // 60}m {s % 60}s"


def _fit(markup: str, cols: int) -> str:
    from agenthicc.tui.rendering import fit, visible_len   # noqa: PLC0415
    if visible_len(markup) > cols:
        return fit(markup, cols)
    return markup


def _thinking_markup(frame: int) -> str:
    word = _THINKING
    n    = len(word)
    cycle = 2 * (n - 1)
    pos   = (frame % cycle) if cycle > 0 else 0
    if pos >= n:
        pos = cycle - pos
    return "".join(
        f"[bold]{ch}[/bold]" if i == pos else ch
        for i, ch in enumerate(word)
    )


# ── StatusComponent ───────────────────────────────────────────────────────────

class StatusComponent:
    """Renders the two-line status bar from ConversationStore signals.

    Line 1: {flower} {state_animation} │ Runtime: mm:ss │ {active_tool}
    Line 2: {model_name} │ Tokens: Nk │ $N.NNNN
    """

    def __init__(self, app_state: Any) -> None:
        self._state = app_state

    def render(self) -> Any:
        from rich.text import Text           # noqa: PLC0415
        from rich.console import Group       # noqa: PLC0415
        from rich.markup import escape as _e # noqa: PLC0415

        conv = self._state.conversation
        cols = _get_cols()

        flower     = _FLOWERS[conv._flower_frame % len(_FLOWERS)]
        agent_st   = conv.agent_state()
        state_name = agent_st.name.lower()

        if conv.is_running():
            if state_name == "recovering":
                state_text = "↻ " + _thinking_markup(conv._thinking_frame)
            else:
                state_text = _thinking_markup(conv._thinking_frame)
        else:
            state_text = agent_st.name.title()

        colors = {
            "idle": "white", "thinking": "yellow", "running": "cyan",
            "recovering": "red", "error": "red", "complete": "green",
        }
        color = colors.get(state_name, "dim")

        # ── line 1: state animation + elapsed + tokens + active tool ────────────
        l1_parts = [f"{flower} [{color}]{state_text}[/{color}]"]
        elapsed = conv.elapsed_s()
        if elapsed > 0:
            l1_parts.append(f"[dim] │[/dim] {_fmt_elapsed(elapsed)}")
        inp = conv.tokens_in()
        out = conv.tokens_out()
        if inp or out:
            l1_parts.append(
                f"[dim] │[/dim] [cyan]↑ {inp:,}[/cyan] [green]↓ {out:,}[/green]"
            )
        while len(l1_parts) > 1 and _vlen("".join(l1_parts)) > cols:
            l1_parts.pop()
        line1 = "".join(l1_parts)

        model = conv.model_name()
        if not model:
            return Text.from_markup(line1)

        # ── line 2: model name only ───────────────────────────────────────────
        line2 = _fit(f"[dim]{_e(model)}[/dim]", cols)

        # ── line 3: session ID + turns + cost ────────────────────────────────────
        sid   = conv.session_id()
        turns = conv.turn_count()
        cost  = conv.cost_usd()

        l3_parts: list[str] = []
        if sid:
            l3_parts.append(f"[dim]{_e(sid)}[/dim]")
        l3_parts.append(f"[dim] │  {turns} turn{'s' if turns != 1 else ''}[/dim]")
        l3_parts.append(f"[dim] │  ${cost:.3f}[/dim]")
        while len(l3_parts) > 1 and _vlen("".join(l3_parts)) > cols:
            l3_parts.pop()
        line3 = "".join(l3_parts)

        return Group(
            Text.from_markup(line1),
            Text.from_markup(line2),
            Text.from_markup(line3),
        )

    def height(self, cols: int) -> int:  # noqa: ARG002
        """Return the total terminal rows this component occupies.

        Counts the blank separator line (rendered by Workspace._build before
        calling render()) plus every line render() produces.  Must always
        equal the actual rendered row count — invariant I-10.

        Layout: 1 (blank) + 1 (line1) + 1 (line2, if model) + 1 (line3, if model)
        → 2 when no model set, 4 when all three lines present.
        """
        has_model = bool(self._state.conversation.model_name())
        blank  = 1   # Text("") prepended by Workspace._build()
        line1  = 1   # always: flower + state + runtime
        line2  = 1 if has_model else 0   # model name
        line3  = 1 if has_model else 0   # session id + metrics
        return blank + line1 + line2 + line3


# ── multi-line composer helper ────────────────────────────────────────────────

def _render_multiline(buf: list[str], cursor: int) -> Any:
    """Build one Rich Text per logical line; return as a Group.

    Used by ComposerComponent.render() when the buffer contains '\\n'.
    No _fit call — Rich handles terminal-width soft-wrapping per line.
    """
    from rich.text import Text            # noqa: PLC0415
    from rich.console import Group        # noqa: PLC0415
    from agenthicc.tui.input.renderer import PROMPT_CHAR, CURSOR_CHAR  # noqa: PLC0415

    # Split on '\n' into logical lines.
    lines: list[list[str]] = []
    current: list[str] = []
    for ch in buf:
        if ch == "\n":
            lines.append(current)
            current = []
        else:
            current.append(ch)
    lines.append(current)

    # Locate the cursor: which logical line and column offset.
    cursor_line = len(lines) - 1
    cursor_col  = len(lines[-1])
    cumulative  = 0
    for i, ln in enumerate(lines):
        if cumulative + len(ln) >= cursor:
            cursor_line = i
            cursor_col  = cursor - cumulative
            break
        cumulative += len(ln) + 1

    # One Text per logical line.
    result: list[Text] = []
    for i, ln in enumerate(lines):
        t = Text()
        t.append(f"{PROMPT_CHAR} " if i == 0 else "  ",
                 style="bold yellow" if i == 0 else "")
        if i == cursor_line:
            t.append("".join(ln[:cursor_col]))
            t.append(CURSOR_CHAR, style="bold")
            t.append("".join(ln[cursor_col:]))
        else:
            t.append("".join(ln))
        result.append(t)

    return Group(*result)


# ── ComposerComponent ─────────────────────────────────────────────────────────

class ComposerComponent:
    """Renders ❯ text▌ from InputState signals."""

    def __init__(self, app_state: Any) -> None:
        self._state = app_state

    def render(self) -> Any:
        from rich.text import Text                              # noqa: PLC0415
        from agenthicc.tui.input.renderer import build_prompt  # noqa: PLC0415

        inp  = self._state.input
        cols = _get_cols()

        # Condensed paste label — always a single line, _fit is safe.
        if inp.paste_condensed():
            disp_buf    = list(inp.paste_label())
            disp_cursor = len(disp_buf)
            return Text.from_markup(_fit(build_prompt(disp_buf, disp_cursor), cols))

        # Non-condensed: always use _render_multiline regardless of line count.
        # Single-line buffers produce one Text in the Group — Rich soft-wraps
        # at terminal width instead of truncating with "…".
        return _render_multiline(inp.buf(), inp.cursor())

    def height(self, cols: int) -> int:  # noqa: ARG002
        inp = self._state.input
        buf = inp.buf()
        if inp.paste_condensed():
            return 1
        lines = "".join(buf).split("\n") if buf else [""]
        total = 0
        for i, line in enumerate(lines):
            overhead = 2  # "❯ " or "  "
            usable   = max(1, cols - overhead)
            total   += max(1, (len(line) + usable - 1) // usable)
        return total


# ── FooterComponent ───────────────────────────────────────────────────────────

class FooterComponent:
    """Renders mode string + context hints.  Always 2 rows."""

    def __init__(self, app_state: Any) -> None:
        self._state = app_state

    def render(self) -> Any:
        from rich.text import Text     # noqa: PLC0415
        from rich.console import Group # noqa: PLC0415

        conv = self._state.conversation
        cols = _get_cols()

        # Row 1: mode string — derived from AppState.active_mode (PRD-75)
        from agenthicc.tui.runtime.mode_manager import build_mode_str  # noqa: PLC0415
        mode     = self._state.active_mode()
        mode_line = _fit(f"  {build_mode_str(mode)}", cols)

        # Row 2: notification > paste hint > normal state hints
        notif = conv.notification()
        if notif:
            hints_str = _fit(f"[dim]{notif}[/dim]", cols)
        elif self._state.input.paste_condensed():
            hints_str = _build_hints(
                "Ctrl+V Expand paste  Backspace Delete  Enter Submit as-is", cols
            )
        else:
            state_name = conv.agent_state().name.lower()
            raw_hints  = _HINTS.get(state_name, _HINTS["idle"])
            hints_str  = _build_hints(raw_hints, cols)

        # PRD-81: optional workflow progress row
        extra: list[Any] = []
        try:
            from rich.markup import escape as _e  # noqa: PLC0415
            _wf = self._state.workflow_run()
            if (isinstance(getattr(_wf, "status", None), str)
                    and _wf.status == "running"
                    and isinstance(getattr(_wf, "workflow_name", None), str)):
                _n      = getattr(_wf, "current_phase_index", 0) + 1
                _tot    = getattr(_wf, "total_phases", 0)
                _cp     = getattr(_wf, "current_phase", None)
                _badge  = self._state.active_mode().badge
                _phase  = f"  {_n}/{_tot}  {_e(_cp)}" if isinstance(_cp, str) else f"  {_n}/{_tot}"
                extra.append(Text.from_markup(
                    _fit(f"  [dim]{_e(_badge)} {_e(_wf.workflow_name)}{_phase}[/dim]", cols)
                ))
        except Exception:  # noqa: BLE001
            pass


        return Group(
            Text.from_markup(mode_line),
            Text.from_markup(hints_str),
            *extra,
        )

    def height(self, cols: int) -> int:  # noqa: ARG002
        extra = 0
        try:
            _wf = self._state.workflow_run()
            if (getattr(_wf, "status", None) == "running"
                    and getattr(_wf, "workflow_name", None)):
                extra += 1
        except Exception:  # noqa: BLE001
            pass
        return 2 + extra


# ── helpers ───────────────────────────────────────────────────────────────────

def _vlen(markup: str) -> int:
    from agenthicc.tui.rendering import visible_len  # noqa: PLC0415
    return visible_len(markup)


def _build_hints(raw: str, cols: int) -> str:
    parts = [h.strip() for h in raw.split("  ") if h.strip()]
    segs: list[str] = []
    for p in parts:
        words = p.split()
        if len(words) >= 2:
            segs.append(f"[bold]{words[0]}[/bold] [dim]{' '.join(words[1:])}[/dim]")
        else:
            segs.append(f"[dim]{p}[/dim]")
    sep = "  [dim]│[/dim]  "
    while len(segs) > 1 and _vlen(sep.join(segs)) > cols:
        segs.pop()
    result = sep.join(segs)
    return _fit(result, cols)
