"""Small runtime bridge for foreground ``/bg`` handoff (PRD-141).

The bridge is installed during CLI discovery, before the interactive session
is constructed.  It keeps the handoff command in the existing command/input
pipeline while the background service remains the single lifecycle owner.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from .settings import background_enabled, load_background_settings
from .supervisor import BackgroundSupervisor
from .store import BackgroundStore

if TYPE_CHECKING:
    from agenthicc.tui.cbreak_reader import Key
    from agenthicc.tui.input.capabilities import _ExitSentinel
    from agenthicc.tui.input.unified_session import UnifiedInputSession
    from agenthicc.tui.runtime.commands import SendMessageCommand

_INSTALLED = False


def _last_user_text(session: object) -> str:
    remembered = getattr(session, "_background_intent", "")
    if isinstance(remembered, str) and remembered.strip():
        return remembered.strip()

    # TUISession records accepted plain submissions even when the optional
    # compatibility bridge was installed after the session was constructed.
    # This keeps /bg usable for the first active request as well as resumed
    # sessions, without treating the /bg command itself as the intent.
    submitted = getattr(session, "_last_submitted_text", "")
    if isinstance(submitted, str) and submitted.strip() and not submitted.lstrip().startswith("/"):
        return submitted.strip()

    conversation = getattr(getattr(session, "_ctx"), "app_state").conversation
    turns = conversation.turns()
    for turn in reversed(turns):
        for event in reversed(turn.events):
            if event.kind == "user_message":
                value = event.payload.get("text")
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return ""


def _handoff(session: object) -> bool:
    ctx = getattr(session, "_ctx")
    intent = _last_user_text(session)
    if not intent:
        ctx.console.print("Cannot background this session before it has a user request.")
        return True
    mode = ctx.app_state.active_mode()
    workflow_name = str(
        getattr(session, "_workflow_override", "") or getattr(mode, "default_workflow", "") or ""
    )
    settings = load_background_settings(config=getattr(ctx.cfg, "background", None))
    if not background_enabled(settings):
        ctx.console.print("Background sessions are disabled by configuration.")
        return True
    root_value = settings.store_path
    root = Path(root_value).expanduser() if isinstance(root_value, str) and root_value else None
    store = BackgroundStore(root)
    supervisor = BackgroundSupervisor(
        store,
        max_workers=settings.max_workers,
        max_workers_per_project=settings.max_workers_per_project,
        cancel_grace_s=settings.cancel_grace_s,
        wall_timeout_s=settings.wall_timeout_s,
        max_activity_bytes=settings.max_activity_bytes,
        trash_retention_days=settings.trash_retention_days,
    )
    try:
        record = supervisor.handoff(
            session_id=ctx.session_id,
            intent=intent,
            workflow_name=workflow_name,
            cwd=str(Path.cwd()),
            config_path=None,
            set_overrides=tuple(ctx.cfg_overrides) if hasattr(ctx, "cfg_overrides") else (),
            dangerously_skip_permissions=bool(
                getattr(
                    getattr(ctx.app_state, "cli_flags", None), "dangerously_skip_permissions", False
                )
            ),
        )
    except Exception as exc:  # noqa: BLE001
        ctx.console.print(f"Unable to background session: {type(exc).__name__}: {exc}")
        return True
    ctx.app_state.conversation.notify_transient(
        f"Backgrounded session {record.session_id[:12]}… ({record.status.value})"
    )
    task = getattr(session, "_agent_task", None)
    if isinstance(task, asyncio.Task) and not task.done():
        task.cancel()
    setattr(getattr(session, "_input_session"), "_background_exit_requested", True)
    return True


def install_tui_handoff() -> None:
    """Install the two foreground aliases once per interpreter."""

    global _INSTALLED
    if _INSTALLED:
        return
    from agenthicc.runners.tui_session import TUISession  # noqa: PLC0415
    from agenthicc.tui.input.unified_session import UnifiedInputSession  # noqa: PLC0415
    from agenthicc.tui.input.capabilities import _EXIT  # noqa: PLC0415
    from agenthicc.commands.builtins import BUILTIN_COMMANDS  # noqa: PLC0415
    from agenthicc.commands.command import BusyPolicy, Command  # noqa: PLC0415

    if not any(command.name == "/background" for command in BUILTIN_COMMANDS):
        BUILTIN_COMMANDS.append(
            Command(
                name="/background",
                aliases=("/bg",),
                description="Detach the active session into the background manager",
                argument_hint="(alias: /bg)",
                busy_policy=BusyPolicy.IMMEDIATE_CONTROL,
            )
        )

    original_dispatch = TUISession.dispatch_slash

    def dispatch_slash(self: TUISession, text: str) -> bool:
        command = text.strip().split(maxsplit=1)[0] if text.strip() else ""
        if command in {"/bg", "/background"}:
            return _handoff(self)
        return original_dispatch(self, text)

    original_handle_send = TUISession.handle_send

    async def handle_send(self: TUISession, cmd: SendMessageCommand) -> None:
        text = str(getattr(cmd, "text", "")).strip()
        if text.startswith("/") and text.split(maxsplit=1)[0] in {"/bg", "/background"}:
            self.dispatch_slash(text)
            return
        if text:
            # TUISession emits user_message before opening the agent turn, so
            # the event is intentionally not owned by a ConversationTurn.
            # Remember the accepted input on the session for /background.
            setattr(self, "_background_intent", text)
        return await original_handle_send(self, cmd)

    original_dispatch_key = UnifiedInputSession._dispatch

    async def dispatch_key(self: UnifiedInputSession, key: Key, ch: str) -> _ExitSentinel | None:
        result = await original_dispatch_key(self, key, ch)
        if getattr(self, "_background_exit_requested", False):
            return _EXIT
        return result

    TUISession.dispatch_slash = dispatch_slash  # type: ignore[method-assign]
    TUISession.handle_send = handle_send  # type: ignore[method-assign]
    setattr(UnifiedInputSession, "_dispatch", dispatch_key)
    _INSTALLED = True
