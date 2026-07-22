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
    "modify_file": "Update",
    "write_file": "Update",
    "patch_file": "Update",
    "append_file": "Append",
    "delete_file": "Delete",
    "move_file": "Move",
    "copy_file": "Copy",
    "make_directory": "Create",
    "touch_file": "Create",
    "truncate_file": "Update",
    "apply_diff": "Update",
}

_TOOL_DISPLAY_OP: dict[str, str] = {
    "read_file": "Read",
    "read_lines": "Read",
    "batch_read": "Read",
    "list_directory": "List",
    "search_files": "Search",
    "grep_file": "Search",
    "grep_files": "Search",
    "file_exists": "Check",
    "get_file_info": "Inspect",
    "checksum_file": "Checksum",
    "shell": "Run",
    "run_bash": "Run",
    "run_command": "Run",
    "run_python": "Run",
    "run_python_expr": "Run",
    "run_tests": "Test",
    "git_status": "Status",
    "git_diff": "Diff",
    "git_log": "Log",
    "git_show": "Show",
}

_LANG_MAP: dict[str, str] = {
    "py": "python",
    "js": "javascript",
    "ts": "typescript",
    "jsx": "jsx",
    "tsx": "tsx",
    "json": "json",
    "yaml": "yaml",
    "yml": "yaml",
    "toml": "toml",
    "md": "markdown",
    "sh": "bash",
    "rs": "rust",
    "go": "go",
    "rb": "ruby",
    "c": "c",
    "cpp": "cpp",
    "h": "c",
    "cs": "csharp",
    "java": "java",
    "kt": "kotlin",
    "swift": "swift",
    "html": "html",
    "css": "css",
    "sql": "sql",
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
    return "[dim](" + ", ".join(f"{_e(k)}={_e(repr(v)[:25])}" for k, v in items[:3]) + ")[/dim]"


def _tool_display_operation(name: str) -> str:
    """Return the user-facing operation verb for a tool completion."""
    operation = _TOOL_OP.get(name, _TOOL_DISPLAY_OP.get(name))
    if operation is not None:
        return operation
    return name.replace("_", " ").title() if name else "Tool"


def _line_count_label(count: int) -> str:
    word = "line" if count == 1 else "lines"
    return f"{count} {word}"


class ScrollBufferAppender:
    """Subscribes to ConversationStore events and renders them to stdout.

    Instantiate once, call mount() after the Live block is started.
    All rendering is queued via call_soon_threadsafe to ensure it happens
    on the asyncio event-loop thread (not on background threads).
    """

    def __init__(self, app_state: AppState, console: Console, max_live_tool_calls: int = 5) -> None:
        self._state: AppState = app_state
        self._console: Console = console
        self._unsub: Callable[[], None] | None = None
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
                    except Exception:  # noqa: BLE001
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
                markup=True,
                highlight=False,
            )

    def _render_tool_complete(self, payload: dict[str, object]) -> None:
        from rich.markup import escape as _e

        name = str(payload.get("name", ""))
        args_str = str(payload.get("args_str", ""))
        success = bool(payload.get("success", True))
        operation = _tool_display_operation(name)
        output_lines_raw = payload.get("output_lines", [])
        output_lines = (
            [str(line) for line in output_lines_raw] if isinstance(output_lines_raw, list) else []
        )
        preview_lines = output_lines[:4]
        output_more_raw = payload.get("output_more", 0)
        output_more = int(output_more_raw) if isinstance(output_more_raw, int) else 0
        output_more = max(output_more, len(output_lines) - len(preview_lines))
        output_count = len(preview_lines) + output_more
        dur = str(payload.get("dur_str", ""))

        self._console.print(
            f"[green]●[/green] [bold]{_e(operation)}[/bold]{args_str}",
            markup=True,
            highlight=False,
        )
        status = "[green]Completed[/green]" if success else "[red]Failed[/red]"
        count = f"  [dim]{_line_count_label(output_count)}[/dim]" if output_count else ""
        self._console.print(
            f"[dim]└─[/dim] {status}{dur}{count}",
            markup=True,
            highlight=False,
        )

        for index, ln in enumerate(preview_lines, 1):
            self._console.print(
                f"  [dim]{index:>4}[/dim]   {_e(ln[:120])}",
                markup=True,
                highlight=False,
            )
        if output_more > 0:
            self._console.print(
                f"  [dim]⋯ +{output_more} more lines[/dim]",
                markup=True,
                highlight=False,
            )
        # Keep each tool call visually self-contained: output belongs directly
        # beneath its completion summary, with one separator after the call.
        self._console.print()

    # ── idle status header (printed once before each new prompt) ──────────────

    def print_idle_header(self) -> None:
        """Print session info + separator before the input prompt."""
        import shutil

        conv = self._state.conversation
        cols = shutil.get_terminal_size((80, 24)).columns
        sid = conv.session_id() or "session"
        turns = conv.turn_count()
        cost = f"${conv.cost_usd():.3f}"
        self._console.print(
            f" [dim]{sid}  |  {turns} turn{'s' if turns != 1 else ''}  |  {cost}[/dim]"
            f"  [cyan]↑ {conv.tokens_in():,}[/cyan]"
            f"  [green]↓ {conv.tokens_out():,}[/green]",
            markup=True,
            highlight=False,
        )
        self._console.print(f"[dim]{'─' * cols}[/dim]", markup=True, highlight=False)


