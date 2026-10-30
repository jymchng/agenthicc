"""``agenthicc agents``: the agents hub and its commands (PRD-141, AGX-05)."""

from __future__ import annotations

import json as json_module
from collections.abc import Coroutine
from pathlib import Path
from typing import TYPE_CHECKING

from agenthicc.background import (
    BackgroundSession,
    BackgroundStore,
    BackgroundSupervisor,
    BackgroundSettings,
    background_enabled,
    load_background_settings,
)
from agenthicc.cli.context import CLIContext
from agenthicc.cli.registry import optional_positionals

if TYPE_CHECKING:
    from agenthicc.tui.workspace.agents_hub.model import LibraryEntry


def _config(ctx: CLIContext) -> object:
    if ctx.config is not None:
        return ctx.config
    from agenthicc.config import load_config

    return load_config(
        cli_overrides=list(ctx.set_overrides),
        cli_secret_overrides=list(ctx.set_secret_overrides),
        config_path=ctx.config_path,
    )


def _store_and_supervisor(
    ctx: CLIContext,
    *,
    settings: BackgroundSettings | None = None,
) -> tuple[BackgroundStore, BackgroundSupervisor]:
    settings = settings or _background_settings(ctx)
    if not background_enabled(settings):
        raise RuntimeError("Background agents are disabled by configuration")
    store = BackgroundStore(settings.store_root)
    supervisor = BackgroundSupervisor.from_settings(store, settings)
    return store, supervisor


def _background_settings(ctx: CLIContext) -> BackgroundSettings:
    config = _config(ctx)
    return load_background_settings(
        config_path=ctx.config_path,
        overrides=ctx.set_overrides,
        config=vars(config).get("background"),
    )


def _public_session(
    session: BackgroundSession,
    *,
    queue_position: int | None = None,
) -> dict[str, object]:
    from agenthicc.tui.runtime.session_export import _Redactor

    raw = session.to_dict()
    if isinstance(raw, dict):
        result = dict(raw)
        result.pop("intent", None)
        result.pop("lease_token", None)
        result.pop("dispatch_token", None)
        result.pop("dispatch_claimed_at", None)
        result.pop("idempotency_key", None)
        result.pop("owner_id", None)
        result["queue_position"] = queue_position
        result["queue_position_advisory"] = (
            "position may change as capacity and priorities change"
            if queue_position is not None
            else None
        )
        redactor = _Redactor()
        result.pop("input_value", None)
        for key in (
            "title",
            "latest_activity",
            "error",
            "approval_request",
            "input_request",
            "mode_application_error",
        ):
            if key in result:
                result[key] = redactor.value(result[key], key)
        history = result.get("attempt_history")
        if isinstance(history, list):
            sanitized_history: list[dict[str, object]] = []
            for entry in history:
                if not isinstance(entry, dict):
                    continue
                sanitized = dict(entry)
                for key in (
                    "error",
                    "latest_activity",
                    "failure_category",
                    "exit_reason",
                    "mode_application_error",
                ):
                    value = sanitized.get(key)
                    if isinstance(value, str):
                        sanitized[key] = redactor.value(value, key)
                sanitized_history.append(sanitized)
            result["attempt_history"] = sanitized_history
        return result
    return {}


def _definition_entries() -> list[LibraryEntry]:
    from agenthicc.tui.workspace.agents_hub.sources import load_library

    return load_library(Path.cwd() / ".agenthicc", Path.home() / ".agenthicc")


def _definition_payload(entry: LibraryEntry) -> dict[str, object]:
    spec = entry.spec
    return {
        "name": entry.name,
        "source": entry.source,
        "state": entry.state,
        "description": entry.description,
        "mode": entry.mode,
        "model": entry.model,
        "tools": list(spec.tools) if spec is not None and spec.tools is not None else None,
        "capabilities": (
            list(spec.capabilities) if spec is not None and spec.capabilities is not None else None
        ),
        "max_steps": spec.max_steps if spec is not None else None,
        "ceiling": spec.ceiling if spec is not None else "",
        "path": entry.path,
        "default": entry.default,
        "note": entry.note,
    }


