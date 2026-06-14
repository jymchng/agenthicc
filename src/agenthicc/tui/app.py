"""TUI application — AgenthiccApp (PRD-55) and supporting utilities."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from .transcript import TranscriptModel

__all__ = [
    "AgenthiccApp",
    "MENU_COMMANDS",
    "RICH_AVAILABLE",
    "SlashCommandHandler",
    "StatusState",
    "_thinking_wave",
    "detect_slash_command",
]

# ── optional-dependency guards ────────────────────────────────────────────

try:
    from rich.panel import Panel
    from rich.text import Text
    from rich.table import Table
    from rich import box as rich_box

    RICH_AVAILABLE = True
except Exception:  # pragma: no cover
    RICH_AVAILABLE = False

# ── constants ─────────────────────────────────────────────────────────────

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


def _make_prompt(mode_manager: Any) -> str:
    """Build the prompt string. Mode is shown in the footer line only, never here."""
    return "\x1b[1;32m❯\x1b[0m "


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




# ── SlashCommandHandler ───────────────────────────────────────────────────


class SlashCommandHandler:
    """Renders slash-command output as Rich Panels/Tables inline."""

    def __init__(self, renderer: Any = None, skills: Any = None) -> None:
        # Optional back-reference to the renderer for live config mutations
        self._renderer = renderer
        self._skills = skills or {}

    def handle(self, text: str, model: TranscriptModel, console: Any) -> bool:
        """Dispatch to UnifiedCommandRegistry via CommandDispatcher.

        When the renderer is an :class:`AgenthiccApp` (Textual), the commands
        ``/status``, ``/history``, ``/models``, ``/help``, and ``/skills`` push
        the corresponding :class:`~agenthicc.tui.widgets.command_modals.ModalScreen`
        instead of printing a Rich table.  The old Rich table path is kept for
        :class:`AgenthiccApp` and headless use.
        """
        from agenthicc.commands import CommandContext, CommandDispatcher  # noqa: PLC0415
        stripped = text.strip()
        first = stripped.split()[0] if stripped.split() else stripped
        if not first.startswith("/"):
            return False

        # ── Textual modal path ────────────────────────────────────────────────
        # When the renderer is an AgenthiccApp we push ModalScreen widgets
        # instead of printing Rich tables so the output stays inside the TUI.
        if isinstance(self._renderer, AgenthiccApp) and hasattr(self._renderer, "push_screen"):
            modal = self._build_modal(first, model)
            if modal is not None:
                self._renderer.push_screen(modal)
                return True

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

    def _build_modal(self, command: str, model: TranscriptModel) -> Any | None:
        """Return the appropriate ModalScreen for *command*, or None.

        Only commands that map to a modal are handled here; all others fall
        through to the normal Rich-table dispatch path.
        """
        from agenthicc.tui.widgets.command_modals import (  # noqa: PLC0415
            AgentStatusModal,
            HelpModal,
            HistoryModal,
            ModelsModal,
            SkillsModal,
        )

        if command == "/status":
            return AgentStatusModal(model)
        if command == "/history":
            return HistoryModal(model)
        if command in ("/models", "/model"):
            return ModelsModal()
        if command == "/help":
            registry = (
                getattr(self._renderer, "_cmd_registry", None)
                or getattr(self._renderer, "_command_registry", None)
            ) if self._renderer else None
            return HelpModal(registry=registry)
        if command == "/skills":
            return SkillsModal(skills=self._skills)
        return None

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



from textual.app import App as _TextualApp, ComposeResult as _ComposeResult


# ── AgenthiccApp ──────────────────────────────────────────────────────────────


class AgenthiccApp(_TextualApp):  # type: ignore[misc]
    """Full-screen Textual application for agenthicc (PRD-55 Phase 6).

    Wires all Phase-2–5 widgets into a single layout:

    Layout (top → bottom):
        StatusBar      — animated thinking indicator (hidden when idle)
        TranscriptView — scrollable transcript viewport (expands to fill)
        SpinnerPanel   — live tool-call progress (mounted/unmounted dynamically)
        InputPanel     — unified input bar (trigger menu + mode footer)

    Public interface:
        async run(on_intent)
        on_intent_submitted()
        on_model_call_complete(input_tokens, output_tokens, cost_usd)
        on_agent_run_complete()
        _flush_new_lines()
        console  (Textual's Rich Console — do not replace)
        _status  (StatusState)
    """

    # Textual manages only the bottom chrome.  The transcript is printed directly
    # to the terminal scroll buffer via self.print() so the user can scroll up
    # in their terminal to read the full conversation history.
    DEFAULT_CSS = """
    Screen {
        layout: vertical;
        height: auto;
    }
    #app-header {
        height: 1;
    }
    #status-bar {
        height: 1;
    }
    #input-panel {
        height: auto;
        max-height: 10;
        min-height: 1;
        border-top: solid $primary;
        border-bottom: solid $primary;
    }
    #app-footer {
        height: 1;
    }
    TriggerMenu {
        height: auto;
        max-height: 10;
        display: none;
    }
    TriggerMenu.visible {
        display: block;
    }
    """

    def __init__(
        self,
        model: TranscriptModel,
        adapter: Any | None = None,
        console: Any | None = None,
        base_path: str = ".",
        history_file: str | None = None,
    ) -> None:
        # Set own attrs BEFORE calling super().__init__() so that Textual's
        # reactive property accesses during __init__ don't hit __getattr__ and
        # try to recurse into self._delegate before it's created.
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "adapter", adapter)
        object.__setattr__(self, "_base_path", base_path)
        object.__setattr__(self, "_history_file", history_file)
        object.__setattr__(self, "_status", StatusState())
        object.__setattr__(self, "_on_intent", None)
        object.__setattr__(self, "_mode_manager", None)
        object.__setattr__(self, "_trigger_registry", None)
        object.__setattr__(self, "_spinner_mounted", False)
        object.__setattr__(self, "agent_active", False)
        object.__setattr__(self, "pending_queue_count", 0)
        object.__setattr__(self, "_printed_count", 0)  # transcript line deferral

        # Textual is a hard dependency — call its App.__init__ unconditionally.
        super().__init__()

    # ── Textual compose ───────────────────────────────────────────────────────

    def compose(self) -> _ComposeResult:  # type: ignore[override]
        """Bottom chrome only — transcript lives in the terminal scroll buffer."""
        from agenthicc.tui.widgets.header import Header  # noqa: PLC0415
        from agenthicc.tui.widgets.status_bar import StatusBar  # noqa: PLC0415
        from agenthicc.tui.widgets.input_panel import InputPanel  # noqa: PLC0415
        from agenthicc.tui.widgets.footer import Footer  # noqa: PLC0415

        self._init_trigger_registry()
        self._init_mode_manager()

        yield Header(id="app-header")
        yield StatusBar(id="status-bar")
        yield InputPanel(
            registry=self._trigger_registry,
            history=[],
            mode_manager=self._mode_manager,
            id="input-panel",
        )
        yield Footer(id="app-footer")

    # ── Lazy initialisers ─────────────────────────────────────────────────────

    def _init_trigger_registry(self) -> None:
        """Build the TriggerRegistry (@ mention + / slash command)."""
        if self._trigger_registry is not None:
            return
        from agenthicc.tui.trigger import TriggerRegistry  # noqa: PLC0415
        from agenthicc.tui.triggers.at_mention import AtMentionTrigger  # noqa: PLC0415
        from agenthicc.tui.triggers.slash_command import SlashCommandTrigger  # noqa: PLC0415
        from agenthicc.commands import build_builtin_registry  # noqa: PLC0415
        _cmd_registry = build_builtin_registry()
        registry = TriggerRegistry()
        registry.register(AtMentionTrigger())
        registry.register(SlashCommandTrigger(_cmd_registry))
        self._trigger_registry = registry

    def _init_mode_manager(self) -> None:
        """Build the ModeManager from the default + plugin registries."""
        if self._mode_manager is not None:
            return
        try:
            from pathlib import Path as _Path  # noqa: PLC0415
            from agenthicc.modes import build_default_registry, ModeManager  # noqa: PLC0415
            from agenthicc.modes.plugin_loader import discover_mode_plugins  # noqa: PLC0415
            _mode_registry = build_default_registry()
            _mode_plugins = discover_mode_plugins(
                project_dir=_Path(".agenthicc"),
                user_dir=_Path.home() / ".agenthicc",
            )
            for _mp in _mode_plugins.all_modes:
                _mode_registry.register(_mp)
            self._mode_manager = ModeManager(_mode_registry, default_name="Auto")
        except Exception:  # noqa: BLE001
            pass

    # ── public interface ──────────────────────────────────────────────────────

    async def run(self, on_intent: Callable) -> None:  # type: ignore[override]
        """Start the Textual event loop.

        Stores *on_intent* so it can be called from ``on_input_submitted``, then
        delegates to ``textual.app.App.run_async()`` in inline mode so the app
        renders directly into the terminal scroll buffer instead of taking over
        the full screen with an alternate buffer.
        """
        self._on_intent = on_intent
        await self.run_async(inline=True)

    def on_intent_submitted(self) -> None:
        """Activate the status spinner when the user submits an intent."""
        self._status.active = True
        self._status.intent_started_at = time.monotonic()
        self._status.input_tokens = 0
        self._status.output_tokens = 0
        self.agent_active = True

    def on_model_call_complete(
        self,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float = 0.0,
    ) -> None:
        """Update token counts after each LLM turn."""
        self._status.input_tokens += input_tokens
        self._status.output_tokens += output_tokens
        self._status.session_cost_usd += cost_usd

    def on_agent_run_complete(self) -> None:
        """Deactivate spinner when the agent run finishes."""
        self._status.active = False
        self._status.completed_agents += 1
        self.agent_active = False

    def _flush_new_lines(self) -> None:
        """Print new transcript lines into the terminal scroll buffer.

        Transcript is NOT inside a Textual widget — it is printed above the
        inline chrome via self.print() so the terminal's native scrollback
        gives the user full conversation history.
        """
        lines = self.model.render()
        new = lines[self._printed_count:]
        _MD = "\x00md\x00"
        for line in new:
            try:
                if line.startswith(_MD):
                    from rich.markdown import Markdown  # noqa: PLC0415
                    self.print(Markdown(line[len(_MD):]))
                else:
                    self.print(line)
            except Exception:  # noqa: BLE001
                pass
        if new:
            self._printed_count = len(lines)

    # ── Textual message handlers ──────────────────────────────────────────────

    def on_input_submitted(self, event: Any) -> None:
        """Forward InputSubmitted to the on_intent callback."""
        from agenthicc.tui.messages import InputSubmitted, UserMessagePosted, AgentStateChanged  # noqa: PLC0415
        if not isinstance(event, InputSubmitted):
            return
        value = event.value.strip()
        if not value:
            return
        # Echo user message into transcript before running the agent
        self.post_message(UserMessagePosted(value))
        self.post_message(AgentStateChanged("thinking"))
        self.on_intent_submitted()
        if self._on_intent is not None:
            import asyncio as _asyncio  # noqa: PLC0415

            async def _run() -> None:
                try:
                    await self._on_intent(value)
                except Exception as exc:
                    from agenthicc.tui.messages import ConsolePrint, AgentStateChanged as _ASC, ErrorOccurred  # noqa: PLC0415
                    self.post_message(ErrorOccurred(str(exc)))
                    self.post_message(_ASC("error"))
                finally:
                    self.on_agent_run_complete()

            _asyncio.ensure_future(_run())

    def on_tool_call_started(self, event: Any) -> None:
        """Forward ToolCallStarted to SpinnerPanel (mount if needed)."""
        from agenthicc.tui.messages import ToolCallStarted  # noqa: PLC0415
        if not isinstance(event, ToolCallStarted):
            return
        self._ensure_spinner_mounted()
        try:
            from agenthicc.tui.widgets.spinner_panel import SpinnerPanel  # noqa: PLC0415
            panel = self.query_one(SpinnerPanel)
            panel.on_tool_call_started(event)
        except Exception:  # noqa: BLE001
            pass

    def on_tool_call_complete(self, event: Any) -> None:
        """Forward ToolCallComplete to SpinnerPanel."""
        from agenthicc.tui.messages import ToolCallComplete  # noqa: PLC0415
        if not isinstance(event, ToolCallComplete):
            return
        try:
            from agenthicc.tui.widgets.spinner_panel import SpinnerPanel  # noqa: PLC0415
            panel = self.query_one(SpinnerPanel)
            panel.on_tool_call_complete(event)
        except Exception:  # noqa: BLE001
            pass

    def on_tokens_updated(self, event: Any) -> None:
        """Forward TokensUpdated to StatusBar."""
        from agenthicc.tui.messages import TokensUpdated  # noqa: PLC0415
        if not isinstance(event, TokensUpdated):
            return
        try:
            from agenthicc.tui.widgets.status_bar import StatusBar  # noqa: PLC0415
            bar = self.query_one(StatusBar)
            bar.on_tokens_updated(event)
        except Exception:  # noqa: BLE001
            pass

    def on_agent_run_started(self, event: Any) -> None:
        """Activate StatusBar and mount SpinnerPanel when agent turn begins."""
        from agenthicc.tui.messages import AgentRunStarted  # noqa: PLC0415
        if not isinstance(event, AgentRunStarted):
            return
        try:
            from agenthicc.tui.widgets.status_bar import StatusBar  # noqa: PLC0415
            bar = self.query_one(StatusBar)
            bar.active = True
        except Exception:  # noqa: BLE001
            pass
        self._ensure_spinner_mounted()

    def on_agent_run_finished(self, event: Any) -> None:
        """Deactivate StatusBar and unmount SpinnerPanel when agent turn ends."""
        from agenthicc.tui.messages import AgentRunFinished  # noqa: PLC0415
        if not isinstance(event, AgentRunFinished):
            return
        try:
            from agenthicc.tui.widgets.status_bar import StatusBar  # noqa: PLC0415
            bar = self.query_one(StatusBar)
            bar.active = False
        except Exception:  # noqa: BLE001
            pass
        self._unmount_spinner()

    def print_to_transcript(self, markup: str) -> None:
        """Print directly into the terminal scroll buffer above the chrome."""
        try:
            self.print(markup)
        except Exception:  # noqa: BLE001
            pass

    def on_console_print(self, event: Any) -> None:
        """Print ConsolePrint markup into the terminal scroll buffer."""
        from agenthicc.tui.messages import ConsolePrint  # noqa: PLC0415
        if not isinstance(event, ConsolePrint):
            return
        try:
            self.print(event.markup)
        except Exception:  # noqa: BLE001
            pass

    def on_transcript_updated(self, _: Any) -> None:
        """Flush new transcript lines into the terminal scroll buffer."""
        try:
            self._flush_new_lines()
        except Exception:  # noqa: BLE001
            pass

    def on_mode_cycled(self, event: Any) -> None:
        """ModeCycled is handled reactively by Footer and ModeFooter via message bus."""
        pass  # Footer widget handles AgentStateChanged; ModeFooter listens itself

    def on_pending_queue_updated(self, event: Any) -> None:
        """Forward PendingQueueUpdated count to ModeFooter."""
        from agenthicc.tui.messages import PendingQueueUpdated  # noqa: PLC0415
        if not isinstance(event, PendingQueueUpdated):
            return
        self.pending_queue_count = event.count
        try:
            from agenthicc.tui.widgets.mode_footer import ModeFooter  # noqa: PLC0415
            footer = self.query_one(ModeFooter)
            if event.count > 0:
                footer.set_notification(f"{event.count} message(s) queued")
            else:
                footer.set_notification(None)
        except Exception:  # noqa: BLE001
            pass

    # ── SpinnerPanel lifecycle helpers ────────────────────────────────────────

    def _ensure_spinner_mounted(self) -> None:
        """Mount SpinnerPanel inside TranscriptView if not already mounted."""
        if self._spinner_mounted:
            return
        try:
            from agenthicc.tui.widgets.transcript_view import TranscriptView  # noqa: PLC0415
            from agenthicc.tui.widgets.spinner_panel import SpinnerPanel  # noqa: PLC0415
            tv = self.query_one(TranscriptView)
            tv.mount(SpinnerPanel())
            self._spinner_mounted = True
        except Exception:  # noqa: BLE001
            pass

    def _unmount_spinner(self) -> None:
        """Unmount SpinnerPanel from TranscriptView when agent turn ends."""
        if not self._spinner_mounted:
            return
        try:
            from agenthicc.tui.widgets.spinner_panel import SpinnerPanel  # noqa: PLC0415
            panel = self.query_one(SpinnerPanel)
            panel.remove()
            self._spinner_mounted = False
        except Exception:  # noqa: BLE001
            self._spinner_mounted = False

    # ── attribute storage — fully self-contained ──────────────────────────────

    def __getattr__(self, name: str) -> Any:
        # AgenthiccApp is self-contained: all attributes assigned by
        # tui_session.py (e.g. _processor, _loaded_config, _skills, _exec_cfg,
        # _active_agent, _mcp_registry, _mention_cache, _project_plugin_tools)
        # are stored directly on this object via __setattr__ below.
        raise AttributeError(
            f"{type(self).__name__!r} object has no attribute {name!r}"
        )

    def __setattr__(self, name: str, value: Any) -> None:
        object.__setattr__(self, name, value)