# ── built-in renderers — registered at module load time, after class is complete ──


@register_renderer("turn_start")
def _render_turn_start(self: ScrollBufferAppender, ev: ConversationEvent) -> None:
    self._group_count = 0
    agent_name = ev.payload.get("agent_name", "assistant")
    self._console.print(
        f"[bold cyan]●[/bold cyan] [bold]{agent_name}[/bold]  [dim]{_hhmmss(ev.timestamp)}[/dim]",
        markup=True,
        highlight=False,
    )


@register_renderer("user_message")
def _render_user_message(self: ScrollBufferAppender, ev: ConversationEvent) -> None:
    from rich.markup import escape as _e  # noqa: PLC0415

    text = ev.payload.get("text", "")
    self._console.print(
        f"[bold yellow]❯[/bold yellow] {_e(text)}",
        markup=True,
        highlight=False,
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
        markup=True,
        highlight=False,
    )


@register_renderer("file_modified")
def _render_file_modified(self: ScrollBufferAppender, ev: ConversationEvent) -> None:
    from agenthicc.tui.diff_renderer import render_file_diff, render_file_create  # noqa: PLC0415

    path = ev.payload.get("path", "")
    old_lines = ev.payload.get("old_lines")
    new_lines = ev.payload.get("new_lines")
    tool = ev.payload.get("tool", "write_file")
    lang = _lang_for_path(path)

    if old_lines is not None and new_lines is not None:
        if old_lines == []:
            # File creation — show compact preview capped at 10 lines.
            self._console.print(
                render_file_create(path, new_lines, language=lang),
                highlight=False,
            )
        else:
            op = _TOOL_OP.get(tool, "Update")
            self._console.print(
                render_file_diff(path, old_lines, new_lines, operation=op, language=lang),
                highlight=False,
            )
    else:
        from rich.markup import escape as _e  # noqa: PLC0415

        op = _TOOL_OP.get(tool, "Update")
        self._console.print(
            f"  [dim]{op}:[/dim] [cyan]{_e(path)}[/cyan]",
            markup=True,
            highlight=False,
        )


@register_renderer("error")
def _render_error(self: ScrollBufferAppender, ev: ConversationEvent) -> None:
    self._flush_group_summary()
    from rich.markup import escape as _e  # noqa: PLC0415

    msg = ev.payload.get("message", "")
    detail = ev.payload.get("detail", "")
    self._console.print(
        f"\n[red bold]ERROR[/red bold] {_e(msg)}",
        markup=True,
        highlight=False,
    )
    if detail:
        self._console.print(
            f"[dim]{_e(detail[:500])}[/dim]",
            markup=True,
            highlight=False,
        )


def _fmt_worked(seconds: float) -> str:
    """Format elapsed seconds as a human-readable worked-for string."""
    s = int(seconds)
    if s < 60:
        return f"{s} second{'s' if s != 1 else ''}"
    m, sec = divmod(s, 60)
    mins = f"{m} min{'s' if m != 1 else ''}"
    return f"{mins} {sec} second{'s' if sec != 1 else ''}" if sec else mins


