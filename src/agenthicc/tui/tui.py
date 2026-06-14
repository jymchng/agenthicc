"""AgenthiccTUI — root class that wires the event bus, components, and input loop.

File layout after refactor
==========================
tui/
  events.py     — Event dataclasses + EventBus
  reactive.py   — _Observable mixin + ReactiveProperty descriptor
  protocols.py  — typing.Protocol contracts for every component
  states.py     — StatusBarState, FooterState, InputBarState, SpinnerState
  live_panel.py — LivePanel (Rich Live block)
  transcript.py — TranscriptView (terminal scroll buffer printer)
  tui.py        — AgenthiccTUI (root) + _StatusShim (backward-compat)

Public re-exports
=================
Everything that was previously a single flat import from ``tui.tui`` is still
importable from this module via the star-import at the bottom, so existing
callsites (tui_session.py, tests) need no changes.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from agenthicc.tui.tui_events import (
    AgentStateChangeEvent,
    AssistantChunkEvent,
    AssistantCompleteEvent,
    AssistantStartEvent,
    ErrorEvent,
    Event,
    EventBus,
    FileModifiedEvent,
    InputChangedEvent,
    ModeChangedEvent,
    NotificationEvent,
    SessionSummaryEvent,
    ThinkingStepEvent,
    TokenUpdateEvent,
    ToolCompleteEvent,
    ToolStartEvent,
    UserMessageEvent,
)
from agenthicc.tui.live_panel import LivePanel
from agenthicc.tui.states import FooterState, StatusBarState
from agenthicc.tui.console_transcript import TranscriptView

# Re-export everything so ``from agenthicc.tui.tui import X`` still works.
from agenthicc.tui.tui_events import *  # noqa: F401, F403
from agenthicc.tui.reactive import ReactiveProperty, _Observable  # noqa: F401
from agenthicc.tui.states import (  # noqa: F401
    FooterState,
    InputBarState,
    SpinnerState,
    StatusBarState,
)

__all__ = ["AgenthiccTUI"]


class AgenthiccTUI:
    """Root TUI class — event bus + components + input loop.

    Exposes the same public interface as the old ``InlineRenderer`` so that
    ``tui_session.py`` and ``agent_turn.py`` require no changes:

        run(on_input)
        on_intent_submitted()
        on_model_call_complete(input_tokens, output_tokens, cost_usd)
        on_agent_run_complete()
        _flush_new_lines()
        console   — Rich Console instance
        _status   — backward-compat StatusState shim

    Satisfies :class:`~agenthicc.tui.protocols.TUIRenderer`.
    """

    _MD_SENTINEL = "\x00md\x00"

    def __init__(
        self,
        model: Any,
        adapter: Any | None = None,
        console: Any | None = None,
        base_path: str = ".",
        history_file: str | None = None,
    ) -> None:
        from rich.console import Console  # noqa: PLC0415
        self.console = console or Console(
            highlight=False, markup=True, force_terminal=True
        )
        self._model = model
        self._adapter = adapter
        self._base_path = base_path
        self._history_file = history_file
        self._mode_manager: Any = None

        self.bus = EventBus()
        self.live_panel = LivePanel(self.console)
        self.transcript = TranscriptView(self.console)
        self.transcript.set_model(model)

        # Convenience aliases to the component states
        self.status_state    = self.live_panel.status
        self.footer_state    = self.live_panel.footer
        self.input_bar_state = self.live_panel.input_bar
        self.spinner_state   = self.live_panel.spinner

        self._wire_bus()
        self._status = _StatusShim(self.status_state, self.footer_state)
        self._current_agent_task: Any = None

        # StreamingInput is created lazily in run() once pending_queue exists.
        self._streaming_input: Any = None

    # ── EventBus wiring ───────────────────────────────────────────────────────

    def _wire_bus(self) -> None:
        b = self.bus
        b.subscribe(UserMessageEvent,      self._on_user_message)
        b.subscribe(AssistantStartEvent,   self._on_assistant_start)
        b.subscribe(AssistantChunkEvent,   self._on_assistant_chunk)
        b.subscribe(AssistantCompleteEvent,self._on_assistant_complete)
        b.subscribe(ThinkingStepEvent,     self._on_thinking_step)
        b.subscribe(ToolStartEvent,        self._on_tool_start)
        b.subscribe(ToolCompleteEvent,     self._on_tool_complete)
        b.subscribe(FileModifiedEvent,     self._on_file_modified)
        b.subscribe(ErrorEvent,            self._on_error)
        b.subscribe(AgentStateChangeEvent, self._on_agent_state)
        b.subscribe(TokenUpdateEvent,      self._on_tokens)
        b.subscribe(SessionSummaryEvent,   self._on_session_summary)
        b.subscribe(InputChangedEvent,     self._on_input_changed)
        b.subscribe(ModeChangedEvent,      self._on_mode_changed)
        b.subscribe(NotificationEvent,     self._on_notification)

    # ── Event handlers ────────────────────────────────────────────────────────

    def _on_user_message(self, e: UserMessageEvent) -> None:
        self.transcript.print_user(e.text)

    def _on_assistant_start(self, _: AssistantStartEvent) -> None:
        pass  # TranscriptModel.render() already outputs the ● assistant header

    def _on_assistant_chunk(self, e: AssistantChunkEvent) -> None:
        self.spinner_state.set_streaming_text(e.chunk)

    def _on_assistant_complete(self, _: AssistantCompleteEvent) -> None:
        # Flush whatever was just appended to the transcript (text + any completed
        # tool calls) to the scroll buffer before clearing the spinner, so the user
        # always sees completed work above the live panel.
        self.transcript.flush_from_model()
        # Clear the spinner so the next batch of tool calls renders fresh.
        # The scroll buffer retains the full history; only the live view resets.
        self.spinner_state.set_streaming_text("")
        self.spinner_state.clear()

    def _on_thinking_step(self, e: ThinkingStepEvent) -> None:
        self.transcript.print_thinking_step(e.step, e.done)

    def _on_tool_start(self, e: ToolStartEvent) -> None:
        # Clear any streaming-text preview before adding the new tool row.
        # If the LLM streamed prose between two tool groups, that text would
        # otherwise appear as a dim separator line between them.
        self.spinner_state.set_streaming_text("")
        self.spinner_state.add_call(e.tool_use_id, e.name, e.args)
        self.status_state.tool  = e.name
        self.status_state.state = "running"
        self.footer_state.mode  = "running"

    def _on_tool_complete(self, e: ToolCompleteEvent) -> None:
        # Update the spinner only — do NOT print to stdout yet.
        # The live panel is active; printing to stdout at the same time creates
        # a duplicate "group 1 / border / group 2" split.  The transcript will
        # be flushed via _flush_new_lines() once live_panel.stop() is called.
        self.spinner_state.complete_call(e.tool_use_id, e.success, e.duration_ms, e.diff)
        self.status_state.state = "thinking"
        self.status_state.tool  = ""

    def _on_file_modified(self, e: FileModifiedEvent) -> None:
        self.transcript.print_file_modified(e.path)

    def _on_error(self, e: ErrorEvent) -> None:
        self.transcript.print_error(e.message, e.detail)
        self.status_state.state = "error"
        self.footer_state.mode  = "error"

    def _on_agent_state(self, e: AgentStateChangeEvent) -> None:
        self.status_state.state = e.state
        if e.tool is not None:
            self.status_state.tool = e.tool
        self.footer_state.mode = e.state

    def _on_tokens(self, e: TokenUpdateEvent) -> None:
        self.status_state.add_tokens(e.input_tokens, e.output_tokens, e.cost_usd)

    def _on_session_summary(self, e: SessionSummaryEvent) -> None:
        self.status_state.session_id = e.session_id

    def _on_input_changed(self, e: InputChangedEvent) -> None:
        self.input_bar_state.update(e.buf, e.cursor, e.paste_condensed, e.paste_label)

    def _on_mode_changed(self, _: ModeChangedEvent) -> None:
        from agenthicc.tui.mention_input import _get_mode_str as get_mode_str  # noqa: PLC0415  (re-exported from mention_input)
        if self._mode_manager:
            s = get_mode_str(self._mode_manager)
            self.input_bar_state.mode_str = s
            self.footer_state.mode_str = s

    def _on_notification(self, e: NotificationEvent) -> None:
        self.footer_state.notify_text(e.text)

    # ── Public interface (mirrors InlineRenderer) ─────────────────────────────

    def on_intent_submitted(self) -> None:
        self._status.active = True
        self._status.intent_started_at = time.monotonic()
        self._status.input_tokens = 0
        self._status.output_tokens = 0
        self.status_state.start_run()
        self.footer_state.mode = "thinking"
        self.spinner_state.clear()

    def on_model_call_complete(
        self, input_tokens: int, output_tokens: int, cost_usd: float = 0.0
    ) -> None:
        self._status.input_tokens += input_tokens
        self._status.output_tokens += output_tokens
        self._status.session_cost_usd += cost_usd
        self.bus.publish(TokenUpdateEvent(input_tokens, output_tokens, cost_usd))

    def on_agent_run_complete(self) -> None:
        self._status.active = False
        self._status.completed_agents += 1
        self.status_state.finish_run()
        self.footer_state.mode = "idle"
        self.footer_state.notify_text(None)

    def _flush_new_lines(self) -> None:
        self.transcript.flush_from_model()

    # ── Tick loop ─────────────────────────────────────────────────────────────

    async def _tick_loop(self) -> None:
        while True:
            await asyncio.sleep(0.05)
            self.status_state.tick()
            self.spinner_state.tick()

    # ── Main run loop ─────────────────────────────────────────────────────────

    async def run(self, on_input: Any) -> None:
        """Start the input loop.  LivePanel is active only during agent runs."""
        import asyncio as _a  # noqa: PLC0415
        import inspect  # noqa: PLC0415
        import signal as _sig  # noqa: PLC0415
        from pathlib import Path as _Path  # noqa: PLC0415
        from agenthicc.tui.mention_input import read_line_with_mention  # noqa: PLC0415
        from agenthicc.tui.trigger import TriggerRegistry  # noqa: PLC0415
        from agenthicc.tui.triggers.at_mention import AtMentionTrigger  # noqa: PLC0415
        from agenthicc.tui.triggers.slash_command import SlashCommandTrigger  # noqa: PLC0415
        from agenthicc.commands import build_builtin_registry, CommandDispatcher  # noqa: PLC0415
        from agenthicc.modes import build_default_registry, ModeManager  # noqa: PLC0415

        _is_async = inspect.iscoroutinefunction(on_input)
        _history: list[str] = []
        _cmd_registry = build_builtin_registry()
        _registry = TriggerRegistry()
        _registry.register(AtMentionTrigger())
        _registry.register(SlashCommandTrigger(_cmd_registry))
        _mode_manager = ModeManager(build_default_registry())
        self._mode_manager = _mode_manager
        _cwd = _Path(self._base_path).resolve()
        _dispatcher = CommandDispatcher(_cmd_registry)

        # StreamingInput — queues messages typed during agent turns.
        # Pass live_panel + trigger_registry so the full @/slash picker works
        # during streaming (Live block is briefly paused for the picker session).
        from agenthicc.tui.input.streaming import StreamingSession as StreamingInput  # noqa: PLC0415
        _pending_queue: list[str] = []
        _streaming_input = StreamingInput(
            self.input_bar_state,
            _pending_queue,
            self.console,
            live_panel=self.live_panel,
            trigger_registry=_registry,
            cwd=_cwd,
        )
        self._streaming_input = _streaming_input
        _loop = _a.get_event_loop()
        _current_task: _a.Task | None = None

        def _sigint_cancel() -> None:
            if _current_task and not _current_task.done():
                _current_task.cancel()

        # SIGINT handling strategy:
        #   Idle mode   — asyncio handler NOT registered so Python's default
        #                 SIGINT→KeyboardInterrupt path is active.  The idle
        #                 input loop also handles \x03 (Ctrl+C with ISIG cleared)
        #                 via the double-press _ctrl_c_sequence, giving two exit
        #                 paths: single SIGINT/KeyboardInterrupt OR double \x03.
        #   Agent mode  — asyncio handler registered so SIGINT cancels the agent
        #                 task instead of crashing the event loop.

        tick_task = _a.create_task(self._tick_loop())

        async def _run_agent(coro: Any) -> None:
            nonlocal _current_task
            # Register SIGINT handler only for the duration of the agent run.
            try:
                _loop.add_signal_handler(_sig.SIGINT, _sigint_cancel)
            except (NotImplementedError, RuntimeError):
                pass
            _current_task = _a.ensure_future(coro)
            try:
                await _current_task
            except (_a.CancelledError, KeyboardInterrupt):
                self._status.active = False
            except Exception as exc:
                self.bus.publish(ErrorEvent(str(exc)))
                self.on_agent_run_complete()
            finally:
                _current_task = None
                try:
                    _loop.remove_signal_handler(_sig.SIGINT)
                except Exception:  # noqa: BLE001
                    pass

        try:
            while True:
                self._print_idle_status()
                try:
                    text = await _a.to_thread(
                        read_line_with_mention,
                        "\x1b[1;32m❯\x1b[0m ",
                        _cwd,
                        _history,
                        _registry,
                        None,
                        self._status.resume_id,
                        _mode_manager,
                    )
                except KeyboardInterrupt:
                    # Single Ctrl+C during idle (SIGINT with default handler).
                    break

                if text is None:
                    break
                text = text.strip()
                if not text:
                    continue

                if text.startswith("/"):
                    from agenthicc.commands import CommandContext  # noqa: PLC0415
                    ctx = CommandContext(
                        text=text,
                        args=" ".join(text.split()[1:]),
                        model=self._model,
                        console=self.console,
                        renderer=self,
                        config=getattr(self, "_loaded_config", None),
                        session_id=self._status.resume_id,
                    )
                    if _dispatcher.dispatch(text, ctx):
                        continue

                self.on_intent_submitted()
                self.live_panel.start()
                _streaming_input.start()
                if _is_async:
                    await _run_agent(on_input(text))
                else:
                    try:
                        on_input(text)
                    except (KeyboardInterrupt, _a.CancelledError):
                        self._status.active = False
                _streaming_input.stop()
                self.live_panel.stop()
                self._flush_new_lines()
                # Drain any messages queued while the agent was running.
                while _pending_queue:
                    next_text = _pending_queue.pop(0)
                    self.console.print(
                        f"[bold green]❯[/bold green] {next_text}",
                        markup=True, highlight=False,
                    )
                    self.on_intent_submitted()
                    self.live_panel.start()
                    _streaming_input.start()
                    if _is_async:
                        await _run_agent(on_input(next_text))
                    _streaming_input.stop()
                    self.live_panel.stop()
                    self._flush_new_lines()
        finally:
            _streaming_input.stop()
            tick_task.cancel()

    def _print_idle_status(self) -> None:
        import shutil  # noqa: PLC0415
        cols = shutil.get_terminal_size((80, 24)).columns
        sid   = self._status.session_id or "session"
        turns = self._status.completed_agents
        cost  = f"${self._status.session_cost_usd:.3f}"
        self.console.print(
            f" [dim]{sid}  |  {turns} turn{'s' if turns != 1 else ''}  |  {cost}[/dim]"
            f"  [cyan]↑ {self._status.input_tokens:,}[/cyan]"
            f"  [green]↓ {self._status.output_tokens:,}[/green]",
            markup=True, highlight=False,
        )
        self.console.print(f"[dim]{'─' * cols}[/dim]", markup=True, highlight=False)


# ── Backward-compat shim ──────────────────────────────────────────────────────

class _StatusShim:
    """Flat-attribute facade for the old ``StatusState`` API.

    ``tui_session.py`` writes e.g. ``renderer._status.session_id = "..."``
    These writes are forwarded to the proper component states.
    """

    def __init__(self, status: StatusBarState, footer: FooterState) -> None:
        object.__setattr__(self, "_status", status)
        object.__setattr__(self, "_footer", footer)
        object.__setattr__(self, "active", False)
        object.__setattr__(self, "intent_started_at", 0.0)
        object.__setattr__(self, "input_tokens", 0)
        object.__setattr__(self, "output_tokens", 0)
        object.__setattr__(self, "session_cost_usd", 0.0)
        object.__setattr__(self, "completed_agents", 0)
        object.__setattr__(self, "spinner_frame", 0)
        object.__setattr__(self, "session_id", "")
        object.__setattr__(self, "resume_id", "")

    def __setattr__(self, name: str, value: Any) -> None:
        object.__setattr__(self, name, value)
        s: StatusBarState = object.__getattribute__(self, "_status")
        if name == "session_id":
            s.session_id = value
