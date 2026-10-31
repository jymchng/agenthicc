"""StatusComponent, ComposerComponent, FooterComponent (PRD-60 §4-6).

The status bar and footer describe each row as a markup string; the workspace
renders a markup row once per distinct string and width and reuses it until
the string changes (AGX-11), so an animation frame re-renders only the row it
changes. The composer builds its rows as segments directly: it folds each
logical line at the terminal width, caches the fold per line, and shows at most
``max_rows`` rows around the cursor.
"""

from __future__ import annotations

import functools
import os
from collections.abc import Iterator
from typing import TYPE_CHECKING

from rich.cells import cell_len
from rich.segment import Segment
from rich.style import Style

from agenthicc.tui.preferences import ui_settings as _ui

if TYPE_CHECKING:
    from rich.console import Console, ConsoleOptions, RenderableType
    from rich.text import Text
    from agenthicc.tui.conversation_store import AppState
    from agenthicc.runners.startup import StartupCoordinator
    from agenthicc.tui.workspace.live_region import Line

# Flower icons that cycle during agent runs
_FLOWERS = ("✿", "❀", "❁", "❃", "✾", "❋", "✽", "❊")
_THINKING = "Thinking"
# Spinner frames shown while compaction LLM call is in flight (PRD-119)
_COMPACT_SPINNER = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

# Static stand-ins for animation when [ui] reduced_motion or screen_reader is
# set (AGX-13).
_STILL = "●"


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
    from agenthicc.tui.rendering import fit, visible_len

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

    def _startup_line(self, cols: int) -> str | None:
        """Describe non-ready startup work without exposing exception details."""
        if self._startup is None:
            return None
        reports = self._startup.snapshot()
        pending = [report for report in reports if report.state.value != "ready"]
        if not pending:
            return None
        labels = ", ".join(f"{report.name} {report.state.value}" for report in pending[:3])
        if len(pending) > 3:
            labels += f", +{len(pending) - 3} more"
        return _fit(f"[dim]Startup: {labels}[/dim]", cols)

    def render(self) -> RenderableType:
        """Return the status bar as Rich text (one ``Text`` per row)."""
        return _markup_renderable(self.markup_lines(_get_cols()))

    def markup_lines(self, cols: int) -> list[str]:
        """Return the status bar's rows as markup, each fitted to *cols*."""
        from rich.markup import escape as _e

        conv = self._state.conversation
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
            from agenthicc.tui.rendering import visible_len

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
            return [wait_line, command_line, _fit(meta, cols)]
        waiting_label = _waiting_label(self._state)
        waiting = waiting_label is not None
        loading_signal = getattr(conv, "transcript_loading", None)
        transcript_loading = loading_signal() is True if callable(loading_signal) else False

        _frame = conv.frame()
        ui = _ui(self._state)
        animated = ui is None or ui.animated
        active = not waiting and (conv.is_running() or conv.compaction_active())
        # Flower animates only while the agent is active; frozen at index 0 when idle.
        if not animated:
            flower = _STILL if active else ""
        else:
            flower = _FLOWERS[_frame % len(_FLOWERS)] if active else _FLOWERS[0]
        agent_st = conv.agent_state()
        state_name = agent_st.name.lower()

        if transcript_loading:
            state_text = "Loading transcript…"
        elif waiting:
            state_text = waiting_label or "Waiting for approval"
        elif conv.is_running():
            thinking = _thinking_markup(_frame) if animated else _THINKING
            state_text = "↻ " + thinking if state_name == "recovering" else thinking
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
        reader = ui is not None and ui.screen_reader
        sep = "[dim],[/dim]" if reader else "[dim] │[/dim]"
        lead = f"{flower} " if flower else ""
        l1_parts = [f"{lead}[{color}]{state_text}[/{color}]"]
        elapsed = _cached_elapsed(conv, total_activity=not waiting)
        if elapsed > 0:
            l1_parts.append(f"{sep} {_fmt_elapsed(elapsed)}")
        inp = conv.tokens_in()
        out = conv.tokens_out()
        usage_status = getattr(conv, "usage_status", lambda: "complete")()
        if inp or out or usage_status != "unavailable":
            marker = " [yellow]?[/yellow]" if usage_status != "complete" else ""
            if reader:
                tokens = f"{inp:,} tokens in, {out:,} out"
                l1_parts.append(f"{sep} {tokens}{' (estimated)' if marker else ''}")
            else:
                l1_parts.append(f"{sep} [cyan]↑ {inp:,}[/cyan] [green]↓ {out:,}[/green]{marker}")
        _pool = conv.subagent_pool_state()
        if _pool is not None:
            l1_parts.append(f"{sep} [magenta]{_pool.done}/{_pool.total} subagents[/magenta]")
        while len(l1_parts) > 1 and _vlen("".join(l1_parts)) > cols:
            l1_parts.pop()
        line1 = "".join(l1_parts)

        model = conv.model_name()
        if not model:
            return [line1]

        # ── line 2: model name (PRD-118: phase override; PRD-119: compaction spinner)
        _wf = self._state.workflow_run()
        if _wf is not None and _wf.status == "running" and _wf.current_phase_model:
            model = _wf.current_phase_model
        if conv.compaction_active():
            _sp = _COMPACT_SPINNER[_frame % len(_COMPACT_SPINNER)] if animated else _STILL
            line2 = _fit(f"[yellow]{_sp} Compacting…[/yellow]", cols)
        else:
            line2 = _fit(f"[dim]{_e(model)}[/dim]", cols)

        # ── line 3: session ID + turns + cost ────────────────────────────────────
        sid = conv.session_id()
        turns = conv.turn_count()
        cost = conv.cost_usd()

        l3_parts: list[str] = []
        bar = "," if reader else " │ "
        if sid:
            l3_parts.append(f"[dim]{_e(sid)}[/dim]")
        l3_parts.append(f"[dim]{bar} {turns} turn{'s' if turns != 1 else ''}[/dim]")
        cost_status = getattr(conv, "cost_status", lambda: "estimated")()
        l3_parts.append(
            f"[dim]{bar} ${cost:.3f}[/dim]"
            if cost_status != "unavailable"
            else f"[dim]{bar} cost unknown[/dim]"
        )
        while len(l3_parts) > 1 and _vlen("".join(l3_parts)) > cols:
            l3_parts.pop()
        line3 = "".join(l3_parts)

        lines = [line1, line2, line3]
        startup_line = self._startup_line(cols)
        if startup_line is not None:
            lines.append(startup_line)
        return lines

    def height(self, cols: int) -> int:
        """Return the total terminal rows this component occupies.

        Counts the blank separator line the workspace draws above the bar plus
        every row :meth:`markup_lines` produces — invariant I-10.
        """
        return 1 + len(self.markup_lines(cols))


