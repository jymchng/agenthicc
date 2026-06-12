"""Entry point for ``python -m agenthicc`` and the ``agenthicc`` command."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path


# ── session index helpers ────────────────────────────────────────────────────

_SESSIONS_DIR  = Path(".agenthicc/sessions")
_SESSION_INDEX = Path(".agenthicc/sessions.json")


def _load_session_index() -> dict:
    if _SESSION_INDEX.exists():
        try:
            return json.loads(_SESSION_INDEX.read_text())
        except Exception:
            return {}
    return {}


def _save_session_index(index: dict) -> None:
    _SESSION_INDEX.parent.mkdir(parents=True, exist_ok=True)
    _SESSION_INDEX.write_text(json.dumps(index, indent=2))


def _register_session(session_id: str) -> None:
    index = _load_session_index()
    index[session_id] = {
        "cwd": os.getcwd(),
        "created_at": time.time(),
        "last_used": time.time(),
        "log_path": str(_SESSIONS_DIR / f"{session_id}.jsonl"),
    }
    _save_session_index(index)


def _touch_session(session_id: str) -> None:
    index = _load_session_index()
    if session_id in index:
        index[session_id]["last_used"] = time.time()
        _save_session_index(index)


def _find_latest_session_for_cwd() -> str | None:
    index = _load_session_index()
    cwd = os.getcwd()
    candidates = [
        (data.get("last_used", 0), sid)
        for sid, data in index.items()
        if data.get("cwd") == cwd
    ]
    return max(candidates)[1] if candidates else None


def _get_session_log_path(session_id: str) -> Path | None:
    index = _load_session_index()
    entry = index.get(session_id)
    if entry:
        return Path(entry["log_path"])
    return None


# ── argument parsing ──────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="agenthicc",
        description="Agenthicc — state-driven agent OS for autonomous software engineering",
    )
    parser.add_argument("--headless", action="store_true",
                        help="Run without the TUI; emit JSON-lines to stdout.")
    parser.add_argument("--config", metavar="PATH", default=None,
                        help="Path to agenthicc.toml.")
    parser.add_argument("--version", action="version", version="agenthicc 0.1.0")
    parser.add_argument("--continue", dest="continue_session", action="store_true",
                        help="Continue the most recent session for this directory.")
    parser.add_argument("--resume", metavar="ID", default=None,
                        help="Resume the session with the given ID.")

    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("login",  help="Authenticate with agenthicc.ai")
    subparsers.add_parser("logout", help="Log out and revoke tokens")
    subparsers.add_parser("whoami", help="Show current authenticated user")
    subparsers.add_parser("sessions", help="List saved sessions")

    return parser.parse_args()


# ── auth subcommands ──────────────────────────────────────────────────────────

async def _do_login() -> None:
    from agenthicc.auth import AuthClient
    client = AuthClient()
    bundle = await client.login()
    print(f"Logged in as {bundle.email}  [plan: {bundle.plan}]")


async def _do_logout() -> None:
    from agenthicc.auth import AuthClient
    await AuthClient().logout()
    print("Logged out.")


def _do_whoami() -> None:
    from agenthicc.auth import AuthClient
    bundle = AuthClient().current_bundle()
    if bundle is None:
        print("Not logged in. Run: agenthicc login")
    else:
        exp = time.strftime("%Y-%m-%d %H:%M", time.localtime(bundle.expires_at))
        print(f"{bundle.email}  plan={bundle.plan}  token_expires={exp}")


def _do_sessions() -> None:
    index = _load_session_index()
    if not index:
        print("No saved sessions.")
        return
    cwd = os.getcwd()
    for sid, data in sorted(index.items(), key=lambda x: x[1].get("last_used", 0), reverse=True):
        marker = " *" if data.get("cwd") == cwd else ""
        last = time.strftime("%Y-%m-%d %H:%M", time.localtime(data.get("last_used", 0)))
        print(f"  {sid[:12]}  {last}  {data.get('cwd', '')} {marker}")


# ── headless mode ─────────────────────────────────────────────────────────────

async def _run_headless() -> None:
    from agenthicc.kernel import AppState, Event, EventProcessor, SecurityPolicy, SystemSettings

    state = AppState.create(settings=SystemSettings(), policy=SecurityPolicy())
    processor = EventProcessor(initial_state=state, persist=False)
    sub = processor.subscribe()
    proc_task = asyncio.create_task(processor.run())
    print(json.dumps({"status": "ready", "mode": "headless"}), flush=True)
    try:
        while True:
            line = await asyncio.get_event_loop().run_in_executor(None, sys.stdin.readline)
            if not line:
                break
            text = line.strip()
            if not text:
                continue
            intent_id = uuid.uuid4().hex
            await processor.emit(Event.create("IntentCreated", {"intent_id": intent_id, "raw_text": text}))
            try:
                snap = await asyncio.wait_for(sub.get(), timeout=2.0)
                intent = snap.intents.get(intent_id)
                print(json.dumps({"event_type": "IntentCreated", "intent_id": intent_id,
                                  "status": intent.status.value if intent else "pending"}), flush=True)
            except asyncio.TimeoutError:
                print(json.dumps({"event_type": "Error", "message": "timeout"}), flush=True)
    finally:
        proc_task.cancel()
        await asyncio.gather(proc_task, return_exceptions=True)


# ── TUI session ───────────────────────────────────────────────────────────────

async def _run_tui_session(resume_id: str | None = None) -> None:
    from agenthicc.kernel import AppState, Event, EventProcessor, SecurityPolicy, SystemSettings
    from agenthicc.kernel.reducer import root_reducer
    from agenthicc.kernel.processor import restore_from_log
    from agenthicc.tui.transcript import TranscriptModel
    from agenthicc.tui.events import TUIEventAdapter
    from agenthicc.tui.app import InlineRenderer

    session_id = resume_id or uuid.uuid4().hex
    _SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = str(_SESSIONS_DIR / f"{session_id}.jsonl")

    settings = SystemSettings(
        event_log_path=log_path,
        snapshot_path=".agenthicc/snapshot.json",
    )
    state = AppState.create(settings=settings, policy=SecurityPolicy())

    # Restore from log when resuming
    if resume_id:
        log_file = _get_session_log_path(resume_id)
        if log_file and log_file.exists():
            state = await restore_from_log(str(log_file), state, root_reducer)
        _touch_session(resume_id)
    else:
        _register_session(session_id)

    processor = EventProcessor(initial_state=state, persist=True)
    model = TranscriptModel()
    adapter = TUIEventAdapter(model)
    adapter.subscribe_to(processor)

    # Re-render tail of resumed session
    if resume_id:
        from rich.console import Console
        from rich.rule import Rule
        con = Console()
        con.print(Rule(f"[dim]resumed session {resume_id[:12]}[/dim]"))
        for line in model.render()[-25:]:
            con.print(line, markup=False, highlight=False)

    # Start ad rotator for free-tier authenticated users
    ad_task: asyncio.Task | None = None
    try:
        from agenthicc.auth import AuthClient, NotLoggedInError
        from agenthicc.ads import AdRotator
        auth_client = AuthClient()
        bundle = auth_client.current_bundle()
        if bundle is not None and not bundle.is_pro:
            rotator = AdRotator(auth_client=auth_client, processor=processor)
            ad_task = asyncio.create_task(rotator.run())
    except Exception:
        pass  # ads never block startup

    renderer = InlineRenderer(
        model, adapter,
        base_path=os.getcwd(),
        history_file=".agenthicc/history",
    )
    renderer._processor = processor

    def on_intent(text: str) -> None:
        intent_id = uuid.uuid4().hex
        asyncio.get_event_loop().call_soon_threadsafe(
            lambda: asyncio.ensure_future(processor.emit(
                Event.create("IntentCreated", {"intent_id": intent_id, "raw_text": text})
            ))
        )

    proc_task = asyncio.create_task(processor.run())
    try:
        await renderer.run(on_intent)
    finally:
        proc_task.cancel()
        if ad_task is not None:
            ad_task.cancel()
        await asyncio.gather(proc_task, *(([ad_task] if ad_task else [])), return_exceptions=True)


def _run_tui(args: argparse.Namespace) -> None:
    try:
        from rich.console import Console  # noqa: F401
        from prompt_toolkit import PromptSession  # noqa: F401
    except ImportError:
        print(
            "error: TUI requires rich and prompt_toolkit:\n"
            "  pip install agenthicc[tui]\n"
            "Or run headless: agenthicc --headless",
            file=sys.stderr,
        )
        sys.exit(1)

    resume_id: str | None = None
    if args.resume:
        resume_id = args.resume
    elif args.continue_session:
        resume_id = _find_latest_session_for_cwd()
        if resume_id is None:
            print("No previous session found for this directory. Starting fresh.")

    try:
        asyncio.run(_run_tui_session(resume_id=resume_id))
    except Exception as exc:
        print(f"TUI error: {exc}", file=sys.stderr)
        sys.exit(1)


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    args = _parse_args()

    if args.command == "login":
        asyncio.run(_do_login())
    elif args.command == "logout":
        asyncio.run(_do_logout())
    elif args.command == "whoami":
        _do_whoami()
    elif args.command == "sessions":
        _do_sessions()
    elif args.headless:
        asyncio.run(_run_headless())
    else:
        _run_tui(args)


if __name__ == "__main__":
    main()
