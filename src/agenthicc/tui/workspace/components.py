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
            "idle": "dim", "thinking": "yellow", "running": "cyan",
            "recovering": "red", "error": "red", "complete": "green",
        }
        color = colors.get(state_name, "dim")

        # ── line 1: state animation + runtime + active tool ───────────────────
        l1_parts = [f"{flower} [{color}]{state_text}[/{color}]"]
        elapsed = conv.elapsed_s()
        if elapsed > 0:
            m, s = divmod(int(elapsed), 60)
            l1_parts.append(f"[dim] │ Runtime:[/dim] {m:02d}:{s:02d}")
        tool = conv.active_tool()
        if tool:
            l1_parts.append(f"[dim] │[/dim] [bold]{_e(tool)}[/bold]")
        while len(l1_parts) > 1 and _vlen("".join(l1_parts)) > cols:
            l1_parts.pop()
        line1 = "".join(l1_parts)

        model = conv.model_name()
        if not model:
            return Text.from_markup(line1)

        # ── line 2: model name only ───────────────────────────────────────────
        line2 = _fit(f"[dim]{_e(model)}[/dim]", cols)

        # ── line 3: session ID + turns + cost + tokens ────────────────────────
        sid   = conv.session_id()
        turns = conv.turn_count()
        cost  = conv.cost_usd()
        inp   = conv.tokens_in()
        out   = conv.tokens_out()

        l3_parts: list[str] = []
        if sid:
            l3_parts.append(f"[dim]{_e(sid)}[/dim]")
        l3_parts.append(f"[dim] │  {turns} turn{'s' if turns != 1 else ''}[/dim]")
        l3_parts.append(f"[dim] │  ${cost:.3f}[/dim]")
        l3_parts.append(
            f"  [cyan]↑ {inp:,}[/cyan]  [green]↓ {out:,}[/green]"
        )
        while len(l3_parts) > 1 and _vlen("".join(l3_parts)) > cols:
            l3_parts.pop()
        line3 = "".join(l3_parts)

        return Group(
            Text.from_markup(line1),
            Text.from_markup(line2),
            Text.from_markup(line3),
        )

    def height(self, cols: int) -> int:  # noqa: ARG002
        return 3 if self._state.conversation.model_name() else 1


# ── ComposerComponent ─────────────────────────────────────────────────────────

class ComposerComponent:
    """Renders ❯ text▌ from InputState signals."""

    def __init__(self, app_state: Any) -> None:
        self._state = app_state

    def render(self) -> Any:
        from rich.text import Text                              # noqa: PLC0415
        from agenthicc.tui.input.renderer import build_prompt  # noqa: PLC0415

        inp    = self._state.input
        cols   = _get_cols()

        if inp.paste_condensed():
            disp_buf    = list(inp.paste_label())
            disp_cursor = len(disp_buf)
        else:
            disp_buf    = inp.buf()
            disp_cursor = inp.cursor()

        prompt = build_prompt(disp_buf, disp_cursor)
        return Text.from_markup(_fit(prompt, cols))

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

        # Row 1: mode string
        mode_line_raw = f"  [dim]{conv.mode_str()}[/dim]"
        mode_line = _fit(mode_line_raw, cols)

        # Row 2: notification OR context hints
        notif = conv.notification()
        if notif:
            hints_str = _fit(f"[dim]{notif}[/dim]", cols)
        else:
            state_name = conv.agent_state().name.lower()
            raw_hints  = _HINTS.get(state_name, _HINTS["idle"])
            hints_str  = _build_hints(raw_hints, cols)

        return Group(
            Text.from_markup(mode_line),
            Text.from_markup(hints_str),
        )

    def height(self, cols: int) -> int:  # noqa: ARG002
        return 2


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
