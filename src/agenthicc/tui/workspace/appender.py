"""ScrollBufferAppender — the ONLY component allowed to call console.print().

Every ConversationEvent is rendered exactly once to stdout (above the always-on
Live region).  Rich's Console.print() while a Live block is active automatically
inserts content above the Live block.

This eliminates:
- _on_tool_complete / _on_assistant_complete ad-hoc prints
- flush_from_model() and _printed_count tracking
- duplicate tool call rendering
- spinner-state / background-thread races

Architecture: PRD-60 §7, PRD-66 §3.
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Callable

from agenthicc.tui.conversation_store import ConversationEvent

# ── event renderer registry ───────────────────────────────────────────────────
# Maps ConversationEvent.kind → bound-method-compatible handler.
# Register new event kinds by calling register_renderer() from any module that
# defines a new ConversationEvent kind — no edits to _render_one() needed.
_EventRenderer = Callable[["ScrollBufferAppender", ConversationEvent], None]
_RENDERERS: dict[str, _EventRenderer] = {}


def register_renderer(kind: str) -> Callable[[_EventRenderer], _EventRenderer]:
    """Decorator: register a function as the renderer for *kind* events."""
    def decorator(fn: _EventRenderer) -> _EventRenderer:
        _RENDERERS[kind] = fn
        return fn
    return decorator

if TYPE_CHECKING:
    from rich.console import Console
    from agenthicc.tui.conversation_store import AppState


_TOOL_OP: dict[str, str] = {
    "write_file":  "Update",
    "patch_file":  "Update",
    "append_file": "Append",
}

_LANG_MAP: dict[str, str] = {
    "py": "python", "js": "javascript", "ts": "typescript",
    "jsx": "jsx",   "tsx": "tsx",       "json": "json",
    "yaml": "yaml", "yml": "yaml",      "toml": "toml",
    "md": "markdown", "sh": "bash",     "rs": "rust",
    "go": "go",     "rb": "ruby",       "c": "c",
    "cpp": "cpp",   "h": "c",           "cs": "csharp",
    "java": "java", "kt": "kotlin",     "swift": "swift",
    "html": "html", "css": "css",       "sql": "sql",
}


def _lang_for_path(path: str) -> str:
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return _LANG_MAP.get(ext, "text")


def _hhmmss(ts: float) -> str:
    import time
    return time.strftime("%H:%M:%S", time.localtime(ts))


def _fmt_args(args: dict) -> str:
    if not args:
        return ""
    from rich.markup import escape as _e
    items = list(args.items())
    if len(items) == 1:
        return f"[dim]({_e(repr(items[0][1])[:60])})[/dim]"
    return "[dim](" + ", ".join(
        f"{_e(k)}={_e(repr(v)[:25])}" for k, v in items[:3]
    ) + ")[/dim]"


class ScrollBufferAppender:
    """Subscribes to ConversationStore events and renders them to stdout.

    Instantiate once, call mount() after the Live block is started.
    All rendering is queued via call_soon_threadsafe to ensure it happens
    on the asyncio event-loop thread (not on background threads).
    """

    def __init__(self, app_state: AppState, console: Console, max_live_tool_calls: int = 5) -> None:
        self._state:   AppState                  = app_state
        self._console: Console                   = console
        self._unsub:   Callable[[], None] | None = None
        self._max_tool_calls: int = max_live_tool_calls
        # Small batch buffer to coalesce rapid events (e.g. many tool completions)
        self._pending: list[ConversationEvent] = []
        self._flush_scheduled = False
        # Tracks how many tool_complete events have been seen in the current
        # consecutive group.  Mirrors ConversationStore.tool_group_count but
        # maintained locally so it is accurate during _flush_batch regardless
        # of what other events in the same batch have already reset the signal.
        self._group_count: int = 0

    def mount(self) -> None:
        self._unsub = self._state.conversation.on_event(self._queue_event)

    def unmount(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None

    # ── event queuing ─────────────────────────────────────────────────────────

    def _queue_event(self, ev: ConversationEvent) -> None:
        self._pending.append(ev)
        if not self._flush_scheduled:
            self._flush_scheduled = True
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.call_soon(self._flush_batch)
                else:
                    self._flush_batch()
            except RuntimeError:
                self._flush_batch()

    def _flush_batch(self) -> None:
        self._flush_scheduled = False
        pending, self._pending = self._pending, []
        # Wrap the entire batch in one console context so all console.print()
        # calls share a single write() + flush().  Without this, each print
        # triggers its own erase-Live-Block → write-content → redraw-Live-Block
        # cycle, causing the status bar to flicker once per event.
        with self._console:
            for ev in pending:
                if not ev.rendered:
                    ev.rendered = True
                    try:
                        self._render_one(ev)
                    except Exception:   # noqa: BLE001
                        pass

    # ── rendering ─────────────────────────────────────────────────────────────

    def _render_one(self, ev: ConversationEvent) -> None:
        """Dispatch to the registered renderer for ev.kind; silently ignore unknown kinds."""
        renderer = _RENDERERS.get(ev.kind)
        if renderer is not None:
            renderer(self, ev)

    def _flush_group_summary(self) -> None:
        """Close the current tool group: reset the live signal, print permanent record."""
        overflow = self._group_count - self._max_tool_calls
        self._group_count = 0
        # Always clear the live footer indicator first.
        self._state.conversation.live_tool_overflow.set(0)
        if overflow > 0:
            word = "call" if overflow == 1 else "calls"
            self._console.print(
                f"  [dim]⎿ ...and {overflow} more tool {word}[/dim]",
                markup=True, highlight=False,
            )

    def _render_tool_complete(self, payload: dict) -> None:
        from rich.markup import escape as _e
        name     = _e(payload.get("name", ""))
        args_str = payload.get("args_str", "")
        icon     = "[green]✓[/green]" if payload.get("success", True) else "[red]✗[/red]"
        dur      = payload.get("dur_str", "")
        self._console.print(
            f"  [dim]⎿[/dim] [bold]{name}[/bold]{args_str}  {icon}{dur}",
            markup=True, highlight=False,
        )
        for ln in payload.get("output_lines", [])[:4]:
            self._console.print(
                f"    [dim]{_e(str(ln)[:120])}[/dim]",
                markup=True, highlight=False,
            )
        if len(payload.get("output_lines", [])) > 4:
            extra = len(payload["output_lines"]) - 4
            self._console.print(
                f"    [dim](+{extra} more lines)[/dim]",
                markup=True, highlight=False,
            )

    # ── idle status header (printed once before each new prompt) ──────────────

    def print_idle_header(self) -> None:
        """Print session info + separator before the input prompt."""
        import shutil
        conv  = self._state.conversation
        cols  = shutil.get_terminal_size((80, 24)).columns
        sid   = conv.session_id() or "session"
        turns = conv.turn_count()
        cost  = f"${conv.cost_usd():.3f}"
        self._console.print(
            f" [dim]{sid}  |  {turns} turn{'s' if turns != 1 else ''}  |  {cost}[/dim]"
            f"  [cyan]↑ {conv.tokens_in():,}[/cyan]"
            f"  [green]↓ {conv.tokens_out():,}[/green]",
            markup=True, highlight=False,
        )
        self._console.print(f"[dim]{'─' * cols}[/dim]", markup=True, highlight=False)


# ── built-in renderers — registered at module load time, after class is complete ──

@register_renderer("turn_start")
def _render_turn_start(self: ScrollBufferAppender, ev: ConversationEvent) -> None:
    self._group_count = 0
    agent_name = ev.payload.get("agent_name", "assistant")
    self._console.print(
        f"[bold cyan]●[/bold cyan] [bold]{agent_name}[/bold]"
        f"  [dim]{_hhmmss(ev.timestamp)}[/dim]",
        markup=True, highlight=False,
    )


@register_renderer("user_message")
def _render_user_message(self: ScrollBufferAppender, ev: ConversationEvent) -> None:
    from rich.markup import escape as _e  # noqa: PLC0415
    text = ev.payload.get("text", "")
    self._console.print(
        f"[bold yellow]❯[/bold yellow] {_e(text)}",
        markup=True, highlight=False,
        style="on grey11",
    )
    self._console.print()


@register_renderer("tool_complete")
def _render_tool_complete_ev(self: ScrollBufferAppender, ev: ConversationEvent) -> None:
    self._group_count += 1
    if self._group_count <= self._max_tool_calls:
        self._render_tool_complete(ev.payload)
    else:
        overflow = self._group_count - self._max_tool_calls
        self._state.conversation.live_tool_overflow.set(overflow)


@register_renderer("text")
def _render_text(self: ScrollBufferAppender, ev: ConversationEvent) -> None:
    self._flush_group_summary()
    text = ev.payload.get("text", "")
    if text.strip():
        from rich.markdown import Markdown  # noqa: PLC0415
        self._console.print(Markdown(text), end="")


@register_renderer("thinking_step")
def _render_thinking_step(self: ScrollBufferAppender, ev: ConversationEvent) -> None:
    from rich.markup import escape as _e  # noqa: PLC0415
    step = ev.payload.get("step", "")
    done = ev.payload.get("done", False)
    icon = "[green]✓[/green]" if done else "[yellow]→[/yellow]"
    self._console.print(
        f"  {icon} [dim]{_e(step)}[/dim]",
        markup=True, highlight=False,
    )


@register_renderer("file_modified")
def _render_file_modified(self: ScrollBufferAppender, ev: ConversationEvent) -> None:
    from agenthicc.tui.diff_renderer import render_file_diff  # noqa: PLC0415
    path      = ev.payload.get("path", "")
    old_lines = ev.payload.get("old_lines")
    new_lines = ev.payload.get("new_lines")
    tool      = ev.payload.get("tool", "write_file")
    op        = _TOOL_OP.get(tool, "Update")
    if old_lines is not None and new_lines is not None:
        self._console.print(
            render_file_diff(path, old_lines, new_lines, operation=op,
                             language=_lang_for_path(path)),
            highlight=False,
        )
    else:
        from rich.markup import escape as _e  # noqa: PLC0415
        self._console.print(
            f"  [dim]{op}:[/dim] [cyan]{_e(path)}[/cyan]",
            markup=True, highlight=False,
        )


@register_renderer("error")
def _render_error(self: ScrollBufferAppender, ev: ConversationEvent) -> None:
    self._flush_group_summary()
    from rich.markup import escape as _e  # noqa: PLC0415
    msg    = ev.payload.get("message", "")
    detail = ev.payload.get("detail", "")
    self._console.print(
        f"\n[red bold]ERROR[/red bold] {_e(msg)}",
        markup=True, highlight=False,
    )
    if detail:
        self._console.print(
            f"[dim]{_e(detail[:500])}[/dim]",
            markup=True, highlight=False,
        )


@register_renderer("turn_complete")
def _render_turn_complete(self: ScrollBufferAppender, ev: ConversationEvent) -> None:
    self._console.print()


@register_renderer("mention_chips")
def _render_mention_chips(self: ScrollBufferAppender, ev: ConversationEvent) -> None:
    from rich.markup import escape as _e  # noqa: PLC0415
    for chip in ev.payload.get("chips", []):
        raw     = chip.get("raw", "")
        preview = chip.get("content_preview", "")
        self._console.print(
            f"  [dim]@[/dim][cyan]{_e(raw.lstrip('@'))}[/cyan]"
            + (f"  [dim]{_e(preview[:60])}[/dim]" if preview else ""),
            markup=True, highlight=False,
        )
