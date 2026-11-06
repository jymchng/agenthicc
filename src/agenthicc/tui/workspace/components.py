"""The live region's components: StatusLine, ComposerComponent, ComposerFrame (TUX-02).

The live region below the transcript is the composer in its frame, one status
line and at most one hint row::

    ╭─ fix flaky scheduler test ─────────────────────────────── Safe ─╮
    │ ❯ ask, or type / for commands, @ for files                      │
    ╰─────────────────────────────────────────────────────────────────╯
     ● Idle · opus-4.8 · ctx 12% ▰▱▱▱▱ · 2 sessions need you ←   /help

- :class:`StatusLine` lays out segments in a fixed priority order and drops
  the lowest ones (never cutting a word) when the line is too narrow. Its
  segments are rebuilt only when a signal they read changes; a spinner frame
  re-renders the state segment alone.
- :class:`ComposerFrame` draws the composer in a rounded box (or between two
  rules) whose top border carries the session title and the mode badge, and
  owns the contextual hint row.
- :class:`ComposerComponent` folds the prompt at the frame's inner width.

Status and frame rows are markup strings; the workspace renders each distinct
string once per width and theme and reuses it (AGX-11). Every colour is a
theme token and every non-ASCII character a theme glyph (TUX-01).
"""

from __future__ import annotations

import functools
import os
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rich.cells import cell_len
from rich.markup import escape
from rich.segment import Segment
from rich.style import Style

from agenthicc.tui.preferences import ui_settings as _ui
from agenthicc.tui.theme import Theme, current_theme, glyphs, palette, style
from agenthicc.tui.workspace.formatters import (
    compact_token_count,
    context_band,
    context_percent,
    cost_text,
    meter_cells,
    METER_CELLS,
    pool_status_counts,
    short_model,
    spoken_model,
)

if TYPE_CHECKING:
    from rich.console import Console, ConsoleOptions, RenderableType
    from rich.text import Text

    from agenthicc.runners.startup import StartupCoordinator
    from agenthicc.tui.conversation_store import AppState
    from agenthicc.tui.workspace.live_region import Line

__all__ = [
    "PLACEHOLDER",
    "ComposerComponent",
    "ComposerFrame",
    "StatusLine",
    "status_sentence",
]

#: The empty composer's muted placeholder, then shorter forms for narrow frames.
PLACEHOLDER = (
    "ask, or type / for commands, @ for files",
    "ask, or type / for commands",
    "ask",
)
#: The frame title before the session has one.
UNTITLED = "new session"

# Below these widths the layout degrades (TUX-02 §2.5).
_RULES_BELOW = 60
_STATE_ONLY_BELOW = 40
#: Priority of the lowest segment still shown below ``_RULES_BELOW`` columns.
_NARROW_PRIORITY = 3
#: Segments from this priority on are dropped before anything is shortened.
_DROP_FIRST = 4