# ── ComposerComponent ─────────────────────────────────────────────────────────

_PROMPT_STYLE = Style(bold=True, color="yellow")
_CURSOR_STYLE = Style(bold=True)
_HIDDEN_STYLE = Style(dim=True)


@functools.lru_cache(maxsize=4096)
def _fold(text: str, width: int, single_cells: bool = False) -> tuple[str, ...]:
    """Split *text* into rows of at most *width* cells (at least one row).

    *single_cells* says every character is one cell wide, so rows are slices.
    """
    if not text:
        return ("",)
    if single_cells or text.isascii():
        return tuple(text[start : start + width] for start in range(0, len(text), width))
    rows: list[str] = []
    row: list[str] = []
    used = 0
    for character in text:
        size = cell_len(character)
        if row and used + size > width:
            rows.append("".join(row))
            row, used = [], 0
        row.append(character)
        used += size
    rows.append("".join(row))
    return tuple(rows)


@functools.lru_cache(maxsize=4096)
def _line_rows(text: str, cursor: int | None, width: int) -> tuple[tuple[str, int | None], ...]:
    """Fold one logical line, marking the row and column that hold the cursor.

    The cursor glyph is part of the folded text, as it occupies a cell. Only
    an edited line or the cursor's line misses this cache on a keystroke.
    """
    from agenthicc.tui.input.renderer import CURSOR_CHAR

    if cursor is None:
        return tuple((row, None) for row in _fold(text, width))
    display = text[:cursor] + CURSOR_CHAR + text[cursor:]
    rows: list[tuple[str, int | None]] = []
    offset = 0
    # The cursor glyph is one cell wide, so an ASCII line stays single-cell.
    for row in _fold(display, width, text.isascii()):
        column = cursor - offset if offset <= cursor < offset + len(row) else None
        rows.append((row, column))
        offset += len(row)
    return tuple(rows)


class _Row:
    """One display row of the composer."""

    __slots__ = ("prefix", "text", "cursor")

    def __init__(self, prefix: str, text: str, cursor: int | None) -> None:
        self.prefix = prefix
        self.text = text
        self.cursor = cursor

    def segments(self) -> Line:
        from agenthicc.tui.input.renderer import CURSOR_CHAR, PROMPT_CHAR

        prefix = Segment(
            self.prefix, _PROMPT_STYLE if self.prefix.startswith(PROMPT_CHAR) else None
        )
        if self.cursor is None:
            return (prefix, Segment(self.text))
        return (
            prefix,
            Segment(self.text[: self.cursor]),
            Segment(CURSOR_CHAR, _CURSOR_STYLE),
            Segment(self.text[self.cursor + len(CURSOR_CHAR) :]),
        )