@register_renderer("turn_complete")
def _render_turn_complete(self: ScrollBufferAppender, ev: ConversationEvent) -> None:
    elapsed = float(ev.payload.get("elapsed_s", 0.0))
    if elapsed >= 1.0:
        self._console.print(
            f"[dim]✾ Worked for {_fmt_worked(elapsed)}[/dim]",
            markup=True,
            highlight=False,
        )
    self._console.print()


@register_renderer("mention_chips")
def _render_mention_chips(self: ScrollBufferAppender, ev: ConversationEvent) -> None:
    from rich.markup import escape as _e  # noqa: PLC0415

    for chip in ev.payload.get("chips", []):
        raw = chip.get("raw", "")
        kind = chip.get("kind", "file")
        ok = chip.get("ok", True)
        path = _e(raw.lstrip("@"))

        # Verb mirrors what actually happened: Read for files/globs/URLs,
        # Listed for directories.  Failed resolutions show the error glyph.
        if kind == "directory":
            verb = "Listed"
            color = "cyan"
        elif kind == "url":
            verb = "Fetched"
            color = "cyan"
        else:
            verb = "Read"
            color = "cyan"

        icon = "" if ok else "[red]✗[/red] "
        self._console.print(
            f"  [dim]⎿[/dim] {icon}[bold]{verb}[/bold]([{color}]{path}[/{color}])",
            markup=True,
            highlight=False,
        )


# ── Generic system text (compactor, etc.) ─────────────────────────────────────


@register_renderer("system")
def _render_system(self: ScrollBufferAppender, ev: ConversationEvent) -> None:
    from rich.markup import escape as _e  # noqa: PLC0415

    text = str(ev.payload.get("text", ""))
    if text:
        self._console.print(f"[dim]{_e(text)}[/dim]", markup=True, highlight=False)
        self._console.print()


# ── Subagent pool renderers (PRD-124 Phase 3) ─────────────────────────────────


@register_renderer("subagent_pool_started")
def _render_subagent_pool_started(self: ScrollBufferAppender, ev: ConversationEvent) -> None:
    from rich.markup import escape as _e  # noqa: PLC0415

    total = ev.payload.get("total", 0)
    workers = ev.payload.get("workers", [])
    self._console.print(
        f"  [bold]▶ Spawning {total} subagent{'s' if total != 1 else ''}[/bold]",
        markup=True,
        highlight=False,
    )
    for w in workers:
        label = _e(str(w.get("label", "")))
        task = _e(str(w.get("task", w.get("type", "")))[:80])
        self._console.print(
            f"  [dim]⎿[/dim] [cyan]{label}[/cyan]  [dim]{task}[/dim]",
            markup=True,
            highlight=False,
        )


@register_renderer("subagent_worker_done")
def _render_subagent_worker_done(self: ScrollBufferAppender, ev: ConversationEvent) -> None:
    from rich.markup import escape as _e  # noqa: PLC0415

    ok = bool(ev.payload.get("ok", True))
    label = _e(str(ev.payload.get("label", "")))
    done = ev.payload.get("done", 0)
    total = ev.payload.get("total", 0)
    ms = float(ev.payload.get("duration_ms", 0.0))
    dur = f"  [dim]{ms / 1_000:.1f}s[/dim]"
    if ok:
        self._console.print(
            f"  [green]✓[/green] [{done}/{total}] [cyan]{label}[/cyan]{dur}",
            markup=True,
            highlight=False,
        )
    else:
        error = _e(str(ev.payload.get("error", "failed"))[:80])
        self._console.print(
            f"  [red]✗[/red] [{done}/{total}] [cyan]{label}[/cyan]  [dim]{error}[/dim]",
            markup=True,
            highlight=False,
        )


@register_renderer("subagent_pool_done")
def _render_subagent_pool_done(self: ScrollBufferAppender, ev: ConversationEvent) -> None:
    succeeded = ev.payload.get("succeeded", 0)
    total = ev.payload.get("total", 0)
    failed = ev.payload.get("failed", 0)
    suffix = f"  [red]{failed} failed[/red]" if failed else ""
    self._console.print(
        f"  [bold]◈ {succeeded}/{total} subagent{'s' if total != 1 else ''} complete[/bold]{suffix}",
        markup=True,
        highlight=False,
    )