_MODE_TOKENS = {"safe": "mode_safe", "plan": "mode_plan", "yolo": "mode_yolo"}


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
    outer TUI activity returns to IDLE. Waiting overlays use the per-turn
    display clock so their content stays stable while the user answers.
    """
    names = (
        ("activity_elapsed_s", "display_elapsed_s") if total_activity else ("display_elapsed_s",)
    )
    for name in names:
        value = getattr(conv, name, None)
        value = value() if callable(value) else value
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return 0.0


def _fit(markup: str, cols: int) -> str:
    from agenthicc.tui.rendering import fit, visible_len

    if visible_len(markup) > cols:
        return fit(markup, cols)
    return markup


def _vlen(markup: str) -> int:
    from agenthicc.tui.rendering import visible_len

    return visible_len(markup)


def _signal(owner: object, name: str) -> object:
    value = getattr(owner, name, None)
    return value() if callable(value) else value


def _signal_text(conv: object, name: str, default: str = "") -> str:
    value = _signal(conv, name)
    return value if isinstance(value, str) else default


def _signal_int(conv: object, name: str, default: int = 0) -> int:
    value = _signal(conv, name)
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _signal_float(conv: object, name: str, default: float = 0.0) -> float:
    value = _signal(conv, name)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return default


def _signal_bool(owner: object, name: str) -> bool:
    return _signal(owner, name) is True


def _tint(token: str, text: str, *, bold: bool = False) -> str:
    """*text* (markup) in the colour of *token*; plain when the token is empty."""
    value = getattr(palette(), token)
    attributes = " ".join(
        part for part in ("bold" if bold else "", value) if part not in ("", "none")
    )
    return f"[{attributes}]{text}[/]" if attributes else text


def _mode_token(name: str) -> str:
    return _MODE_TOKENS.get(name.lower(), "accent")


def _reader(app_state: object) -> bool:
    ui = _ui(app_state)
    return ui is not None and ui.screen_reader


def _animated(app_state: object) -> bool:
    ui = _ui(app_state)
    return ui is None or ui.animated


# ── StatusLine ────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _Form:
    markup: str
    width: int


def _form(markup: str) -> _Form:
    return _Form(markup, _vlen(markup))


@dataclass(frozen=True, slots=True)
class _Part:
    """One status segment: its priority and its forms, longest first."""

    priority: int
    forms: tuple[_Form, ...]
    #: The user command's output: cropped, rather than dropped, to fit.
    croppable: bool = False


@dataclass(frozen=True, slots=True)
class _Inputs:
    """Everything the static segments (2–7) read: their cache key."""

    reader: bool
    needs_you: int
    hub_key: str
    background_running: int
    dnd: bool
    context_tokens: int
    context_window: int
    model: str
    workers: tuple[int, int] | None
    workflow: tuple[int, int, str] | None
    workflow_override: str
    startup: str
    terminal_count: int
    usage_status: str
    tokens: tuple[int, int]
    cost: str
    branch: str
    dirty: bool
    user_line: str
    theme: Theme


def _hub_key(app_state: AppState) -> str:
    from agenthicc.tui.keymap import KeymapError, display_key

    try:
        keys = app_state.keymap().action("agents.open_hub").keys
    except (KeymapError, AttributeError, TypeError):
        return ""
    if not isinstance(keys, tuple) or not all(isinstance(key, str) for key in keys):
        return ""
    if "left_on_empty" in keys:
        return glyphs().left
    return display_key(keys[0]) if keys else ""


@functools.lru_cache(maxsize=64)
def _static_parts(inputs: _Inputs) -> tuple[_Part, ...]:
    """Segments 2–7 for *inputs* (rebuilt only when one of them changes)."""
    from agenthicc.tui.attention_labels import sessions_need_you

    g = inputs.theme.glyphs
    reader = inputs.reader
    parts: list[_Part] = []

    # 2. Background sessions that need the user (AGX-10).
    if inputs.needs_you:
        n = inputs.needs_you
        if reader:
            parts.append(_Part(2, (_form(sessions_need_you(n)),)))
        else:
            key = f" {escape(inputs.hub_key)}" if inputs.hub_key else ""
            parts.append(
                _Part(
                    2,
                    tuple(
                        _form(_tint("warning", f"{g.diamond} {label}{key}", bold=True))
                        for label in (
                            sessions_need_you(n),
                            sessions_need_you(n, "short"),
                            sessions_need_you(n, "bare"),
                        )
                    ),
                )
            )

    if inputs.user_line:
        # 3–7 replaced by the user's status-line command (TUX-02 §2.3).
        from rich.text import Text

        parts.append(_Part(3, (_form(Text.from_ansi(inputs.user_line).markup),), croppable=True))
    else:
        parts.extend(_builtin_parts(inputs))
    return tuple(sorted(parts, key=lambda part: part.priority))


def _builtin_parts(inputs: _Inputs) -> list[_Part]:
    from agenthicc.tui.attention_labels import sessions_running

    g = inputs.theme.glyphs
    reader = inputs.reader
    parts: list[_Part] = []

    # 3. Context meter.
    percent = context_percent(inputs.context_tokens, inputs.context_window)
    if percent is not None:
        if reader:
            parts.append(_Part(3, (_form(f"context {percent}%"),)))
        else:
            cells = meter_cells(percent)
            bar = g.meter_full * cells + g.meter_empty * (METER_CELLS - cells)
            band = context_band(percent)
            text = _tint("muted", "ctx") + " " + _tint(band, f"{percent}%")
            parts.append(_Part(3, (_form(f"{text} {_tint(band, bar)}"), _form(text))))

    # 4. Model.
    if inputs.model:
        parts.append(_Part(4, (_form(_tint("muted", escape(inputs.model))),)))

    # 5. Workers, workflow phase, background sessions, startup, terminals.
    if inputs.workers is not None:
        done, total = inputs.workers
        label = f"{done} of {total} workers" if reader else f"{done}/{total} workers"
        parts.append(_Part(5, (_form(label if reader else _tint("agent", label)),)))
    if inputs.workflow is not None:
        index, total, phase = inputs.workflow
        full = f"{index}/{total} {escape(phase)}" if phase else f"{index}/{total}"
        if reader:
            full = f"phase {index} of {total} {escape(phase)}".rstrip()
        parts.append(
            _Part(5, (_form(_tint("info", full)), _form(_tint("info", f"{index}/{total}"))))
        )
    if inputs.workflow_override:
        parts.append(
            _Part(5, (_form(_tint("info", f"workflow {escape(inputs.workflow_override)}")),))
        )
    if inputs.terminal_count:
        word = "terminal" if inputs.terminal_count == 1 else "terminals"
        parts.append(_Part(5, (_form(_tint("muted", f"{inputs.terminal_count} {word} running")),)))
    if inputs.background_running:
        n = inputs.background_running
        parts.append(
            _Part(
                5,
                tuple(
                    _form(label if reader else _tint("info", f"{g.bullet} {label}"))
                    for label in (sessions_running(n), sessions_running(n, "short"))
                ),
            )
        )
    if inputs.dnd:
        parts.append(
            _Part(5, (_form(_tint("muted", "do not disturb")), _form(_tint("muted", "dnd"))))
        )
    if inputs.startup:
        parts.append(_Part(5, (_form(_tint("muted", escape(inputs.startup))),)))

    # 6. Tokens and cost, only as reported.
    if inputs.usage_status in {"complete", "partial"} and any(inputs.tokens):
        tokens_in, tokens_out = inputs.tokens
        approx = "about " if reader else g.approx
        partial = approx if inputs.usage_status == "partial" else ""
        if reader:
            label = f"{partial}{tokens_in:,} tokens in, {tokens_out:,} out"
        else:
            label = (
                f"{partial}{g.up}{compact_token_count(tokens_in)} "
                f"{g.down}{compact_token_count(tokens_out)}"
            )
        parts.append(_Part(6, (_form(_tint("muted", label)),)))
    if inputs.cost:
        parts.append(_Part(6, (_form(_tint("muted", escape(inputs.cost))),)))

    # 7. Git branch and dirty mark.
    if inputs.branch:
        mark = "*" if inputs.dirty else ""
        label = (
            f"branch {inputs.branch}{' modified' if mark else ''}"
            if reader
            else f"{inputs.branch}{mark}"
        )
        parts.append(_Part(7, (_form(_tint("muted", escape(label))),)))
    return parts


def _layout(
    state: Sequence[_Form],
    parts: Sequence[_Part],
    right: _Form | None,
    sep_width: int,
    cols: int,
    room: int,
) -> tuple[int, list[tuple[int, int]], bool, int]:
    """Choose what fits *room* cells of a *cols*-wide terminal.

    Returns the state form, the ``(part, form)`` pairs shown, whether the
    right-hand hint shows, and the cells left over.
    """
    if cols < _STATE_ONLY_BELOW:
        visible = []
    elif cols < _RULES_BELOW:
        visible = [i for i, part in enumerate(parts) if part.priority <= _NARROW_PRIORITY]
    else:
        visible = list(range(len(parts)))
    show_right = right is not None and cols >= _RULES_BELOW
    chosen = dict.fromkeys(visible, 0)
    state_form = 0

    def width() -> int:
        total = state[state_form].width
        total += sum(sep_width + parts[i].forms[chosen[i]].width for i in visible)
        if show_right and right is not None:
            total += 2 + right.width
        return total

    while width() > room:
        if visible and parts[visible[-1]].priority >= _DROP_FIRST:
            chosen.pop(visible.pop())
            continue
        if show_right:
            show_right = False
            continue
        shortened = next(
            (i for i in reversed(visible) if chosen[i] + 1 < len(parts[i].forms)), None
        )
        if shortened is not None:
            chosen[shortened] += 1
            continue
        if state_form + 1 < len(state):
            state_form += 1
            continue
        if visible:
            chosen.pop(visible.pop())
            continue
        break
    return state_form, [(i, chosen[i]) for i in visible], show_right, room - width()


def _sweep(word: str, frame: int) -> str:
    """*word* as markup with one bold letter that moves back and forth per frame."""
    n = len(word)
    cycle = 2 * (n - 1)
    pos = frame % cycle if cycle > 0 else 0
    if pos >= n:
        pos = cycle - pos
    return "".join(
        f"[bold]{escape(ch)}[/bold]" if index == pos else escape(ch)
        for index, ch in enumerate(word)
    )


class StatusLine:
    """One prioritised status row (TUX-02 §2.1)."""

    def __init__(self, app_state: AppState, startup: StartupCoordinator | None = None) -> None:
        self._state = app_state
        self._startup = startup

    def height(self, cols: int) -> int:
        """The rows the status line occupies: always one."""
        return 1

    def render(self) -> RenderableType:
        from rich.text import Text

        return Text.from_markup(self.markup(_get_cols()), emoji=False)

    # ── state (segment 1, the only animated one) ─────────────────────────────

    def _state_forms(self, reader: bool) -> tuple[_Form, ...]:
        app = self._state
        conv = app.conversation
        g = glyphs()
        frame = _signal_int(conv, "frame")
        spinner = g.spinner[frame % len(g.spinner)] if _animated(app) else g.bullet

        def forms(
            token: str,
            lead: str,
            words: Sequence[str],
            elapsed: str = "",
            *,
            sweep: bool = False,
        ) -> tuple[_Form, ...]:
            out: list[_Form] = []
            for text in words:
                tail = f" {elapsed}" if elapsed else ""
                if reader:
                    out.append(_form(escape(f"{text}{tail}")))
                    continue
                word = _sweep(text, frame) if sweep and _animated(app) else escape(text)
                body = _tint(token, word) + (_tint("muted", tail) if tail else "")
                out.append(_form(f"{_tint(token, lead)} {body}" if lead else body))
            return tuple(out)

        if _signal_bool(conv, "terminal_waiting"):
            label = _signal_text(conv, "terminal_wait_label") or "terminal"
            elapsed = _fmt_elapsed(_signal_float(conv, "terminal_wait_elapsed_s"))
            return forms(
                "info",
                spinner,
                (f"Waiting for terminal: {label}", "Waiting for terminal", "Waiting"),
                elapsed,
            )
        if _signal(app, "pending_approval") is not None:
            return forms("warning", g.diamond, ("Waiting for you", "Waiting"))
        if _signal_bool(conv, "transcript_loading"):
            # The frame clock does not run while idle: a still bullet, no spinner.
            return forms("info", g.bullet, ("Loading transcript", "Loading"))
        # Shown from 0s while work runs, so the row never shifts on the first tick.
        elapsed = _fmt_elapsed(_cached_elapsed(conv))
        if _signal_bool(conv, "compaction_active"):
            return forms("warning", spinner, ("Compacting",), elapsed)
        agent_state = _signal(conv, "agent_state")
        name = str(getattr(agent_state, "name", "IDLE")).lower()
        if name == "running":
            tool = _signal_text(conv, "active_tool").strip()
            words = [f"Running {tool}", f"Running {tool.split()[0]}"] if tool else []
            return forms("info", spinner, (*dict.fromkeys(words), "Running"), elapsed)
        if name == "thinking":
            # The turning flower and one bold letter sweeping across the word.
            return forms("warning", spinner, ("Thinking",), elapsed, sweep=True)
        if name == "recovering":
            return forms("danger", spinner, ("Recovering",), elapsed)
        if name == "error":
            return forms("danger", g.fail, ("Error",))
        return forms("success", g.bullet, ("Idle",))

    # ── segments 2–7 ─────────────────────────────────────────────────────────

    def _inputs(self, reader: bool) -> _Inputs:
        app = self._state
        conv = app.conversation
        pool = _signal(conv, "subagent_pool_state")
        workers: tuple[int, int] | None = None
        if pool is not None:
            from agenthicc.subagents.pool import SubagentPoolState

            if isinstance(pool, SubagentPoolState):
                counts = pool_status_counts(pool)
                if counts.running + counts.queued + counts.needs_input > 0:
                    workers = (counts.done, pool.total)
        run = _signal(app, "workflow_run")
        workflow: tuple[int, int, str] | None = None
        model = _signal_text(conv, "model_name")
        if run is not None:
            from agenthicc.workflows.plugin import WorkflowRun

            if isinstance(run, WorkflowRun) and run.status == "running":
                workflow = (
                    run.current_phase_index + 1,
                    run.total_phases,
                    run.current_phase or "",
                )
                if run.current_phase_model:
                    model = run.current_phase_model
        from agenthicc.config import ExecutionSettings

        usage_status = _signal_text(conv, "usage_status", "unavailable")
        return _Inputs(
            reader=reader,
            needs_you=_signal_int(conv, "background_needs_you"),
            hub_key=_hub_key(app),
            background_running=_signal_int(conv, "background_running"),
            dnd=_signal_bool(conv, "background_dnd"),
            context_tokens=_signal_int(conv, "context_tokens"),
            context_window=_signal_int(conv, "context_window"),
            model=short_model(model, ExecutionSettings.provider) if model else "",
            workers=workers,
            workflow=workflow,
            workflow_override=_signal_text(conv, "workflow_override"),
            startup=self._startup_text(),
            terminal_count=(
                _signal_int(conv, "terminal_running_count")
                if _signal_bool(conv, "terminal_waiting")
                else 0
            ),
            usage_status=usage_status,
            tokens=(_signal_int(conv, "tokens_in"), _signal_int(conv, "tokens_out")),
            cost=cost_text(
                _signal_float(conv, "cost_usd"), _signal_text(conv, "cost_status", "unavailable")
            ),
            branch=_signal_text(conv, "git_branch"),
            dirty=_signal_bool(conv, "git_dirty"),
            user_line=_signal_text(conv, "statusline_text"),
            theme=current_theme(),
        )

    def _startup_text(self) -> str:
        """Startup work that is not ready yet, without exception details."""
        if self._startup is None:
            return ""
        pending = [report for report in self._startup.snapshot() if report.state.value != "ready"]
        if not pending:
            return ""
        text = ", ".join(f"{report.name} {report.state.value}" for report in pending[:2])
        return text + (f", +{len(pending) - 2} more" if len(pending) > 2 else "")

    def _right(self, reader: bool) -> _Form | None:
        if reader:
            return None
        from agenthicc.tui.keymap import Hint, KeymapError

        try:
            hint = self._state.keymap().hint("composer.shortcuts")
        except (KeymapError, AttributeError, TypeError):
            hint = None
        if isinstance(hint, Hint):
            return _form(f"{escape(hint.keys)} {_tint('muted', escape(hint.label))}")
        return _form(_tint("muted", "/help"))

    # ── the row ───────────────────────────────────────────────────────────────

    def markup(self, cols: int) -> str:
        """The status line as markup, at most *cols* cells wide."""
        reader = _reader(self._state)
        state = self._state_forms(reader)
        parts = _static_parts(self._inputs(reader))
        separator = ", " if reader else f" {_tint('muted', glyphs().sep)} "
        sep_width = _vlen(separator)
        right = self._right(reader)
        lead = "" if reader else " "
        width = max(1, cols - len(lead))
        state_form, chosen, show_right, room = _layout(state, parts, right, sep_width, cols, width)
        pieces = [state[state_form].markup]
        pieces += [parts[i].forms[form].markup for i, form in chosen]
        placed = {i for i, _ in chosen}
        croppable = next(
            (i for i, part in enumerate(parts) if part.croppable and i not in placed), None
        )
        if croppable is not None and room - sep_width >= 8 and cols >= _STATE_ONLY_BELOW:
            pieces.append(_fit(parts[croppable].forms[0].markup, room - sep_width))
            room -= sep_width + min(parts[croppable].forms[0].width, room - sep_width)
        line = lead + separator.join(pieces)
        if show_right and right is not None:
            line += " " * max(2, room + 2) + right.markup
        return _fit(line, cols)

    def sentence(self) -> str:
        """The status as a screen reader hears it: ``Idle. Opus 4.8. Context 12 percent.``"""
        return status_sentence(self._state)


def status_sentence(app_state: AppState) -> str:
    """The state, attention, model and context as sentences (AGX-13 announcer)."""
    from agenthicc.config import ExecutionSettings
    from agenthicc.tui.attention_labels import sessions_need_you

    conv = app_state.conversation
    status = StatusLine(app_state)._state_forms(reader=True)[-1]
    from rich.text import Text

    sentences = [Text.from_markup(status.markup).plain]
    needs = _signal_int(conv, "background_needs_you")
    if needs:
        sentences.append(sessions_need_you(needs).capitalize())
    model = _signal_text(conv, "model_name")
    if model:
        sentences.append(spoken_model(short_model(model, ExecutionSettings.provider)))
    percent = context_percent(
        _signal_int(conv, "context_tokens"), _signal_int(conv, "context_window")
    )
    if percent is not None:
        sentences.append(f"Context {percent} percent")
    return " ".join(f"{sentence}." for sentence in sentences)


# ── ComposerComponent ─────────────────────────────────────────────────────────

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
def _line_rows(
    text: str, cursor: int | None, width: int, cursor_glyph: str
) -> tuple[tuple[str, int | None], ...]:
    """Fold one logical line, marking the row and column that hold the cursor.

    The cursor glyph is part of the folded text, as it occupies a cell. Only
    an edited line or the cursor's line misses this cache on a keystroke.
    """
    if cursor is None:
        return tuple((row, None) for row in _fold(text, width))
    display = text[:cursor] + cursor_glyph + text[cursor:]
    rows: list[tuple[str, int | None]] = []
    offset = 0
    single = text.isascii() and cell_len(cursor_glyph) == len(cursor_glyph)
    for row in _fold(display, width, single):
        column = cursor - offset if offset <= cursor < offset + len(row) else None
        rows.append((row, column))
        offset += len(row)
    return tuple(rows)


class _Row:
    """One display row of the composer."""

    __slots__ = ("prefix", "text", "cursor", "placeholder")

    def __init__(self, prefix: str, text: str, cursor: int | None, placeholder: str = "") -> None:
        self.prefix = prefix
        self.text = text
        self.cursor = cursor
        self.placeholder = placeholder

    def segments(self) -> Line:
        g = glyphs()
        prefix = Segment(
            self.prefix, style("accent", bold=True) if self.prefix.startswith(g.prompt) else None
        )
        tail = (Segment(self.placeholder, style("muted")),) if self.placeholder else ()
        if self.cursor is None:
            return (prefix, Segment(self.text), *tail)
        return (
            prefix,
            Segment(self.text[: self.cursor]),
            Segment(g.cursor, Style(bold=True)),
            Segment(self.text[self.cursor + len(g.cursor) :]),
            *tail,
        )


def _hidden_row(arrow: str, count: int) -> Line:
    return (Segment(f"  {arrow} {count} more line{'s' if count != 1 else ''}", _HIDDEN_STYLE),)


def _placeholder(width: int) -> str:
    """The longest placeholder form that fits *width* cells, or ``""``."""
    return next((text for text in PLACEHOLDER if cell_len(text) <= width), "")


class _ComposerView:
    """The composer as a Rich renderable, laid out at the width it is given."""

    def __init__(self, composer: ComposerComponent) -> None:
        self._composer = composer

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> Iterator[Segment]:
        for line in self._composer.lines(options.max_width):
            yield from line
            yield Segment.line()


class ComposerComponent:
    """Renders ``❯ text▌`` from InputState signals.

    Each logical line is folded at the given width (continuation rows are
    indented under the prompt). When the prompt needs more than ``max_rows``
    rows, only the rows around the cursor are drawn, with ``↑ N more lines`` /
    ``↓ N more lines`` in place of the rest (AGX-11). An empty prompt shows a
    muted placeholder after the cursor (TUX-02).
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
        g = glyphs()
        width = max(1, cols - 2)
        inp = self._state.input
        buf = inp.buf()
        cursor = inp.cursor()
        if inp.paste_condensed():
            label = inp.paste_label()
            if not (buf and label and label in "".join(buf)):
                # Callers that set only a paste label show the label.
                buf, cursor = list(label), len(label)
        elif not buf:
            hint = "" if _reader(self._state) else _placeholder(width - cell_len(g.cursor))
            return [_Row(f"{g.prompt} ", "", 0, hint)], 0
        rows: list[_Row] = []
        cursor_row = 0
        start = 0
        for index, line in enumerate("".join(buf).split("\n")):
            end = start + len(line)
            column = cursor - start if start <= cursor <= end else None
            folded = _line_rows(line, column, width, g.cursor)
            for fold_index, (text, row_cursor) in enumerate(folded):
                if row_cursor is not None:
                    cursor_row = len(rows)
                prefix = f"{g.prompt} " if index == 0 and fold_index == 0 else "  "
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
        g = glyphs()
        lines: list[Line] = []
        if top > 0:
            lines.append(_hidden_row(g.up, top))
        lines.extend(row.segments() for row in rows[top : top + visible])
        if top + visible < total:
            lines.append(_hidden_row(g.down, total - top - visible))
        return tuple(lines)