def _hidden_row(arrow: str, count: int) -> Line:
    return (Segment(f"  {arrow} {count} more line{'s' if count != 1 else ''}", _HIDDEN_STYLE),)


class _ComposerView:
    """The composer as a Rich renderable, laid out at the width it is given."""

    def __init__(self, composer: "ComposerComponent") -> None:
        self._composer = composer

    def __rich_console__(self, console: "Console", options: "ConsoleOptions") -> Iterator[Segment]:
        for line in self._composer.lines(options.max_width):
            yield from line
            yield Segment.line()


class ComposerComponent:
    """Renders ``❯ text▌`` from InputState signals.

    Each logical line is folded at the terminal width (continuation rows are
    indented under the prompt). When the prompt needs more than ``max_rows``
    rows, only the rows around the cursor are drawn, with ``↑ N more lines`` /
    ``↓ N more lines`` in place of the rest (AGX-11).
    """

    def __init__(self, app_state: AppState, *, max_rows: int = 12) -> None:
        self._state = app_state
        self.max_rows = max(3, max_rows)
        # First visible row while the prompt is taller than max_rows.
        self._top = 0

    def render(self) -> RenderableType:
        return _ComposerView(self)

    def height(self, cols: int) -> int:
        return len(self.lines(cols))

    def _rows(self, cols: int) -> tuple[list[_Row], int]:
        """Return every display row and the index of the row with the cursor."""
        from agenthicc.tui.input.renderer import PROMPT_CHAR

        width = max(1, cols - 2)
        inp = self._state.input
        buf = inp.buf()
        cursor = inp.cursor()
        if inp.paste_condensed():
            label = inp.paste_label()
            if not (buf and label and label in "".join(buf)):
                # Callers that set only a paste label show the label.
                buf, cursor = list(label), len(label)
        rows: list[_Row] = []
        cursor_row = 0
        start = 0
        for index, line in enumerate("".join(buf).split("\n")):
            end = start + len(line)
            column = cursor - start if start <= cursor <= end else None
            for fold_index, (text, row_cursor) in enumerate(_line_rows(line, column, width)):
                if row_cursor is not None:
                    cursor_row = len(rows)
                prefix = f"{PROMPT_CHAR} " if index == 0 and fold_index == 0 else "  "
                rows.append(_Row(prefix, text, row_cursor))
            start = end + 1
        return rows, cursor_row

    def lines(self, cols: int) -> tuple[Line, ...]:
        """Return the composer's rows as segment lines at *cols* columns."""
        rows, cursor_row = self._rows(cols)
        total = len(rows)
        if total <= self.max_rows:
            self._top = 0
            return tuple(row.segments() for row in rows)
        # Keep the cursor visible, moving the window as little as possible;
        # each hidden side costs one indicator row.
        visible = self.max_rows
        top = self._top
        for _ in range(3):
            top = max(0, min(min(max(top, cursor_row - visible + 1), cursor_row), total - visible))
            visible = self.max_rows - (top > 0) - (top + visible < total)
        top = max(0, min(min(max(top, cursor_row - visible + 1), cursor_row), total - visible))
        self._top = top
        lines: list[Line] = []
        if top > 0:
            lines.append(_hidden_row("↑", top))
        lines.extend(row.segments() for row in rows[top : top + visible])
        if top + visible < total:
            lines.append(_hidden_row("↓", total - top - visible))
        return tuple(lines)


# ── FooterComponent ───────────────────────────────────────────────────────────


