"""EmbeddedComposer — the main composer inside another surface (AGI-05).

The composer (``UnifiedInputSession`` with the PRD-74 capability pipeline) is
bound to the TUI's global input state and opens its ``/`` and ``@`` pickers
in the workspace's single overlay slot. An answer to an ``ask_user`` question
is typed *inside* an overlay, or inside the agents hub, so it cannot use
either.

An ``EmbeddedComposer`` owns a private ``AppState`` (and so a private
``InputState``), its own ``OverlayHost`` for its pickers, and a
``UnifiedInputSession`` with the same capabilities as the main composer:
paste, newline, history, cursor and word movement, and the trigger pickers.
Its host renders it with :meth:`render` (the picker, when open, then the
prompt) and feeds it keys with :meth:`feed`. Keys are dispatched strictly in
order, one at a time, so a fast typist's keys are never dropped or reordered.

Submitting calls ``on_submit(text)``; the composer clears only when it
returns ``True``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from agenthicc.tui.cbreak_reader import Key

if TYPE_CHECKING:
    from rich.console import RenderableType

    from agenthicc.background.model import LaunchSpec
    from agenthicc.commands.registry import UnifiedCommandRegistry
    from agenthicc.config import AgenthiccConfig
    from agenthicc.tui.conversation_store import AppState
    from agenthicc.tui.input.unified_session import UnifiedInputSession
    from agenthicc.tui.keymap import Keymap
    from agenthicc.tui.workspace.overlay import OverlayHost
    from agenthicc.workflows.registry import WorkflowRegistry

__all__ = ["ANSWER_EXCLUDED", "ComposerContext", "EmbeddedComposer", "load_composer_context"]

SubmitHandler = Callable[[str], Awaitable[bool]]


@dataclass(frozen=True)
class ComposerContext:
    """What an embedded composer's pickers and mentions are scoped to."""

    cwd: Path
    cfg: AgenthiccConfig | None = None
    commands: UnifiedCommandRegistry | None = None
    workflows: WorkflowRegistry | None = None
    keymap: Keymap | None = None
    history: list[str] = field(default_factory=list)
    session_id: str = ""
    mode_name: str = "Safe"


class EmbeddedComposer:
    """A full composer with private state, pickers and an in-order key queue."""

    def __init__(
        self,
        context: ComposerContext,
        *,
        on_submit: SubmitHandler,
        redraw: Callable[[], None] = lambda: None,
        on_exit: Callable[[], None] = lambda: None,
        exclude: Iterable[type[object]] = (),
    ) -> None:
        from agenthicc.tui.conversation_store import AppState
        from agenthicc.tui.input.capabilities import IDLE_CAPABILITIES, ModeCycleCapability
        from agenthicc.tui.input.unified_session import UnifiedInputSession
        from agenthicc.tui.runtime.commands import CommandBus, SendMessageCommand
        from agenthicc.tui.runtime.mode_manager import ModeManager
        from agenthicc.tui.trigger import TriggerManager
        from agenthicc.tui.triggers.at_mention import AtMentionTrigger
        from agenthicc.tui.triggers.slash_command import SkillTrigger, SlashCommandTrigger
        from agenthicc.tui.workspace.overlay import OverlayHost

        self.context = context
        self._on_submit = on_submit
        self._redraw = redraw
        self._on_exit = on_exit
        triggers = TriggerManager()
        triggers.register(AtMentionTrigger())
        if context.commands is not None:
            triggers.register(SlashCommandTrigger(context.commands, context.workflows))
            triggers.register(SkillTrigger(context.commands))
        state = AppState()
        state.conversation.session_id.set(context.session_id)
        if context.keymap is not None:
            state.keymap.set(context.keymap)
        overlay = OverlayHost(state)
        overlay.set_redraw_callback(redraw)
        bus = CommandBus()
        self.accepted = False

        async def submit(command: SendMessageCommand) -> _Receipt:
            accepted = await self._on_submit(command.text)
            self.accepted = self.accepted or accepted
            return _Receipt(accepted)

        bus.register(SendMessageCommand, submit)
        editor = UnifiedInputSession(
            app_state=state,
            command_bus=bus,
            trigger_registry=triggers,
            mode_manager=ModeManager(app_state=state, default_name=context.mode_name),
            overlay_host=overlay,
            cwd=context.cwd,
            cfg=context.cfg,
            history=context.history,
            busy=True,
            clear_after_acceptance=True,
            preserve_submission_text=True,
        )
        # The asking session owns its mode; an answer never changes it.
        excluded: tuple[type[object], ...] = (ModeCycleCapability, *exclude)
        editor._capabilities = [
            capability for capability in IDLE_CAPABILITIES if not isinstance(capability, excluded)
        ]
        self.state: AppState = state
        self.editor: UnifiedInputSession = editor
        self.overlay: OverlayHost = overlay
        self._keys: asyncio.Queue[tuple[Key, str]] | None = None
        self._drain: asyncio.Task[None] | None = None
        self._closed = False

    # ── input ─────────────────────────────────────────────────────────────

    @property
    def text(self) -> str:
        return self.editor._buf.text

    def set_text(self, text: str) -> None:
        self.editor.set_text(text)

    @property
    def picker_open(self) -> bool:
        return self.overlay.active

    @property
    def dispatch_task(self) -> asyncio.Task[None] | None:
        """The task dispatching queued keys, while there are any."""
        return self._drain

    def feed(self, key: Key, ch: str) -> None:
        """Queue one key; keys are dispatched in order, one at a time.

        Without a running event loop (a synchronous caller such as a unit
        test), the key is dispatched to completion before returning.
        """
        if self._closed:
            return
        if self._keys is None:
            self._keys = asyncio.Queue()
        self._keys.put_nowait((key, ch))
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self._drain_keys())
            return
        if self._drain is None or self._drain.done():
            self._drain = loop.create_task(
                self._drain_keys(), name="agenthicc-embedded-composer-keys"
            )

    async def _drain_keys(self) -> None:
        from agenthicc.tui.input.capabilities import _EXIT

        keys = self._keys
        assert keys is not None
        while not keys.empty() and not self._closed:
            key, ch = keys.get_nowait()
            result = await self.editor._dispatch(key, ch)
            self._redraw()
            if result is _EXIT:
                self._on_exit()
                return

    async def settle(self) -> None:
        """Wait until every queued key has been dispatched (for tests and closes)."""
        while self._drain is not None and not self._drain.done():
            await asyncio.wait({self._drain})

    def close(self) -> None:
        """Stop taking keys and close an open picker.

        Safe to call from a submit handler, which runs inside the key task:
        the queue then simply stops, instead of the task cancelling itself.
        """
        self._closed = True
        if self.overlay.active:
            self.overlay.hide()
        drain = self._drain
        if drain is not None and not drain.done():
            try:
                current = asyncio.current_task()
            except RuntimeError:
                current = None
            if drain is not current:
                drain.cancel()

    # ── output ────────────────────────────────────────────────────────────

    def render(self) -> list[RenderableType]:
        """The open picker, if any, then the prompt."""
        from agenthicc.tui.workspace.components import ComposerComponent

        lines: list[RenderableType] = []
        if self.overlay.active:
            rendered = self.overlay.render()
            if rendered is not None:
                lines.append(rendered)
        lines.append(ComposerComponent(self.state).render())
        return lines