# ── ComposerFrame ─────────────────────────────────────────────────────────────


class ComposerFrame:
    """The composer's frame, its title and mode badge, and its hint row (TUX-02)."""

    def __init__(self, app_state: AppState, composer: ComposerComponent) -> None:
        self._state = app_state
        self.composer = composer

    def form(self, cols: int) -> str:
        """``box``, ``rules`` or ``none`` (screen reader) at *cols* columns."""
        ui = _ui(self._state)
        if ui is not None and ui.screen_reader:
            return "none"
        if cols < _RULES_BELOW or (ui is not None and ui.composer_frame == "rules"):
            return "rules"
        return "box"

    def inner_width(self, cols: int) -> int:
        return max(1, cols - 4) if self.form(cols) == "box" else cols

    def height(self, cols: int) -> int:
        borders = 0 if self.form(cols) == "none" else 2
        return borders + self.composer.height(self.inner_width(cols))

    def _title(self) -> str:
        title = _signal_text(self._state.conversation, "session_title")
        return title or UNTITLED

    def _badge(self) -> tuple[str, int]:
        mode = self._state.active_mode()
        name = str(mode.name)
        agent = _signal(self._state, "agent")
        agent_text = (
            f"{escape(agent)} {glyphs().sep} "
            if isinstance(agent, str) and agent not in ("", "default")
            else ""
        )
        markup = agent_text + _tint(_mode_token(name), escape(name), bold=True)
        return markup, _vlen(markup)

    def top(self, cols: int, form: str | None = None) -> str:
        """The top border: a rule ending ``title · Safe ─``, or ``╭─ title ── Safe ─╮``."""
        form = form or self.form(cols)
        if form == "rules":
            return self._rule_top(cols)
        g = glyphs()
        tl, tr, _bl, _br, h, _v = g.box
        left_corner, right_corner = (tl, tr) if form == "box" else (h, h)
        badge, badge_width = self._badge()
        # "╭─ " + title + " " + fill + " " + badge + " ─╮"
        fixed = cell_len(left_corner) + 3 + 1 + badge_width + 2 + cell_len(right_corner)
        room = cols - fixed - 1  # at least one fill cell
        title = self._title()
        if room < cell_len(g.ellipsis) + 1:
            title = ""
        elif cell_len(title) > room:
            title = _cut(title, room - cell_len(g.ellipsis)) + g.ellipsis
        if room + badge_width + 2 < 0:
            badge, badge_width = "", 0
        head = f"{left_corner}{h} "
        title_part = f"{_tint('muted', escape(title))} " if title else ""
        used = cell_len(head) + (cell_len(title) + 1 if title else 0)
        tail_width = (badge_width + 2 if badge else 0) + cell_len(h) + cell_len(right_corner)
        fill = max(0, cols - used - tail_width)
        badge_part = f" {badge} " if badge else ""
        line = (
            _tint("subtle", head)
            + title_part
            + _tint("subtle", h * fill)
            + badge_part
            + _tint("subtle", f"{h}{right_corner}")
        )
        return _fit(line, cols)

    def _rule_top(self, cols: int) -> str:
        """Claude Code's input rule: ``──────── title · Safe ─``, right-aligned."""
        g = glyphs()
        h = g.box[4]
        badge, badge_width = self._badge()
        sep = f" {g.sep} "
        # fill (at least 3) + " " + title + sep + badge + " " + h
        room = cols - 3 - 1 - cell_len(sep) - badge_width - 1 - cell_len(h)
        title = self._title()
        if room < cell_len(g.ellipsis) + 1:
            title = ""
        elif cell_len(title) > room:
            title = _cut(title, room - cell_len(g.ellipsis)) + g.ellipsis
        if title:
            label = f"{_tint('muted', escape(title))}{_tint('muted', sep)}{badge}"
            label_width = cell_len(title) + cell_len(sep) + badge_width
        elif cols - 3 - badge_width - 2 - cell_len(h) >= 0:
            label, label_width = badge, badge_width
        else:
            label, label_width = "", 0
        if not label:
            return _tint("subtle", h * cols)
        fill = max(0, cols - label_width - 2 - cell_len(h))
        line = _tint("subtle", h * fill) + f" {label} " + _tint("subtle", h)
        return _fit(line, cols)

    def bottom(self, cols: int, form: str | None = None) -> str:
        form = form or self.form(cols)
        g = glyphs()
        _tl, _tr, bl, br, h, _v = g.box
        if form == "box":
            return _tint("subtle", bl + h * max(0, cols - 2) + br)
        return _tint("subtle", h * cols)

    def body(self, cols: int, form: str | None = None) -> list[Line]:
        """The composer rows, inside the box's side borders when boxed."""
        form = form or self.form(cols)
        if form != "box":
            return list(self.composer.lines(cols))
        inner = self.inner_width(cols)
        edge = style("subtle")
        v = glyphs().box[5]
        rows: list[Line] = []
        for line in self.composer.lines(inner):
            used = sum(cell_len(segment.text) for segment in line)
            rows.append(
                (
                    Segment(f"{v} ", edge),
                    *line,
                    Segment(" " * max(0, inner - used)),
                    Segment(f" {v}", edge),
                )
            )
        return rows

    def hint_row(self, cols: int) -> str | None:
        """The contextual hints (a turn running, a condensed paste), or ``None`` idle."""
        app = self._state
        conv = app.conversation
        reader = _reader(app)
        if _signal_bool(conv, "terminal_waiting"):
            hints: tuple[tuple[str, str], ...] = (
                ("Esc", "interrupt"),
                ("/ps", "view"),
                ("/stop", "stop all"),
            )
            return _hint_row(hints, cols, reader, current_theme())
        conditions: set[str] = set()
        if _signal_bool(conv, "is_running"):
            conditions.add("running")
        if app.input.paste_condensed() is True:
            conditions.update(("paste", "paste_at_cursor"))
        pool = _signal(conv, "subagent_pool_state")
        if pool is not None:
            conditions.add("subagents")
            from agenthicc.subagents.pool import SubagentPoolState

            if isinstance(pool, SubagentPoolState) and pool.active:
                conditions.add("subagents_running")
        keymap = app.keymap()
        found = tuple(
            (hint.keys, hint.label)
            for hint in keymap.hints("composer", frozenset(conditions))
            if keymap.action(hint.action).when
        )
        return _hint_row(found, cols, reader, current_theme()) if found else None


