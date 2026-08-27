"""StatusComponent, ComposerComponent, FooterComponent (PRD-60 §4-6)."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rich.console import RenderableType
    from rich.text import Text
    from agenthicc.tui.conversation_store import AppState
    from agenthicc.runners.startup import StartupCoordinator

# Flower icons that cycle during agent runs
_FLOWERS = ("✿", "❀", "❁", "❃", "✾", "❋", "✽", "❊")
_THINKING = "Thinking"
# Spinner frames shown while compaction LLM call is in flight (PRD-119)
_COMPACT_SPINNER = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

# Hint strings per agent state.
# During thinking/running the user can still type (to queue a message), use
# @-mentions and /commands — so the idle hints remain accurate and we keep
# the mode line consistent between idle and streaming.
_IDLE_HINTS = "Enter Submit  Ctrl+J Newline  /cmd  @Mention"
_HINTS: dict[str, str] = {
    "idle": _IDLE_HINTS,
    "thinking": _IDLE_HINTS,  # same — streaming input accepts all these keys
    "running": _IDLE_HINTS,  # same
    "recovering": "ESC Cancel  (LLM responding to tool error)",
    "error": "R Retry  Esc Dismiss",
    "complete": "Enter New Task  Ctrl+L Clear",
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


def _cached_elapsed(conv: object, *, total_activity: bool = True) -> float:
    """Read a cached duration without deriving time during rendering.

    ``activity_elapsed_s`` spans consecutive internal LLM turns until the
    outer TUI activity returns to IDLE. Waiting overlays intentionally use the
    per-turn display clock so their content remains stable while the user is
    answering a prompt. The fallback keeps lightweight ConversationStore-
    shaped test doubles compatible.
    """
    names = (
        ("activity_elapsed_s", "display_elapsed_s") if total_activity else ("display_elapsed_s",)
    )
    for name in names:
        value = getattr(conv, name, None)
        value = value() if callable(value) else value
        if isinstance(value, (int, float)):
            return float(value)
    return 0.0


def _fit(markup: str, cols: int) -> str:
    from agenthicc.tui.rendering import fit, visible_len  # noqa: PLC0415

    if visible_len(markup) > cols:
        return fit(markup, cols)
    return markup


def _thinking_markup(frame: int) -> str:
    word = _THINKING
    n = len(word)
    cycle = 2 * (n - 1)
    pos = (frame % cycle) if cycle > 0 else 0
    if pos >= n:
        pos = cycle - pos
    return "".join(f"[bold]{ch}[/bold]" if i == pos else ch for i, ch in enumerate(word))


def _waiting_label(state: "AppState") -> str | None:
    """Return a stable label while an approval/question modal is pending."""
    pending_signal = getattr(state, "pending_approval", None)
    if not callable(pending_signal):
        return None
    pending = pending_signal()
    if pending is None:
        return None
    kind = getattr(pending, "kind", None)
    if not isinstance(kind, str):
        kind = ""
    return {
        "questions": "Waiting for your answer",
        "plan_review": "Waiting for plan approval",
    }.get(kind, "Waiting for approval")


# ── StatusComponent ───────────────────────────────────────────────────────────


class StatusComponent:
    """Renders the two-line status bar from ConversationStore signals.

    Line 1: {flower} {state_animation} │ Runtime: mm:ss │ {active_tool}
    Line 2: {model_name} │ Tokens: Nk │ $N.NNNN
    """

    def __init__(self, app_state: AppState, startup: StartupCoordinator | None = None) -> None:
        self._state = app_state
        self._startup = startup

    def _startup_line(self, cols: int) -> "Text | None":
        """Render non-ready startup work without exposing exception details."""
        if self._startup is None:
            return None
        from rich.text import Text  # noqa: PLC0415

        reports = self._startup.snapshot()
        pending = [report for report in reports if report.state.value != "ready"]
        if not pending:
            return None
        labels = ", ".join(f"{report.name} {report.state.value}" for report in pending[:3])
        if len(pending) > 3:
            labels += f", +{len(pending) - 3} more"
        return Text.from_markup(_fit(f"[dim]Startup: {labels}[/dim]", cols))

    def render(self) -> RenderableType:
        from rich.text import Text  # noqa: PLC0415
        from rich.console import Group  # noqa: PLC0415
        from rich.markup import escape as _e  # noqa: PLC0415

        conv = self._state.conversation
        cols = _get_cols()
        _terminal_wait_signal = getattr(conv, "terminal_waiting", None)
        terminal_waiting = (
            _terminal_wait_signal() is True if callable(_terminal_wait_signal) else False
        )
        if terminal_waiting:
            elapsed_signal = getattr(conv, "terminal_wait_elapsed_s", None)
            elapsed_value = elapsed_signal() if callable(elapsed_signal) else 0.0
            count_signal = getattr(conv, "terminal_running_count", None)
            running_count = count_signal() if callable(count_signal) else 0
            label_signal = getattr(conv, "terminal_wait_label", None)
            label = label_signal() if callable(label_signal) else "terminal"
            wait_message = (
                "[cyan]Waiting for background terminal[/cyan] "
                f"({_fmt_elapsed(float(elapsed_value))} • Esc to interrupt) · "
                f"{int(running_count)} background terminals running · /ps to view · /stop to stop all"
            )
            from agenthicc.tui.rendering import visible_len  # noqa: PLC0415

            if visible_len(wait_message) > cols:
                wait_message = (
                    f"[cyan]Waiting terminal[/cyan] ({_fmt_elapsed(float(elapsed_value))} • Esc) · "
                    f"{int(running_count)} running · /ps · /stop all"
                )
            wait_line = _fit(wait_message, cols)
            model = conv.model_name()
            command_line = f"[dim]└ {_e(str(label))}[/dim]"
            if model:
                command_line += f" [dim]│ {_e(model)}[/dim]"
            command_line = _fit(command_line, cols)
            sid = conv.session_id()
            turns = conv.turn_count()
            cost = conv.cost_usd()
            inp = conv.tokens_in()
            out = conv.tokens_out()
            usage_status = getattr(conv, "usage_status", lambda: "complete")()
            cost_status = getattr(conv, "cost_status", lambda: "estimated")()
            meta = ""
            if sid:
                meta += f"[dim]{_e(sid)}[/dim]"
            meta += f"[dim] │  {turns} turn{'s' if turns != 1 else ''}[/dim]"
            meta += (
                f"[dim] │  ${cost:.3f}[/dim]"
                if cost_status != "unavailable"
                else "[dim] │  cost unknown[/dim]"
            )
            meta += f"[dim] │  ↑ {inp:,} ↓ {out:,}[/dim]"
            if usage_status != "complete":
                meta += "[yellow] ?[/yellow]"
            return Group(
                Text.from_markup(wait_line),
                Text.from_markup(command_line),
                Text.from_markup(_fit(meta, cols)),
            )
        waiting_label = _waiting_label(self._state)
        waiting = waiting_label is not None
        loading_signal = getattr(conv, "transcript_loading", None)
        transcript_loading = loading_signal() is True if callable(loading_signal) else False

        _frame = conv.frame()
        # Flower animates only while the agent is active; frozen at index 0 when idle.
        flower = (
            _FLOWERS[_frame % len(_FLOWERS)]
            if not waiting and (conv.is_running() or conv.compaction_active())
            else _FLOWERS[0]
        )
        agent_st = conv.agent_state()
        state_name = agent_st.name.lower()

        if transcript_loading:
            state_text = "Loading transcript…"
        elif waiting:
            state_text = waiting_label or "Waiting for approval"
        elif conv.is_running():
            if state_name == "recovering":
                state_text = "↻ " + _thinking_markup(_frame)
            else:
                state_text = _thinking_markup(_frame)
        else:
            state_text = agent_st.name.title()

        colors = {
            "idle": "white",
            "thinking": "yellow",
            "running": "cyan",
            "recovering": "red",
            "error": "red",
            "complete": "green",
        }
        color = "yellow" if transcript_loading else colors.get(state_name, "dim")

        # ── line 1: state animation + elapsed + tokens + active tool ────────────
        l1_parts = [f"{flower} [{color}]{state_text}[/{color}]"]
        elapsed = _cached_elapsed(conv, total_activity=not waiting)
        if elapsed > 0:
            l1_parts.append(f"[dim] │[/dim] {_fmt_elapsed(elapsed)}")
        inp = conv.tokens_in()
        out = conv.tokens_out()
        usage_status = getattr(conv, "usage_status", lambda: "complete")()
        if inp or out or usage_status != "unavailable":
            marker = " [yellow]?[/yellow]" if usage_status != "complete" else ""
            l1_parts.append(
                f"[dim] │[/dim] [cyan]↑ {inp:,}[/cyan] [green]↓ {out:,}[/green]{marker}"
            )
        _pool = conv.subagent_pool_state()
        if _pool is not None:
            l1_parts.append(
                f"[dim] │[/dim] [magenta]{_pool.done}/{_pool.total} subagents[/magenta]"
            )
        while len(l1_parts) > 1 and _vlen("".join(l1_parts)) > cols:
            l1_parts.pop()
        line1 = "".join(l1_parts)

        model = conv.model_name()
        if not model:
            return Text.from_markup(line1)

        # ── line 2: model name (PRD-118: phase override; PRD-119: compaction spinner)
        _wf = self._state.workflow_run()
        if _wf is not None and _wf.status == "running" and _wf.current_phase_model:
            model = _wf.current_phase_model
        if conv.compaction_active():
            _sp = _COMPACT_SPINNER[_frame % len(_COMPACT_SPINNER)]
            line2 = _fit(f"[yellow]{_sp} Compacting…[/yellow]", cols)
        else:
            line2 = _fit(f"[dim]{_e(model)}[/dim]", cols)

        # ── line 3: session ID + turns + cost ────────────────────────────────────
        sid = conv.session_id()
        turns = conv.turn_count()
        cost = conv.cost_usd()

        l3_parts: list[str] = []
        if sid:
            l3_parts.append(f"[dim]{_e(sid)}[/dim]")
        l3_parts.append(f"[dim] │  {turns} turn{'s' if turns != 1 else ''}[/dim]")
        cost_status = getattr(conv, "cost_status", lambda: "estimated")()
        l3_parts.append(
            f"[dim] │  ${cost:.3f}[/dim]"
            if cost_status != "unavailable"
            else "[dim] │  cost unknown[/dim]"
        )
        while len(l3_parts) > 1 and _vlen("".join(l3_parts)) > cols:
            l3_parts.pop()
        line3 = "".join(l3_parts)

        startup_line = self._startup_line(cols)
        lines: list[RenderableType] = [
            Text.from_markup(line1),
            Text.from_markup(line2),
            Text.from_markup(line3),
        ]
        if startup_line is not None:
            lines.append(startup_line)
        return Group(*lines)

    def height(self, cols: int) -> int:  # noqa: ARG002
        """Return the total terminal rows this component occupies.

        Counts the blank separator line (rendered by Workspace._build before
        calling render()) plus every line render() produces.  Must always
        equal the actual rendered row count — invariant I-10.

        Layout: 1 (blank) + 1 (line1) + 1 (line2, if model) + 1 (line3, if model)
        → 2 when no model set, 4 when all three lines present.
        """
        conv = self._state.conversation
        terminal_signal = getattr(conv, "terminal_waiting", None)
        if callable(terminal_signal) and terminal_signal() is True:
            return 4  # blank separator + wait line + command line + metadata
        has_model = bool(conv.model_name())
        blank = 1  # Text("") prepended by Workspace._build()
        line1 = 1  # always: flower + state + runtime
        line2 = 1 if has_model else 0  # model name
        line3 = 1 if has_model else 0  # session id + metrics
        startup_line = self._startup_line(cols)
        return blank + line1 + line2 + line3 + (1 if startup_line is not None else 0)


# ── multi-line composer helper ────────────────────────────────────────────────


def _render_multiline(buf: list[str], cursor: int) -> RenderableType:
    """Build one Rich Text per logical line; return as a Group.

    Used by ComposerComponent.render() when the buffer contains '\\n'.
    No _fit call — Rich handles terminal-width soft-wrapping per line.
    """
    from rich.text import Text  # noqa: PLC0415
    from rich.console import Group  # noqa: PLC0415
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
    cursor_col = len(lines[-1])
    cumulative = 0
    for i, ln in enumerate(lines):
        if cumulative + len(ln) >= cursor:
            cursor_line = i
            cursor_col = cursor - cumulative
            break
        cumulative += len(ln) + 1

    # One Text per logical line.
    result: list[Text] = []
    for i, ln in enumerate(lines):
        t = Text()
        t.append(f"{PROMPT_CHAR} " if i == 0 else "  ", style="bold yellow" if i == 0 else "")
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

    def __init__(self, app_state: AppState) -> None:
        self._state = app_state

    def render(self) -> RenderableType:
        from rich.text import Text  # noqa: PLC0415
        from agenthicc.tui.input.renderer import build_prompt  # noqa: PLC0415

        inp = self._state.input

        # UnifiedInputSession projects the hidden paste range into buf while
        # retaining any text typed before or after it.  Keep the fallback for
        # callers that construct InputState directly (including overlays and
        # tests) with only a paste label.
        if inp.paste_condensed():
            disp_buf = inp.buf()
            label = inp.paste_label()
            if disp_buf and label and label in "".join(disp_buf):
                return _render_multiline(disp_buf, inp.cursor())
            fallback = list(label)
            return Text.from_markup(_fit(build_prompt(fallback, len(fallback)), _get_cols()))

        # Non-condensed: always use _render_multiline regardless of line count.
        # Single-line buffers produce one Text in the Group — Rich soft-wraps
        # at terminal width instead of truncating with "…".
        return _render_multiline(inp.buf(), inp.cursor())

    def height(self, cols: int) -> int:  # noqa: ARG002
        inp = self._state.input
        buf = inp.buf()
        if inp.paste_condensed():
            label = inp.paste_label()
            if not label or label not in "".join(buf):
                return 1
        lines = "".join(buf).split("\n") if buf else [""]
        total = 0
        for i, line in enumerate(lines):
            overhead = 2  # "❯ " or "  "
            usable = max(1, cols - overhead)
            total += max(1, (len(line) + usable - 1) // usable)
        return total


# ── FooterComponent ───────────────────────────────────────────────────────────


class FooterComponent:
    """Renders mode string + context hints.  Always 2 rows."""

    def __init__(self, app_state: AppState) -> None:
        self._state = app_state

    def render(self) -> RenderableType:
        from rich.text import Text  # noqa: PLC0415
        from rich.console import Group  # noqa: PLC0415

        conv = self._state.conversation
        cols = _get_cols()

        # Row 1: mode string — derived from AppState.active_mode (PRD-75).
        # When a /workflow override is active, append ⬡ workflow-name indicator.
        from agenthicc.tui.runtime.mode_manager import build_mode_str  # noqa: PLC0415
        from rich.markup import escape as _e_m  # noqa: PLC0415

        mode = self._state.active_mode()
        _wf_ovr_raw = conv.workflow_override()
        _wf_ovr = _wf_ovr_raw if isinstance(_wf_ovr_raw, str) else None
        _wf_suffix = f"  [cyan dim]⬡ {_e_m(_wf_ovr)}[/cyan dim]" if _wf_ovr else ""
        mode_line = _fit(f"  {build_mode_str(mode)}{_wf_suffix}", cols)

        # Row 2+: notification (may be multi-line) > paste hint > normal hints.
        # Multi-line notifications (from stacked notify_transient() calls or
        # explicit \n in the message) each get their own rendered row.
        raw_notif = conv.notification()
        notif = raw_notif if isinstance(raw_notif, str) else None
        hints_str: str | Text
        if notif:
            notif_lines = _wrap_notification(notif, cols)
            hints_str = notif_lines[0]
            # Extra lines rendered after Group — collected here, appended below.
            _extra_notif_lines = notif_lines[1:]
        elif self._state.input.paste_condensed():
            _extra_notif_lines = []
            hints_str = _build_hints(
                "Ctrl+V Expand paste  Backspace Delete paste/char  Esc Delete paste  Enter Submit as-is",
                cols,
            )
        else:
            _extra_notif_lines = []
            state_name = conv.agent_state().name.lower()
            raw_hints = _HINTS.get(state_name, _HINTS["idle"])
            hints_str = _build_hints(raw_hints, cols)
        hints_renderable = hints_str if isinstance(hints_str, Text) else Text.from_markup(hints_str)

        # PRD-81: optional workflow progress row
        extra: list[RenderableType] = []
        # Extra notification lines from stacked notify_transient() calls.
        extra.extend(_extra_notif_lines)
        from rich.markup import escape as _e  # noqa: PLC0415

        _wf = self._state.workflow_run()
        if _wf is not None and _wf.status == "running":
            _n = _wf.current_phase_index + 1
            _tot = _wf.total_phases
            _badge = self._state.active_mode().badge
            _phase = (
                f"  {_n}/{_tot}  {_e(_wf.current_phase)}"
                if _wf.current_phase is not None
                else f"  {_n}/{_tot}"
            )
            extra.append(
                Text.from_markup(
                    _fit(f"  [dim]{_e(_badge)} {_e(_wf.workflow_name)}{_phase}[/dim]", cols)
                )
            )

        # PRD-124: optional subagent worker grid row
        _pool = conv.subagent_pool_state()
        if _pool is not None:
            extra.append(Text.from_markup(_fit(_build_worker_grid(_pool, cols), cols)))

        return Group(
            Text.from_markup(mode_line),
            hints_renderable,
            *extra,
        )

    def height(self, cols: int) -> int:  # noqa: ARG002
        extra = 0
        # Extra notification lines (stacked notify_transient calls or \n in message)
        try:
            notif = self._state.conversation.notification()
            if notif and isinstance(notif, str):
                extra += max(0, len(_wrap_notification(notif, cols)) - 1)
        except Exception:  # noqa: BLE001
            pass
        _wf = self._state.workflow_run()
        if _wf is not None and _wf.status == "running":
            extra += 1
        if self._state.conversation.subagent_pool_state() is not None:
            extra += 1
        return 2 + extra


# ── helpers ───────────────────────────────────────────────────────────────────

_WORKER_STATUS_ICONS = {
    "pending": "○",
    "running": "⠸",  # static char; frame cycling done by status bar spinner
    "done": "✓",
    "failed": "✗",
}
_WORKER_STATUS_COLORS = {
    "pending": "dim",
    "running": "cyan",
    "done": "green",
    "failed": "red",
}


def _build_worker_grid(pool: object, cols: int) -> str:
    """Render a compact one-line worker grid for the footer."""
    from rich.markup import escape as _e  # noqa: PLC0415

    workers = getattr(pool, "workers", [])
    cells: list[str] = []
    for w in workers:
        icon = _WORKER_STATUS_ICONS.get(w.status, "?")
        color = _WORKER_STATUS_COLORS.get(w.status, "dim")
        label = _e(w.label)
        cells.append(f"[{color}]{icon}[/{color}] {label}")
    row = "  " + "  ".join(cells)
    return _fit(row, cols)


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


def _wrap_notification(message: str, cols: int) -> list["Text"]:
    """Wrap a notification without discarding any of its text.

    Notifications can contain workflow run IDs, which are long unbroken
    strings. Passing them through :func:`_fit` loses the suffix and makes the
    recovery command unusable. Rich's ``Text.wrap(..., overflow="fold")``
    preserves every character, wrapping at spaces where possible and folding
    long IDs when a single token is wider than the terminal.
    """

    from rich.console import Console  # noqa: PLC0415
    from rich.text import Text  # noqa: PLC0415

    width = max(1, cols)
    return list(
        Text(message, style="dim").wrap(
            Console(width=width, color_system=None, force_terminal=False),
            width=width,
            overflow="fold",
        )
    )
