"""TUI application — InlineRenderer (PRD-09) + legacy headless/ANSI renderer.

The preferred entry point is :func:`run_inline`, which renders agent output
directly into the normal terminal scroll buffer using :mod:`rich` and reads
user input via :mod:`agenthicc.tui.mention_input` (no prompt_toolkit).

:func:`render_frame_ansi` and :func:`run_headless` are unchanged for
backward-compatibility with pyte E2E tests and headless CI pipelines.
:func:`build_app` is deprecated and now raises :class:`RuntimeError`.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, TextIO

from .transcript import TranscriptModel, ToolCallState

__all__ = [
    "INPUT_PROMPT",
    "InlineRenderer",
    "MENU_COMMANDS",
    "RICH_AVAILABLE",
    "SlashCommandHandler",
    "StatusState",
    "build_app",
    "detect_slash_command",
    "render_frame_ansi",
    "run_headless",
    "run_inline",
]

# ── optional-dependency guards ────────────────────────────────────────────

try:
    from rich.console import Console, Group as RichGroup
    from rich.live import Live
    from rich.spinner import Spinner
    from rich.panel import Panel
    from rich.text import Text
    from rich.table import Table
    from rich import box as rich_box

    RICH_AVAILABLE = True
except Exception:  # pragma: no cover
    RICH_AVAILABLE = False

# ── constants ─────────────────────────────────────────────────────────────

INPUT_PROMPT = "> "

#: Slash commands that open a menu overlay.
MENU_COMMANDS = {
    "/status": "status",
    "/history": "history",
}

#: Help text for the /help slash command.
SLASH_HELP = {
    "/status":  "Show active agent turn table",
    "/history": "Print last 20 transcript lines",
    "/model":   "Show or switch LLM provider/model  (e.g. /model openai gpt-4o)",
    "/models":  "List available providers",
    "/expand":  "Expand tool output or @mention  (/expand abc12345 or /expand @path)",
    "/help":    "Show this help table",
    "/skills":  "List available skills",
}


def detect_slash_command(text: str) -> str | None:
    """Return the menu name for *text* if it is a menu slash command."""
    return MENU_COMMANDS.get(text.strip())


# ── Thinking... wave animation ────────────────────────────────────────────

_THINKING_TEXT = "Thinking..."
_THINKING_LEN = len(_THINKING_TEXT)


def _thinking_wave(frame: int) -> str:
    """Return 'Thinking...' with one bold character sweeping L→R then R→L."""
    cycle = 2 * (_THINKING_LEN - 1)
    pos = frame % cycle
    if pos >= _THINKING_LEN:
        pos = cycle - pos
    result = ""
    for i, ch in enumerate(_THINKING_TEXT):
        if i == pos:
            result += f"\x1b[1m{ch}\x1b[22m"   # bold on → bold off
        else:
            result += ch
    return result


# ── StatusState ───────────────────────────────────────────────────────────


@dataclass
class StatusState:
    """Mutable state for the Status Bar (PRD-20)."""

    active: bool = False
    spinner_frame: int = 0
    intent_started_at: float = 0.0  # time.monotonic() when intent submitted
    input_tokens: int = 0
    output_tokens: int = 0
    session_cost_usd: float = 0.0
    completed_agents: int = 0
    session_id: str = ""    # display label, e.g. "anthropic/claude-sonnet-4-6"
    resume_id: str = ""     # actual UUID used in --resume hint


# ── InlineRenderer ────────────────────────────────────────────────────────


class InlineRenderer:
    """Renders agenthicc session output directly into the terminal scroll buffer.

    Uses rich for styled output and a prompt_toolkit PromptSession for the
    input bar so readline editing (history, Ctrl-A/E, arrow keys) works.
    """

    def __init__(
        self,
        model: TranscriptModel,
        adapter: Any | None = None,
        console: Any | None = None,
        base_path: str = ".",
        history_file: str | None = None,
    ) -> None:
        self.model = model
        self.adapter = adapter
        if RICH_AVAILABLE:
            # force_terminal=True: Rich detects patch_stdout()'s wrapped stdout as
            # non-terminal and strips ANSI codes; this forces proper escape sequences.
            self.console = console or Console(
                highlight=False, markup=True, force_terminal=True
            )
        else:  # pragma: no cover
            self.console = console
        self._printed_count: int = 0
        self._live: Any | None = None
        self._base_path = base_path
        self._history_file = history_file
        # processor ref for interrupt/cancel; set externally or via adapter
        self._processor = getattr(adapter, "_processor", None) if adapter else None
        self._status = StatusState()

    # ── main loop ─────────────────────────────────────────────────────────

    async def run(self, on_input: Any) -> None:
        """Input loop using a custom CBREAK reader with Rich @mention dropdown.

        * Typing ``@`` immediately opens an inline file picker below the prompt.
        * History (↑/↓) is maintained in memory and persisted to the history file.
        * Ctrl+C: first press warns, second press exits.
        * No prompt_toolkit dependency in the hot path.
        """
        import asyncio as _asyncio  # noqa: PLC0415
        import inspect, os as _os, shutil as _sh  # noqa: PLC0415
        from pathlib import Path as _Path  # noqa: PLC0415
        from agenthicc.tui.mention_input import read_line_with_mention  # noqa: PLC0415

        _is_async = inspect.iscoroutinefunction(on_input)

        # ── history ───────────────────────────────────────────────────────
        _history: list[str] = []
        if self._history_file:
            try:
                _history = [
                    ln.strip()
                    for ln in _Path(self._history_file).read_text(encoding="utf-8").splitlines()
                    if ln.strip()
                ]
            except FileNotFoundError:
                pass
            except Exception:
                pass

        def _save_history() -> None:
            if self._history_file and _history:
                try:
                    _Path(self._history_file).parent.mkdir(parents=True, exist_ok=True)
                    _Path(self._history_file).write_text(
                        "\n".join(_history[-1000:]) + "\n", encoding="utf-8"
                    )
                except Exception:
                    pass

        _cwd = _Path(self._base_path).resolve()

        # ── trigger registry ──────────────────────────────────────────────────
        from agenthicc.tui.trigger import TriggerRegistry  # noqa: PLC0415
        from agenthicc.tui.triggers.at_mention import AtMentionTrigger  # noqa: PLC0415
        from agenthicc.tui.triggers.slash_command import SlashCommandTrigger  # noqa: PLC0415
        # ── unified command registry (PRD-44) ────────────────────────────
        from agenthicc.commands import build_builtin_registry, CommandDispatcher, Command as _Cmd  # noqa: PLC0415
        from agenthicc.commands.plugin_loader import discover_command_plugins  # noqa: PLC0415
        _cmd_registry = build_builtin_registry()
        self._cmd_registry = _cmd_registry
        # Skills auto-register (PRD-45)
        for slug, skill in getattr(self, "_skills", {}).items():
            def _make_skill_handler(s=skill, sl=slug):
                def _h(ctx):
                    import os as _os
                    from pathlib import Path as _P
                    from agenthicc.skills.runner import process_skill_body as _psb
                    args = ctx.args.split() if ctx.args.strip() else []
                    sid = getattr(getattr(self._status, "resume_id", None), "__str__", lambda: "")() or ""
                    body = _psb(s, args=args, cwd=_P(_os.getcwd()), session_id=sid)
                    self._pending_skill = body
                    ctx.console.print(f"  [dim]Invoking skill [bold]/{sl}[/bold][/dim]")
                    return True
                return _h
            _cmd_registry.register(_Cmd(
                name=f"/{slug}",
                description=getattr(skill, "description", "") or getattr(skill, "name", slug),
                group="Skills",
                argument_hint=getattr(skill, "argument_hint", ""),
                source_id=f"skill:{slug}",
                handler=_make_skill_handler(),
            ))
        # Command plugins auto-register (PRD-46)
        _cmd_plugins = discover_command_plugins(
            project_dir=_Path(".agenthicc"),
            user_dir=_Path.home() / ".agenthicc",
        )
        for cmd in _cmd_plugins.all_commands:
            _cmd_registry.register(cmd)
        if _cmd_plugins.all_commands:
            from rich.console import Console as _RC
            names = ", ".join(c.name for c in _cmd_plugins.all_commands)
            _RC().print(f"[dim]Loaded {len(_cmd_plugins.all_commands)} command plugin(s): {names}[/dim]")
        _dispatcher = CommandDispatcher(_cmd_registry)
        self._dispatcher = _dispatcher
        _trigger_registry = TriggerRegistry()
        _trigger_registry.register(AtMentionTrigger())
        _trigger_registry.register(SlashCommandTrigger(_cmd_registry))  # UnifiedCommandRegistry

        try:
            while True:
                self._flush_new_lines()
                self._print_status()

                _initial_menu = getattr(self, "_pending_menu", None)
                if _initial_menu is not None:
                    self._pending_menu = None

                try:
                    text = await _asyncio.to_thread(
                        read_line_with_mention, "❯ ", _cwd, _history,
                        _trigger_registry, _initial_menu,
                    )
                except KeyboardInterrupt:
                    # Safety net: ISIG is cleared inside _raw_mode so this
                    # should never fire during normal input, but if a SIGINT
                    # arrives from outside (e.g. a subprocess) we exit cleanly.
                    break

                if text is None:
                    # Ctrl+C / Ctrl+D handled inside read_line_with_mention.
                    break

                text = text.strip()
                if not text:
                    continue

                self.console.print(
                    f"[dim]{'─' * _sh.get_terminal_size((80, 24)).columns}[/dim]"
                )

                handled = SlashCommandHandler(
                    renderer=self, skills=getattr(self, "_skills", {})
                ).handle(text, self.model, self.console)
                pending = getattr(self, "_pending_skill", None)
                if pending:
                    self._pending_skill = None
                    self.on_intent_submitted()
                    if _is_async:
                        await on_input(pending)
                    else:
                        on_input(pending)
                    self._flush_new_lines()
                elif not handled:
                    self.on_intent_submitted()
                    if _is_async:
                        await on_input(text)
                    else:
                        on_input(text)
                    self._flush_new_lines()
        finally:
            _save_history()

    def _print_status(self) -> None:
        """Print status line + top border of the input area."""
        import shutil as _sh, time as _t  # noqa: PLC0415
        s = self._status
        cols = _sh.get_terminal_size((80, 24)).columns

        if s.active:
            wave = _thinking_wave(s.spinner_frame)
            elapsed = _t.monotonic() - s.intent_started_at if s.intent_started_at else 0.0
            self.console.print(
                f" {wave}  [dim]{elapsed:.1f}s  │[/dim]"
                f"  [cyan]↑ {s.input_tokens:,}[/cyan]"
                f"  [green]↓ {s.output_tokens:,}[/green]"
            )
        else:
            sid = s.session_id or "session"
            turns = s.completed_agents
            self.console.print(
                f" [dim]{sid}  │  {turns} turn{'s' if turns != 1 else ''}  │  ${s.session_cost_usd:.3f}[/dim]"
            )

        # Top border of input area — plain horizontal rule
        self.console.print(f"[dim]{'─' * cols}[/dim]")

    # Sentinel returned by _get_line on EOF (Ctrl+D) — distinct from None (Ctrl+C).
    _EOF: object = object()

    # Readline-safe prompt: \x01 / \x02 bracket ANSI codes so readline excludes
    # them from its display-width calculation.  Without these delimiters readline
    # thinks the prompt is wider than it is and lets the cursor drift left into
    # the ❯ glyph when the user presses ← at position 0.
    _PROMPT = "\x01\x1b[1;32m\x02❯\x01\x1b[0m\x02 "

    def _get_line(self) -> str | None | object:
        """Blocking input — runs in a thread via asyncio.to_thread.

        Returns:
            str    — the typed line (may be empty string)
            None   — Ctrl+C; signal handler owns the count / exit logic
            _EOF   — Ctrl+D / EOF; caller should break the loop
        """
        try:
            return input(self._PROMPT)
        except KeyboardInterrupt:
            return None
        except EOFError:
            return self._EOF

    # ── render loop ───────────────────────────────────────────────────────

    async def _render_loop(self) -> None:
        """Background task: print new lines and update spinner every 50 ms."""
        while True:
            await asyncio.sleep(0.05)
            if self.adapter is not None:
                self.adapter.sync()
            self._flush_new_lines()
            self._update_spinner()
            self.model.advance_spinner()
            self._status.spinner_frame += 1

    _MD_SENTINEL = "\x00md\x00"

    def _flush_new_lines(self) -> None:
        """Print lines from model.render() not yet printed.

        Lines prefixed with ``_MD_SENTINEL`` are agent prose rendered through
        Rich's ``Markdown`` class; all other lines use standard Rich markup.
        """
        lines = self.model.render()
        new = lines[self._printed_count:]
        for line in new:
            if line.startswith(self._MD_SENTINEL):
                from rich.markdown import Markdown  # noqa: PLC0415
                md_text = line[len(self._MD_SENTINEL):]
                self.console.print(Markdown(md_text), highlight=False)
            else:
                self.console.print(line, markup=True, highlight=False)
        if new:
            self._printed_count = len(lines)

    def _update_spinner(self) -> None:
        """Start / update / stop the spinner Live block."""
        if not RICH_AVAILABLE:  # pragma: no cover
            return
        panel = self._build_spinner_panel()
        if panel is None:
            if self._live is not None:
                self._live.stop()
                self._live = None
        else:
            if self._live is None:
                self._live = Live(
                    panel,
                    console=self.console,
                    refresh_per_second=12,
                    transient=True,
                )
                self._live.start()
            else:
                self._live.update(panel)

    def _build_spinner_panel(self) -> Any | None:
        """Return a Panel containing one Spinner per running tool, or None."""
        if not RICH_AVAILABLE:  # pragma: no cover
            return None
        running = [
            tc
            for turn in self.model.turns
            for tc in turn.tool_calls
            if tc.state == ToolCallState.RUNNING
        ]
        if not running:
            return None
        rows = [Spinner("dots", text=f"  [tool] {tc.name}") for tc in running]
        return Panel(RichGroup(*rows), border_style="dim", padding=(0, 1))

    def has_running_tools(self) -> bool:
        """Return True if any tool call is currently in the RUNNING state."""
        return self._build_spinner_panel() is not None

    # ── Status Bar lifecycle hooks ────────────────────────────────────────

    def on_intent_submitted(self) -> None:
        """Call when user submits an intent. Activates the status spinner."""
        self._status.active = True
        self._status.intent_started_at = time.monotonic()
        self._status.input_tokens = 0
        self._status.output_tokens = 0

    def on_model_call_complete(
        self, input_tokens: int, output_tokens: int, cost_usd: float = 0.0
    ) -> None:
        """Update token counts after each LLM turn completes."""
        self._status.input_tokens += input_tokens
        self._status.output_tokens += output_tokens
        self._status.session_cost_usd += cost_usd

    def on_agent_run_complete(self) -> None:
        """Deactivate spinner when agent run finishes (if no more running tools)."""
        if not self.has_running_tools():
            self._status.active = False
            self._status.completed_agents += 1

    # ── Status Bar rendering ──────────────────────────────────────────────

    def _render_status_panel(self) -> Any:
        """Build the Status Bar panel. Active: spinner+tokens. Idle: session summary."""
        if not RICH_AVAILABLE:  # pragma: no cover
            return None
        from .transcript import SPINNER_FRAMES  # noqa: PLC0415

        s = self._status
        if s.active:
            elapsed = (
                time.monotonic() - s.intent_started_at if s.intent_started_at else 0.0
            )
            frame = SPINNER_FRAMES[s.spinner_frame % len(SPINNER_FRAMES)]
            text = Text.assemble(
                (frame + " Thinking...  ", "bold"),
                (f"{elapsed:.1f}s", "dim"),
                ("  │  ", "dim"),
                ("↑ ", "dim"),
                (f"{s.input_tokens:,} tok", "cyan"),
                ("  ↓ ", "dim"),
                (f"{s.output_tokens:,} tok", "green"),
            )
        else:
            cost = f"${s.session_cost_usd:.3f}"
            sid = s.session_id[:12] if s.session_id else "session"
            completed = s.completed_agents
            text = Text.assemble(
                (f" {sid}", "dim"),
                ("  │  ", "dim"),
                (f"{completed} agent{'s' if completed != 1 else ''} completed", "dim"),
                ("  │  ", "dim"),
                (cost, "dim"),
            )
        return Panel(text, box=rich_box.DOUBLE, padding=(0, 1), style="dim")

    def _render_input_panel(self, input_text: str = "") -> Any:
        """Build the Input Bar panel with a double-line border."""
        if not RICH_AVAILABLE:  # pragma: no cover
            return None
        content = Text(INPUT_PROMPT + input_text + "▌", style="bold white")
        return Panel(content, box=rich_box.DOUBLE, padding=(0, 1))


# ── SlashCommandHandler ───────────────────────────────────────────────────


class SlashCommandHandler:
    """Renders slash-command output as Rich Panels/Tables inline."""

    def __init__(self, renderer: Any = None, skills: Any = None) -> None:
        # Optional back-reference to InlineRenderer for live config mutations
        self._renderer = renderer
        self._skills = skills or {}

    def handle(self, text: str, model: TranscriptModel, console: Any) -> bool:
        """Dispatch to UnifiedCommandRegistry via CommandDispatcher."""
        from agenthicc.commands import CommandContext, CommandDispatcher  # noqa: PLC0415
        stripped = text.strip()
        first = stripped.split()[0] if stripped.split() else stripped
        if not first.startswith("/"):
            return False

        ctx = CommandContext(
            text=stripped,
            args=" ".join(stripped.split()[1:]),
            model=model,
            console=console,
            renderer=self._renderer,
            config=getattr(self._renderer, "_loaded_config", None) if self._renderer else None,
            session_id=getattr(getattr(self._renderer, "_status", None), "session_id", ""),
        )

        # Prefer the renderer's real CommandDispatcher when available.
        renderer_dispatcher = (
            getattr(self._renderer, "_dispatcher", None) if self._renderer else None
        )
        if isinstance(renderer_dispatcher, CommandDispatcher):
            return renderer_dispatcher.dispatch(stripped, ctx)

        # If the renderer exposes a _menu_registry, try that first for menu
        # commands (e.g. /config → ConfigurationMenu).
        menu_registry = (
            getattr(self._renderer, "_menu_registry", None) if self._renderer else None
        )
        if menu_registry is not None:
            factory = menu_registry.get(first) if hasattr(menu_registry, "get") else None
            if factory is not None:
                widget = factory(ctx)
                if self._renderer is not None:
                    self._renderer._pending_menu = widget
                return True

        # Fallback: use the built-in command registry so that /status, /history,
        # /help etc. work even when no renderer (or no real dispatcher) is set.
        from agenthicc.commands import build_builtin_registry  # noqa: PLC0415
        fallback = CommandDispatcher(build_builtin_registry())
        return fallback.dispatch(stripped, ctx)

    def _status(self, model: TranscriptModel, console: Any) -> None:
        if not RICH_AVAILABLE:  # pragma: no cover
            return
        table = Table(title="Agent Status", box=rich_box.SIMPLE)
        table.add_column("Agent ID", style="cyan")
        table.add_column("Name")
        table.add_column("Cost")
        table.add_column("Tokens", justify="right")
        for turn in model.turns:
            table.add_row(
                turn.agent_id[:8],
                turn.agent_name,
                f"${turn.cost_usd:.4f}" if turn.cost_usd is not None else "$0.0000",
                str(turn.tokens) if turn.tokens is not None else "0",
            )
        if not model.turns:
            table.add_row("—", "(no active agents)", "", "")
        console.print(table)

    def _history(self, model: TranscriptModel, console: Any) -> None:
        if not RICH_AVAILABLE:  # pragma: no cover
            return
        lines = model.render()[-20:]
        console.print(
            Panel(
                "\n".join(lines) or "(empty)",
                title="/history — last 20 lines",
            )
        )

    def _model(self, cmd: str, console: Any) -> None:
        """Handle /model and /models commands."""
        if not RICH_AVAILABLE:  # pragma: no cover
            return
        from agenthicc.config import (  # noqa: PLC0415
            PROVIDER_API_KEY_ENVVAR,
            PROVIDER_DEFAULT_MODELS,
            SUPPORTED_PROVIDERS,
            load_config,
        )
        import os  # noqa: PLC0415

        parts = cmd.split()
        # /models — list all providers
        if parts[0] == "/models" or len(parts) == 1:
            cfg = load_config()
            current_provider = cfg.execution.provider
            current_model = cfg.execution.effective_model()

            table = Table(title="LLM Providers", box=rich_box.SIMPLE)
            table.add_column("Provider", style="cyan")
            table.add_column("Default Model")
            table.add_column("API Key Env")
            table.add_column("Status")

            for provider in SUPPORTED_PROVIDERS:
                env_var = PROVIDER_API_KEY_ENVVAR.get(provider, "—")
                key_set = "✓ set" if (
                    provider == "ollama" or os.environ.get(env_var)
                ) else "✗ not set"
                key_style = "green" if "✓" in key_set else "dim red"
                active = "◀ active" if provider == current_provider else ""
                table.add_row(
                    f"[bold]{provider}[/bold]" if active else provider,
                    PROVIDER_DEFAULT_MODELS.get(provider, "—"),
                    env_var,
                    Text(key_set, style=key_style),
                )
            console.print(table, markup=True)
            console.print(
                Text.assemble(
                    ("Active: ", "dim"), (current_provider, "cyan bold"),
                    (" / ", "dim"), (current_model, "bold"),
                )
            )
            console.print(
                Text(
                    "  Set provider: /model <provider> [model]\n"
                    "  Example:  /model anthropic claude-sonnet-4-6\n"
                    "  Example:  /model openai gpt-4o-mini\n"
                    "  Example:  /model ollama llama3.2",
                    style="dim",
                )
            )
            return

        # /model <provider> [model] — switch provider/model
        provider = parts[1].lower() if len(parts) > 1 else ""
        model_override = parts[2] if len(parts) > 2 else ""

        if provider not in SUPPORTED_PROVIDERS:
            console.print(
                Text(
                    f"Unknown provider: {provider!r}\n"
                    f"Supported: {', '.join(SUPPORTED_PROVIDERS)}",
                    style="red",
                )
            )
            return

        # Push the change back to the renderer's status state for display
        env_var = PROVIDER_API_KEY_ENVVAR.get(provider)
        if provider != "ollama" and env_var and not os.environ.get(env_var):
            console.print(
                Text(
                    f"Warning: {env_var} is not set — agent calls will fail.\n"
                    f"  export {env_var}=\"your-api-key\"",
                    style="yellow",
                )
            )

        effective_model = model_override or PROVIDER_DEFAULT_MODELS.get(provider, "")
        console.print(
            Text.assemble(
                ("Switched to ", "dim"),
                (provider, "cyan bold"),
                (" / ", "dim"),
                (effective_model, "bold"),
                (
                    "\n  Add to .agenthicc/agenthicc.toml to persist:\n"
                    f"  [execution]\n  provider = \"{provider}\"\n"
                    f"  model = \"{effective_model}\"",
                    "dim",
                ),
            )
        )

        # Mutate the renderer's live status if available
        if self._renderer is not None:
            self._renderer._status.session_id = f"{provider}/{effective_model}"

    def _expand(self, cmd: str, model: TranscriptModel, console: Any) -> None:
        """Toggle expanded output for a tool call by ID prefix, or an @mention chip."""
        parts = cmd.split()
        prefix = parts[1] if len(parts) > 1 else ""
        found = 0
        for turn in model.turns:
            for tc in turn.tool_calls:
                if not prefix or tc.tool_use_id.startswith(prefix):
                    tc.expanded = True
                    found += 1
        if prefix.startswith("@"):
            for turn in model.turns:
                for chip in getattr(turn, "mention_chips", []):
                    if chip.raw.startswith(prefix) or chip.raw == prefix:
                        chip.expanded = True
                        found += 1
        if found:
            console.print(f"[dim]Expanded {found} item{'s' if found > 1 else ''}.[/dim]")
        else:
            console.print(f"[dim]No item found matching {prefix!r}[/dim]")

    def _help(self, console: Any) -> None:
        if not RICH_AVAILABLE:  # pragma: no cover
            return
        registry = (
            getattr(self._renderer, "_cmd_registry", None)
            or getattr(self._renderer, "_command_registry", None)
        ) if self._renderer else None
        if registry is not None:
            for group in registry.groups():
                table = Table(title=group, box=rich_box.SIMPLE)
                table.add_column("Command", style="bold")
                table.add_column("Arguments", style="dim")
                table.add_column("Description")
                for cmd in registry.commands_for_group(group):
                    table.add_row(cmd.name, cmd.argument_hint or "", cmd.description)
                console.print(table)
            return
        # Fallback: use SLASH_HELP dict
        table = Table(title="Slash Commands", box=rich_box.SIMPLE)
        table.add_column("Command", style="bold")
        table.add_column("Description")
        for cmd, desc in SLASH_HELP.items():
            table.add_row(cmd, desc)
        console.print(table)

    def _list_skills(self, console: Any) -> None:
        if not RICH_AVAILABLE:
            return
        table = Table(title="Available Skills", box=rich_box.SIMPLE)
        table.add_column("Command", style="bold cyan")
        table.add_column("Name")
        table.add_column("Description")
        if not self._skills:
            table.add_row("—", "(no skills found)", "")
        else:
            for slug, skill in sorted(self._skills.items()):
                table.add_row(f"/{slug}", skill.name, skill.description[:80] or "—")
        console.print(table)

    def _invoke_skill(self, cmd: str, console: Any) -> None:
        import os as _os  # noqa: PLC0415
        from pathlib import Path  # noqa: PLC0415
        from agenthicc.skills.runner import process_skill_body  # noqa: PLC0415

        parts = cmd.split()
        slug = parts[0][1:]
        args = parts[1:]
        skill = self._skills.get(slug)
        if not skill:
            console.print(f"[red]Skill {slug!r} not found.[/red]")
            return
        session_id = ""
        if self._renderer is not None:
            session_id = getattr(self._renderer._status, "resume_id", "") or ""
        helper = skill.path / "helper.py"
        if helper.exists():
            console.print(f"  [dim]helper.py available at {helper}[/dim]")
        body = process_skill_body(skill, args=args, cwd=Path(_os.getcwd()), session_id=session_id)
        # Wrap with an explicit instruction frame so the LLM treats the skill
        # body as directives to execute, not as content to discuss.
        framed = (
            f"[Skill /{slug} — execute the following instructions:]\n\n"
            f"{body}"
        )
        if self._renderer is not None:
            self._renderer._pending_skill = framed
        console.print(f"  [bold cyan]⚡[/bold cyan] [dim]Invoking skill [bold]/{slug}[/bold][/dim]")


# ── run_inline ────────────────────────────────────────────────────────────


async def run_inline(
    model: TranscriptModel,
    adapter: Any | None = None,
    on_input: Callable[[str], None] | None = None,
) -> None:
    """Convenience wrapper: create InlineRenderer and run.

    Renders agent output directly into the terminal scroll buffer (no alternate
    screen), with a prompt_toolkit PromptSession for the input bar.
    """
    renderer = InlineRenderer(model, adapter)
    await renderer.run(on_input or (lambda _: None))


# ── deprecated build_app ──────────────────────────────────────────────────


def build_app(model: TranscriptModel, on_input: Callable[[str], None]) -> Any:
    """Deprecated. Use :func:`run_inline` instead.

    .. deprecated::
        ``build_app()`` returned a full-screen prompt_toolkit Application.
        The new entry point is the inline scroll-buffer renderer.
    """
    raise RuntimeError(
        "build_app() is deprecated. Use run_inline() instead.\n"
        "\n"
        "Migration:\n"
        "  Before: app = build_app(model, on_input); await app.run_async()\n"
        "  After:  await run_inline(model, adapter, on_input=on_input)\n"
    )


# ── headless mode ────────────────────────────────────────────────────────


async def run_headless(event_queue: asyncio.Queue, output_stream: TextIO) -> None:
    """Emit one JSON line per kernel event instead of rendering a TUI.

    Stops when a ``None`` sentinel is read from *event_queue*.
    """
    while True:
        event = await event_queue.get()
        if event is None:
            break
        record = {
            "ts": getattr(event, "timestamp", time.time()),
            "event_type": getattr(event, "event_type", type(event).__name__),
            "event_id": getattr(event, "event_id", None),
            "payload": getattr(event, "payload", {}),
            "source_agent_id": getattr(event, "source_agent_id", None),
        }
        output_stream.write(json.dumps(record, default=str) + "\n")
        output_stream.flush()


# ── offline ANSI frame renderer (used by the pyte e2e tests) ─────────────


def render_frame_ansi(
    model: TranscriptModel,
    cols: int,
    rows: int,
    input_text: str = "",
    menu_lines: list[str] | None = None,
    status_state: "StatusState | None" = None,
) -> str:
    """Compose a full ANSI frame the way the prompt_toolkit layout does.

    Row layout (1-indexed ANSI rows):
      rows 1 .. rows-3   transcript (auto-scrolled to the tail)
      row  rows-2        status bar (spinner when active, summary when idle)
      row  rows-1        session status line
      row  rows          input bar  (ALWAYS the last row)

    A menu overlay, when present, is painted over the transcript region
    anchored 2 rows above the terminal bottom — i.e. its last row sits just
    above the status bar and it never touches the input bar.
    """
    transcript_rows = max(rows - 3, 0)
    lines = model.render()
    visible = lines[-transcript_rows:] if transcript_rows else []

    def clip(text: str) -> str:
        return text[:cols]

    buf: list[str] = ["\x1b[2J\x1b[H"]  # clear screen, home cursor
    for i, line in enumerate(visible):
        buf.append(f"\x1b[{i + 1};1H{clip(line)}")

    # menu overlay floats above the status bar (bottom=3 anchor)
    if menu_lines:
        overlay_end = rows - 3  # last overlay row (1-indexed)
        overlay_start = max(overlay_end - len(menu_lines) + 1, 1)
        for i, line in enumerate(menu_lines[: overlay_end - overlay_start + 1]):
            buf.append(f"\x1b[{overlay_start + i};1H{clip(line)}")

    # Status bar line (new row rows-2)
    if status_state is not None and status_state.active:
        from .transcript import SPINNER_FRAMES  # noqa: PLC0415

        frame = SPINNER_FRAMES[status_state.spinner_frame % len(SPINNER_FRAMES)]
        elapsed = (
            time.monotonic() - status_state.intent_started_at
            if status_state.intent_started_at
            else 0.0
        )
        status_bar = (
            f" {frame} Thinking...  {elapsed:.1f}s"
            f"  ↑{status_state.input_tokens:,} ↓{status_state.output_tokens:,}"
        )
    else:
        status_bar = ""
    buf.append(f"\x1b[{rows - 2};1H{clip(status_bar)}")

    agents = len({t.agent_id for t in model.turns})
    status = (
        f" {agents} agents | ${model.total_cost_usd:.3f}"
        f" | {model.total_tokens:,} tok"
    )
    buf.append(f"\x1b[{rows - 1};1H{clip(status)}")
    buf.append(f"\x1b[{rows};1H{clip(INPUT_PROMPT + input_text)}")
    return "".join(buf)