def _cut(text: str, width: int) -> str:
    out: list[str] = []
    used = 0
    for character in text:
        size = cell_len(character)
        if used + size > width:
            break
        out.append(character)
        used += size
    return "".join(out).rstrip()


# ── rows shared by the workspace ──────────────────────────────────────────────


@functools.lru_cache(maxsize=256)
def _hint_row(hints: tuple[tuple[str, str], ...], cols: int, plain: bool, theme: Theme) -> str:
    """Render ``(keys, label)`` hints in priority order, dropping the last to fit."""
    segs = [f"[bold]{escape(keys)}[/bold] {_tint('muted', escape(label))}" for keys, label in hints]
    sep = ", " if plain else f" {_tint('muted', theme.glyphs.sep)} "
    lead = "" if plain else " "
    while len(segs) > 1 and _vlen(lead + sep.join(segs)) > cols:
        segs.pop()
    return _fit(lead + sep.join(segs), cols)


def notice_rows(app_state: AppState, cols: int) -> tuple[str, ...]:
    """A notification, the ``/loop`` status or an attention toast, wrapped whole."""
    conv = app_state.conversation
    for name in ("notification", "loop_status", "background_toast"):
        text = _signal(conv, name)
        if isinstance(text, str) and text:
            return _notification_rows(text, cols, current_theme())
    return ()


@functools.lru_cache(maxsize=256)
def _notification_rows(message: str, cols: int, theme: Theme) -> tuple[str, ...]:
    """Return a wrapped notification as muted rows, one per terminal row."""
    return tuple(_tint("muted", escape(row.plain)) for row in _wrap_notification(message, cols))


def _wrap_notification(message: str, cols: int) -> list[Text]:
    """Wrap a notification without discarding any of its text.

    Notifications can contain workflow run IDs, which are long unbroken
    strings; cutting them would make the recovery command unusable. Rich's
    ``Text.wrap(..., overflow="fold")`` keeps every character, wrapping at
    spaces where possible and folding a token wider than the terminal.
    """
    from rich.console import Console
    from rich.text import Text

    width = max(1, cols)
    return list(
        Text(message).wrap(
            Console(width=width, color_system=None, force_terminal=False),
            width=width,
            overflow="fold",
        )
    )