class FooterComponent:
    """Renders mode string + context hints (two rows, plus optional rows)."""

    def __init__(self, app_state: AppState) -> None:
        self._state = app_state

    def render(self) -> RenderableType:
        """Return the footer as Rich text (one ``Text`` per row)."""
        from rich.console import Group
        from rich.text import Text

        return Group(
            *(Text.from_markup(row, emoji=False) for row in self.markup_lines(_get_cols()))
        )

    def markup_lines(self, cols: int) -> list[str]:
        """Return the footer's rows as markup, each fitted to *cols*."""
        from agenthicc.tui.runtime.mode_manager import build_mode_str
        from rich.markup import escape as _e

        conv = self._state.conversation

        # Row 1: mode string — derived from AppState.active_mode (PRD-75).
        # When a /workflow override is active, append ⬡ workflow-name indicator.
        mode = self._state.active_mode()
        _wf_ovr_raw = conv.workflow_override()
        _wf_ovr = _wf_ovr_raw if isinstance(_wf_ovr_raw, str) else None
        _wf_suffix = f"  [cyan dim]⬡ {_e(_wf_ovr)}[/cyan dim]" if _wf_ovr else ""
        needs_you = conv.background_needs_you()
        running_agents = conv.background_running()
        attention_badges: list[str] = []
        if needs_you:
            attention_badges.append(f"[bold yellow]◆ {needs_you} need you[/bold yellow]")
        if running_agents:
            attention_badges.append(f"[cyan]● {running_agents} running[/cyan]")
        if conv.background_dnd():
            attention_badges.append("[dim]◉ DND[/dim]")
        attention_suffix = "  [dim]·[/dim]  ".join(attention_badges)
        # AGX-09: the primary agent, once another than the default is chosen.
        agent = self._state.agent()
        agent_prefix = (
            f"[bold]{_e(agent)}[/bold] [dim]·[/dim] "
            if isinstance(agent, str) and agent and agent != "default"
            else ""
        )
        mode_row = f"  {agent_prefix}{build_mode_str(mode)}{_wf_suffix}"
        if attention_suffix:
            mode_row += f"  [dim]│[/dim]  {attention_suffix}"
        rows = [_fit(mode_row, cols)]

        # Row 2+: notification (may be multi-line) > paste hint > normal hints.
        # Multi-line notifications (from stacked notify_transient() calls or
        # explicit \n in the message) each get their own rendered row.
        raw_notif = conv.notification()
        notif = raw_notif if isinstance(raw_notif, str) else None
        if not notif:
            raw_loop_status = conv.loop_status()
            notif = raw_loop_status if isinstance(raw_loop_status, str) else None
        if not notif:
            raw_attention = conv.background_toast()
            notif = raw_attention if isinstance(raw_attention, str) else None
        if notif:
            rows.extend(_notification_rows(notif, cols))
        else:
            # Hints are the composer's applicable key bindings (AGX-13).
            conditions: set[str] = set()
            if conv.is_running():
                conditions.add("running")
            if self._state.input.paste_condensed():
                conditions.update(("paste", "paste_at_cursor"))
            pool_state = conv.subagent_pool_state()
            if pool_state is not None:
                conditions.add("subagents")
                if pool_state.active:
                    conditions.add("subagents_running")
            keymap = self._state.keymap()
            ui = _ui(self._state)
            hints = tuple(
                (hint.keys, hint.label) for hint in keymap.hints("composer", frozenset(conditions))
            )
            if conv.agents_hint():
                hints = (*hints, ("←", "agents"))
            rows.append(_hint_row(hints, cols, ui is not None and ui.screen_reader))

        # PRD-81: optional workflow progress row
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
            rows.append(_fit(f"  [dim]{_e(_badge)} {_e(_wf.workflow_name)}{_phase}[/dim]", cols))
        return rows

    def height(self, cols: int) -> int:
        return len(self.markup_lines(cols))


# ── helpers ───────────────────────────────────────────────────────────────────


def _vlen(markup: str) -> int:
    from agenthicc.tui.rendering import visible_len

    return visible_len(markup)


@functools.lru_cache(maxsize=256)
def _hint_row(hints: tuple[tuple[str, str], ...], cols: int, plain: bool = False) -> str:
    """Render ``(keys, label)`` hints in priority order, dropping the last to fit."""
    from rich.markup import escape

    segs = [f"[bold]{escape(keys)}[/bold] [dim]{escape(label)}[/dim]" for keys, label in hints]
    sep = "[dim],[/dim] " if plain else "  [dim]│[/dim]  "
    while len(segs) > 1 and _vlen(sep.join(segs)) > cols:
        segs.pop()
    return _fit(sep.join(segs), cols)


@functools.lru_cache(maxsize=256)
def _notification_rows(message: str, cols: int) -> tuple[str, ...]:
    """Return a wrapped notification as dim markup rows, one per terminal row."""
    from rich.markup import escape

    return tuple(f"[dim]{escape(row.plain)}[/dim]" for row in _wrap_notification(message, cols))


def _markup_renderable(rows: list[str]) -> RenderableType:
    from rich.console import Group
    from rich.text import Text

    texts = [Text.from_markup(row, emoji=False) for row in rows]
    return texts[0] if len(texts) == 1 else Group(*texts)


def _wrap_notification(message: str, cols: int) -> list["Text"]:
    """Wrap a notification without discarding any of its text.

    Notifications can contain workflow run IDs, which are long unbroken
    strings. Passing them through :func:`_fit` loses the suffix and makes the
    recovery command unusable. Rich's ``Text.wrap(..., overflow="fold")``
    preserves every character, wrapping at spaces where possible and folding
    long IDs when a single token is wider than the terminal.
    """

    from rich.console import Console
    from rich.text import Text

    width = max(1, cols)
    return list(
        Text(message, style="dim").wrap(
            Console(width=width, color_system=None, force_terminal=False),
            width=width,
            overflow="fold",
        )
    )