def _answer_excluded() -> tuple[type[object], ...]:
    from agenthicc.tui.input import capabilities as caps

    return (
        caps.CtrlCCapability,
        caps.CtrlDCapability,
        caps.AgentsHubCapability,
        caps.SubagentsCapability,
        caps.BackgroundSubagentsCapability,
        caps.AgentCycleCapability,
        caps.InterruptCapability,
    )


#: Capabilities an answer composer leaves out: an answer never exits the
#: application, opens the hub or another agent, or interrupts the turn that
#: is waiting for it. Call it for the classes.
ANSWER_EXCLUDED = _answer_excluded


def load_composer_context(
    cwd: Path,
    *,
    launch: LaunchSpec | None = None,
    keymap: Keymap | None = None,
    history: list[str] | None = None,
    session_id: str = "",
    mode_name: str = "Safe",
) -> ComposerContext:
    """The commands, skills, workflows and configuration for a composer at *cwd*.

    Blocking (it reads plugins and configuration): run it off the event
    loop. *launch* is the session's recorded launch configuration (AGI-04).
    """
    from agenthicc.commands.builtins import build_builtin_registry
    from agenthicc.commands.plugin_loader import discover_command_plugins
    from agenthicc.config import load_config
    from agenthicc.runners.tui_session import _build_skill_command
    from agenthicc.skills.loader import discover_skills
    from agenthicc.workflows.registry import build_workflow_registry

    root = cwd.expanduser().resolve()
    project_dir = root / ".agenthicc"
    user_dir = Path.home() / ".agenthicc"
    project_config = project_dir / "agenthicc.toml"
    cfg = load_config(
        project_path=project_config if project_config.is_file() else None,
        config_path=launch.config_path if launch is not None else None,
        cli_overrides=list(launch.set_overrides) if launch is not None else [],
        cli_secret_overrides=list(launch.set_secret_overrides) if launch is not None else [],
    )
    commands = build_builtin_registry()
    for command in discover_command_plugins(
        project_dir=project_dir, user_dir=user_dir
    ).all_commands:
        commands.register(command)
    for slug, skill in discover_skills(project_dir=project_dir, user_dir=user_dir).items():
        command = _build_skill_command(slug, skill)
        if commands.get(command.name) is None:
            commands.register(command)
    workflows = build_workflow_registry(
        project_dir=project_dir, user_dir=user_dir, load_external=True
    )
    return ComposerContext(
        cwd=root,
        cfg=cfg,
        commands=commands,
        workflows=workflows,
        keymap=keymap,
        history=history if history is not None else [],
        session_id=session_id,
        mode_name=mode_name,
    )


@dataclass(frozen=True)
class _Receipt:
    """What ``SubmitCapability`` reads to decide whether to clear the prompt."""

    ok: bool