def _queue_positions(supervisor: BackgroundSupervisor) -> dict[str, int]:
    try:
        value = supervisor.queue_positions()
    except AttributeError:
        # Compatibility for a supervisor injected by an older extension.
        return {}
    except (OSError, RuntimeError, ValueError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {
        key: position
        for key, position in value.items()
        if isinstance(key, str) and isinstance(position, int) and not isinstance(position, bool)
    }


async def _open_hub(
    ctx: CLIContext,
    *,
    tab: str = "",
    session_id: str = "",
    run_id: str = "",
) -> None:
    """Open the agents hub full screen, then follow an attach or a saved session."""
    from rich.console import Console

    from agenthicc.tui.workspace.agents_hub import DispatchDefaults, Tab, run_agents_hub
    from agenthicc.tui.workspace.agents_hub.sources import HubSources

    settings = _background_settings(ctx)
    store, supervisor = _store_and_supervisor(ctx, settings=settings)
    config = _config(ctx)
    ui = vars(config).get("ui")
    from agenthicc.config import UISettings

    result = await run_agents_hub(
        Console(highlight=False),
        store=store,
        supervisor=supervisor,
        settings=settings.manager,
        sources=HubSources.default(Path.cwd()),
        tab=Tab(tab) if tab else Tab.RUNNING,
        open_session=session_id or None,
        run_id=run_id,
        dispatch=DispatchDefaults(
            config_path=ctx.config_path,
            set_overrides=tuple(ctx.set_overrides),
            set_secret_overrides=tuple(ctx.set_secret_overrides),
            dangerously_skip_permissions=ctx.flags.dangerously_skip_permissions,
            mode_name=ctx.mode_name,
        ),
        ui=ui if isinstance(ui, UISettings) else None,
    )
    if result.session_id is None:
        return
    if result.action == "attach":
        await attach_background_session(
            ctx,
            result.session_id,
            store=store,
            supervisor=supervisor,
            fail_on_error=False,
        )
    elif result.action == "open_saved":
        from agenthicc.cli.commands.sessions import open_saved_session

        await open_saved_session(ctx, result.session_id)


async def attach_background_session(
    ctx: CLIContext,
    session_id: str,
    *,
    json_output: bool = False,
    fail_on_error: bool = True,
    store: BackgroundStore | None = None,
    supervisor: BackgroundSupervisor | None = None,
) -> bool:
    """Attach one exact background session through the canonical TUI path.

    Both the agents hub's attach action and the public ``attach`` command call
    this function.  Keeping the handoff here prevents a second session lookup
    or an accidental goal-run/last-session fallback.
    """

    from rich.console import Console

    if store is None or supervisor is None:
        store, supervisor = _store_and_supervisor(ctx)
    try:
        record = store.get(session_id, include_deleted=True)
        if record.status.value == "deleted":
            raise ValueError("deleted background sessions cannot be attached")
        foreground = supervisor.attach_foreground(session_id)
    except (KeyError, OSError, RuntimeError, ValueError) as exc:
        message = f"Unable to attach background session {session_id}: {exc}"
        if json_output:
            print(
                json_module.dumps({"session_id": session_id, "status": "error", "error": str(exc)})
            )
        else:
            Console(highlight=False).print(message)
        if fail_on_error:
            raise SystemExit(1) from exc
        return False

    if json_output:
        print(
            json_module.dumps(
                {
                    "session_id": session_id,
                    "status": "handoff_ready",
                    "background_status": foreground.status.value,
                    "cwd": foreground.cwd,
                },
                sort_keys=True,
            )
        )
        return True

    from agenthicc.runners.tui_session import _run_tui_session

    try:
        await _run_tui_session(
            resume_id=session_id,
            cli_overrides=list(ctx.set_overrides),
            cli_secret_overrides=list(ctx.set_secret_overrides),
            record_cassette=ctx.record_cassette,
            cli_flags=ctx.flags,
            config_path=ctx.config_path,
            cwd=foreground.cwd,
            config=ctx.config,
            mode_name=ctx.mode_name,
        )
    except Exception as exc:  # noqa: BLE001
        Console(highlight=False).print(
            f"Unable to open foreground session {session_id}: {type(exc).__name__}: {exc}"
        )
        if fail_on_error:
            raise SystemExit(1) from exc
        return False
    return True


def _run_interactive(screen: Coroutine[object, object, None]) -> None:
    """Run an interactive screen; JSON paths never start an event loop (AGX-01)."""
    import asyncio

    asyncio.run(screen)


@optional_positionals("session_id")
def agents(
    ctx: CLIContext,
    session_id: str = "",
    tab: str = "",
    run: str = "",
    json: bool = False,
) -> None:
    """Open the agents hub: running work, what needs you, history and the library.

    SESSION_ID opens that agent's detail; ``--tab`` picks running, needs-you,
    history or library; ``--run`` shows one goal run's agents. ``--json``
    prints the agents (of the run) instead.
    """

    if run:
        from agenthicc.runs.cli import _manager

        try:
            payload = _manager(ctx).projection(run).to_dict()
        except KeyError:
            print(f"Run not found: {run}")
            raise SystemExit(1) from None
        if json:
            print(json_module.dumps(payload.get("agents", []), indent=2, sort_keys=True))
            return
    elif json:
        store, supervisor = _store_and_supervisor(ctx)
        positions = _queue_positions(supervisor)
        session_payload = [
            _public_session(item, queue_position=positions.get(item.session_id))
            for item in store.list()
        ]
        print(json_module.dumps(session_payload, indent=2, sort_keys=True))
        return
    # The hub is imported only now: listing paths stay light (AGX-01).
    from agenthicc.tui.workspace.agents_hub import Tab

    if tab and tab not in {item.value for item in Tab}:
        names = ", ".join(item.value for item in Tab)
        print(f"Unknown tab {tab!r}: use {names}")
        raise SystemExit(2)
    _run_interactive(_open_hub(ctx, tab=tab, session_id=session_id, run_id=run))


async def run(
    ctx: CLIContext,
    background: bool = False,
    workflow: str = "",
    intent: str = "",
    title: str = "",
    priority: str = "normal",
    idempotency_key: str = "",
    json: bool = False,
) -> None:
    """Run one request; ``--background`` returns after accepting a worker."""

    if not background:
        print("Use --background to start a durable background session.")
        return
    _store, supervisor = _store_and_supervisor(ctx)
    try:
        session = supervisor.submit(
            intent=intent,
            workflow_name=workflow,
            title=title,
            cwd=str(Path.cwd()),
            config_path=ctx.config_path,
            set_overrides=ctx.set_overrides,
            dangerously_skip_permissions=ctx.flags.dangerously_skip_permissions,
            set_secret_overrides=ctx.set_secret_overrides,
            mode_name=ctx.mode_name,
            priority=priority,
            idempotency_key=idempotency_key,
        )
    except (RuntimeError, ValueError) as exc:
        print(f"Unable to start background session: {exc}")
        raise SystemExit(1) from exc
    position = _queue_positions(supervisor).get(session.session_id)
    payload = _public_session(session, queue_position=position)
    if json:
        print(json_module.dumps(payload, indent=2, sort_keys=True))
    elif position is not None:
        print(
            f"Background session {session.session_id} accepted and queued "
            f"(advisory position {position})."
        )
    else:
        print(
            f"Background session {session.session_id} accepted; "
            f"worker state is {session.status.value}."
        )


def agents_definitions_list(ctx: CLIContext, json: bool = False) -> None:
    """List every agent definition and its validation and precedence state."""
    entries = _definition_entries()
    if json:
        print(
            json_module.dumps(
                [_definition_payload(item) for item in entries], indent=2, sort_keys=True
            )
        )
        return
    if not entries:
        print("No agent definitions.")
        return
    for entry in entries:
        marker = "★" if entry.default else " "
        print(
            f"{marker} {entry.name:20} {entry.source:16} {entry.state:10} "
            f"{entry.mode or '—':10} {entry.model or '(session)':20} {entry.tools or 'all tools'}"
        )
        if entry.note:
            print(f"  {entry.note}")
        if entry.path:
            print(f"  {entry.path}")


def agents_definitions_show(ctx: CLIContext, name: str) -> None:
    """Show every definition named NAME, including shadowed candidates."""
    matches = [entry for entry in _definition_entries() if entry.name == name]
    if not matches:
        print(f"Agent definition not found: {name}")
        raise SystemExit(1)
    for index, entry in enumerate(matches):
        if index:
            print()
        print(f"{entry.name} · {entry.source} · {entry.state}")
        if entry.description:
            print(f"Description: {entry.description}")
        if entry.mode:
            print(f"Mode: {entry.mode}")
        if entry.model:
            print(f"Model: {entry.model}")
        if entry.tools:
            print(f"Tools: {entry.tools}")
        if entry.path:
            print(f"Path: {entry.path}")
        if entry.note:
            print(f"Status: {entry.note}")
        if entry.spec is not None:
            print("\nPrompt:\n" + (entry.spec.prompt or "(empty)"))


def agents_definitions_validate(ctx: CLIContext) -> None:
    """Report invalid or untrusted agent definitions with their source paths."""
    entries = _definition_entries()
    problems = [entry for entry in entries if entry.state in ("invalid", "untrusted")]
    print(f"{len(entries)} agent definition(s) checked.")
    if not problems:
        print("All definitions are valid and trusted.")
        return
    for entry in problems:
        print(f"{entry.name} · {entry.state}: {entry.note or 'requires attention'}")
        if entry.path:
            print(f"  {entry.path}")
    raise SystemExit(1)


def agents_list(
    ctx: CLIContext,
    json: bool = False,
    all: bool = False,
    trash: bool = False,
) -> None:
    """List current, archived, or recoverable-trash agents (statuses as the hub shows them)."""
    from agenthicc.background.vocabulary import status_look

    store, supervisor = _store_and_supervisor(ctx)
    sessions = store.list(include_archived=all, include_deleted=trash)
    positions = _queue_positions(supervisor)
    payload = [
        _public_session(item, queue_position=positions.get(item.session_id)) for item in sessions
    ]
    if json:
        print(json_module.dumps(payload, indent=2, sort_keys=True))
        return
    if not payload:
        print("No background agents.")
        return
    for session, item in zip(sessions, payload, strict=True):
        look = status_look(session.status, queue_position=positions.get(session.session_id))
        print(
            f"{str(item.get('session_id', ''))[:16]}  "
            f"{look.glyph} {look.label:16}  "
            f"{str(item.get('title', ''))[:48]}  "
            f"{item.get('workflow_name') or 'direct'}  "
            f"priority={item.get('priority', 'normal')}  "
            + (
                f"queue=#{item['queue_position']} (advisory)"
                if item.get("queue_position") is not None
                else ""
            )
        )


def agents_status(ctx: CLIContext, session_id: str, json: bool = False) -> None:
    """Show redacted status for SESSION_ID."""

    store, supervisor = _store_and_supervisor(ctx)
    try:
        payload = _public_session(
            store.get(session_id, include_deleted=True),
            queue_position=_queue_positions(supervisor).get(session_id),
        )
    except KeyError:
        payload = {"session_id": session_id, "status": "not_found"}
    if json:
        print(json_module.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"Session: {payload.get('session_id', session_id)}")
        print(f"State: {payload.get('status', 'not_found')}")
        print(f"Priority: {payload.get('priority', 'normal')}")
        if payload.get("queue_position") is not None:
            print(f"Queue position: {payload['queue_position']} (advisory)")
        print(f"Dispatch: {payload.get('dispatch_state', 'legacy')}")
        print(f"Title: {payload.get('title', '')}")
        mode_application = payload.get("mode_application_status", "pending")
        if mode_application == "applied" and payload.get("mode_name"):
            print(f"Mode: {payload['mode_name']}")
        elif mode_application == "failed":
            requested_mode = payload.get("requested_mode_name")
            print(
                "Mode: failed"
                + (
                    f" (requested: {requested_mode or '<empty>'})"
                    if requested_mode is not None
                    else ""
                )
            )
        else:
            requested_mode = payload.get("requested_mode_name")
            print(
                "Mode: pending"
                + (
                    f" (requested: {requested_mode or '<empty>'})"
                    if requested_mode is not None
                    else " (default/persisted resolution)"
                )
            )
        if payload.get("mode_application_error"):
            print(f"Mode error: {payload['mode_application_error']}")
        print(f"Activity: {payload.get('latest_activity', '')}")
        if payload.get("error"):
            print(f"Error: {payload['error']}")


def _mutate(ctx: CLIContext, session_id: str, action: str) -> None:
    _store, supervisor = _store_and_supervisor(ctx)
    try:
        if action == "cancel":
            session = supervisor.cancel(session_id)
        elif action == "resume":
            session = (
                supervisor.resume(session_id)
                if ctx.mode_name is None
                else supervisor.resume(session_id, mode_name=ctx.mode_name)
            )
        elif action == "retry":
            session = (
                supervisor.retry(session_id)
                if ctx.mode_name is None
                else supervisor.retry(session_id, mode_name=ctx.mode_name)
            )
        elif action == "archive":
            session = supervisor.archive(session_id)
        elif action == "delete":
            session = supervisor.delete(session_id)
        elif action == "restore":
            session = supervisor.restore_deleted(session_id)
        else:
            raise ValueError(f"unknown agent operation: {action}")
    except (KeyError, RuntimeError, ValueError) as exc:
        print(f"Unable to {action} {session_id}: {exc}")
        raise SystemExit(1) from exc
    print(f"{action}: {session.session_id} → {session.status.value}")


def agents_cancel(ctx: CLIContext, session_id: str) -> None:
    _mutate(ctx, session_id, "cancel")


def agents_resume(ctx: CLIContext, session_id: str) -> None:
    _mutate(ctx, session_id, "resume")


def agents_retry(ctx: CLIContext, session_id: str) -> None:
    _mutate(ctx, session_id, "retry")


def agents_priority(ctx: CLIContext, session_id: str, priority: str) -> None:
    """Set priority to urgent, normal, or low and record an audit event."""

    _store, supervisor = _store_and_supervisor(ctx)
    try:
        session = supervisor.set_priority(session_id, priority)
    except (KeyError, RuntimeError, ValueError) as exc:
        print(f"Unable to change priority for {session_id}: {exc}")
        raise SystemExit(1) from exc
    print(f"priority: {session.session_id} → {session.priority.value}")


def agents_approve(ctx: CLIContext, session_id: str) -> None:
    _store, supervisor = _store_and_supervisor(ctx)
    try:
        session = supervisor.approve(session_id, True)
    except (KeyError, RuntimeError, ValueError) as exc:
        print(f"Unable to approve {session_id}: {exc}")
        raise SystemExit(1) from exc
    print(f"approve: {session.session_id} → {session.status.value}")


def agents_reject(ctx: CLIContext, session_id: str) -> None:
    _store, supervisor = _store_and_supervisor(ctx)
    try:
        session = supervisor.approve(session_id, False)
    except (KeyError, RuntimeError, ValueError) as exc:
        print(f"Unable to reject {session_id}: {exc}")
        raise SystemExit(1) from exc
    print(f"reject: {session.session_id} → {session.status.value}")


def agents_input(ctx: CLIContext, session_id: str, value: str) -> None:
    """Deliver VALUE to a session currently waiting for user input."""

    _store, supervisor = _store_and_supervisor(ctx)
    try:
        session = supervisor.provide_input(session_id, value)
    except (KeyError, RuntimeError, ValueError) as exc:
        print(f"Unable to provide input to {session_id}: {exc}")
        raise SystemExit(1) from exc
    print(f"input: {session.session_id} → {session.status.value}")


def agents_inbox_list(
    ctx: CLIContext,
    project: str = "",
    type: str = "",
    older_than_s: float = -1.0,
    status: str = "",
    limit: int = 100,
    json: bool = False,
) -> None:
    """List durable inbox metadata without exposing submitted answers."""

    from agenthicc.background.inbox import BackgroundInteractionInbox

    store, _supervisor = _store_and_supervisor(ctx)
    inbox = BackgroundInteractionInbox(store)
    inbox.reconcile()
    items = inbox.list_items(
        project_root=project or None,
        item_type=type or None,
        older_than_s=older_than_s if older_than_s >= 0 else None,
        status=status or None,
        limit=limit,
    )
    if json:
        print(json_module.dumps([item.to_dict() for item in items], indent=2, sort_keys=True))
        return
    if not items:
        print("No pending background inbox items.")
        return
    for item in items:
        target = item.target
        description = target.get("tool_name", item.kind)
        print(
            f"{item.item_id}  {item.kind:8}  {item.status:9}  "
            f"version={item.version}  {item.workspace}  "
            f"session={item.session_id} attempt={item.attempt}  {description}"
        )


def _parse_inbox_version(value: str) -> int:
    try:
        version = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("VERSION must be a positive integer") from exc
    if version < 1:
        raise ValueError("VERSION must be a positive integer")
    return version


def agents_inbox_answer(
    ctx: CLIContext,
    item_id: str,
    response: str,
    expected_version: str,
    idempotency_key: str,
) -> None:
    """Answer exact inbox item; multi-question response must be a JSON object."""

    from agenthicc.background.inbox import (
        BackgroundInteractionInbox,
        response_from_text,
    )

    store, _supervisor = _store_and_supervisor(ctx)
    inbox = BackgroundInteractionInbox(store)
    try:
        item = inbox.get(item_id)
        if item.kind == "approval":
            raise ValueError("approval items must use the explicit approve or reject command")
        accepted, replay = inbox.respond(
            item_id,
            response_from_text(item, response),
            expected_version=_parse_inbox_version(expected_version),
            idempotency_key=idempotency_key,
        )
    except (KeyError, RuntimeError, ValueError) as exc:
        print(f"Unable to answer inbox item {item_id}: {exc}")
        raise SystemExit(1) from exc
    print(f"answer: {accepted.item_id} → {accepted.status} (version {accepted.version})")
    if replay:
        print("idempotent replay")


def _resolve_inbox_approval(
    ctx: CLIContext,
    item_id: str,
    allowed: bool,
    expected_version: str,
    idempotency_key: str,
    *,
    message: str = "",
    mode: str = "",
    remember: bool = False,
    remember_all: bool = False,
    scope_grant: str = "",
) -> None:
    from agenthicc.background.inbox import BackgroundInteractionInbox

    store, _supervisor = _store_and_supervisor(ctx)
    inbox = BackgroundInteractionInbox(store)
    try:
        item = inbox.get(item_id)
        response: dict[str, object] = {"allowed": allowed}
        if message:
            response["message"] = message
        if mode:
            response["mode"] = mode
        if remember:
            response["remember"] = True
        if remember_all:
            response["remember_all"] = True
        if scope_grant:
            response["scope_grant"] = scope_grant
        elif allowed and item.target.get("workspace_access"):
            response["scope_grant"] = "target_once"
        updated, replay = inbox.respond(
            item_id,
            response,
            expected_version=_parse_inbox_version(expected_version),
            idempotency_key=idempotency_key,
        )
    except (KeyError, RuntimeError, ValueError) as exc:
        print(f"Unable to resolve inbox item {item_id}: {exc}")
        raise SystemExit(1) from exc
    print(f"{'approve' if allowed else 'reject'}: {updated.item_id} → {updated.status}")
    if replay:
        print("idempotent replay")


def agents_inbox_approve(
    ctx: CLIContext,
    item_id: str,
    expected_version: str,
    idempotency_key: str,
    message: str = "",
    mode: str = "",
    remember: bool = False,
    remember_all: bool = False,
    scope_grant: str = "",
) -> None:
    _resolve_inbox_approval(
        ctx,
        item_id,
        True,
        expected_version,
        idempotency_key,
        message=message,
        mode=mode,
        remember=remember,
        remember_all=remember_all,
        scope_grant=scope_grant,
    )


def agents_inbox_reject(
    ctx: CLIContext,
    item_id: str,
    expected_version: str,
    idempotency_key: str,
    message: str = "",
) -> None:
    _resolve_inbox_approval(
        ctx,
        item_id,
        False,
        expected_version,
        idempotency_key,
        message=message,
    )


def agents_inbox_expire(
    ctx: CLIContext,
    item_id: str,
    expected_version: str,
    idempotency_key: str,
    reason: str = "expired by operator",
) -> None:
    from agenthicc.background.inbox import BackgroundInteractionInbox

    store, _supervisor = _store_and_supervisor(ctx)
    inbox = BackgroundInteractionInbox(store)
    try:
        updated = inbox.expire(
            item_id,
            expected_version=_parse_inbox_version(expected_version),
            idempotency_key=idempotency_key,
            reason=reason,
        )
    except (KeyError, RuntimeError, ValueError) as exc:
        print(f"Unable to expire inbox item {item_id}: {exc}")
        raise SystemExit(1) from exc
    print(f"expire: {updated.item_id} → {updated.status}")


def agents_notifications(
    ctx: CLIContext,
    session_id: str = "",
    status: str = "",
    limit: int = 100,
    json: bool = False,
) -> None:
    """Show safe delivery receipts without payloads, targets, or secret values."""

    from agenthicc.background.notifications import BackgroundNotificationOutbox

    if status and status not in {"pending", "sending", "delivered", "failed"}:
        raise SystemExit("status must be pending, sending, delivered, or failed")
    store, _supervisor = _store_and_supervisor(ctx)
    outbox = BackgroundNotificationOutbox(store.root)
    rows = outbox.list_deliveries(limit=limit, status=status, session_id=session_id)
    if json:
        print(json_module.dumps(rows, indent=2, sort_keys=True))
        return
    if not rows:
        print("No background notification deliveries.")
        return
    for row in rows:
        next_attempt = row.get("next_attempt_at")
        retry_text = f" next={next_attempt:.0f}" if isinstance(next_attempt, (int, float)) else ""
        print(
            f"{str(row.get('event_id', ''))[:16]}  "
            f"{str(row.get('destination_name', ''))[:24]:24}  "
            f"{str(row.get('status', 'unknown')):10}  "
            f"attempt={row.get('attempts', 0)}/{row.get('max_attempts', 0)}  "
            f"result={row.get('last_result_category') or '—'}{retry_text}"
        )


def agents_rename(ctx: CLIContext, session_id: str, title: str) -> None:
    """Set a bounded, local-only display title."""

    store, _supervisor = _store_and_supervisor(ctx)
    try:
        session = store.rename(session_id, title)
    except (KeyError, ValueError) as exc:
        print(f"Unable to rename {session_id}: {exc}")
        raise SystemExit(1) from exc
    print(f"rename: {session.session_id} → {session.title}")


def agents_labels(ctx: CLIContext, session_id: str, labels: str = "") -> None:
    """Replace user labels; labels are not sent to providers."""

    store, _supervisor = _store_and_supervisor(ctx)
    values = tuple(item.strip() for item in labels.split(",") if item.strip())
    try:
        session = store.set_labels(session_id, values)
    except (KeyError, ValueError) as exc:
        print(f"Unable to set labels on {session_id}: {exc}")
        raise SystemExit(1) from exc
    print(f"labels: {session.session_id} → {', '.join(session.labels) or 'none'}")


def agents_purge(ctx: CLIContext) -> None:
    """Apply the configured recoverable-trash retention policy."""

    _store, supervisor = _store_and_supervisor(ctx)
    removed = supervisor.purge_expired_trash()
    print(f"purged: {len(removed)}")


def agents_archive(ctx: CLIContext, session_id: str) -> None:
    _mutate(ctx, session_id, "archive")


def agents_delete(ctx: CLIContext, session_id: str) -> None:
    _mutate(ctx, session_id, "delete")


def agents_restore(ctx: CLIContext, session_id: str) -> None:
    _mutate(ctx, session_id, "restore")
