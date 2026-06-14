"""TUI session orchestrator — kernel init, renderer setup, and intent loop."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from agenthicc.sessions import (
    _SESSIONS_DIR,
    _find_latest_session_for_cwd,
    _get_session_log_path,
    _register_session,
    _touch_session,
)
from agenthicc.runners.agent_builder import _build_agent_runner
from agenthicc.runners.agent_turn import _run_agent_turn


async def _run_tui_session(resume_id: str | None = None, cli_overrides: list[str] | None = None) -> None:
    from agenthicc.kernel import AppState, Event, EventProcessor, SecurityPolicy, SystemSettings
    from agenthicc.kernel.reducer import root_reducer
    from agenthicc.kernel.processor import restore_from_log
    from agenthicc.tui.transcript import TranscriptModel
    from agenthicc.tui.events import TUIEventAdapter
    from agenthicc.tui.app import InlineRenderer
    from agenthicc.config import load_config, build_llm_config
    from agenthicc.conversation_store import ConversationStore  # noqa: PLC0415

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

    # Load config and build LLM transport for agent runs
    cfg = load_config(cli_overrides=cli_overrides or [])
    try:
        llm_cfg = build_llm_config(cfg.execution)
    except ValueError as exc:
        from rich.console import Console  # noqa: PLC0415
        Console().print(f"[red]LLM config error: {exc}[/red]\n"
                        "[dim]Set ANTHROPIC_API_KEY or OPENAI_API_KEY env var, or use --set execution.provider=...[/dim]",
                        markup=True)
        llm_cfg = None

    renderer = InlineRenderer(
        model, adapter,
        base_path=os.getcwd(),
        history_file=".agenthicc/history",
    )
    renderer._processor = processor
    renderer._status.session_id = (
        f"{cfg.execution.provider}/{cfg.execution.effective_model()}"
    )
    renderer._status.resume_id = session_id
    renderer._loaded_config = cfg   # used by ConfigurationMenu

    renderer._active_agent = "default"
    renderer._exec_cfg = cfg.execution  # expose to _run_agent_turn for @mention config

    # ── skills discovery ──────────────────────────────────────────────
    from agenthicc.skills.loader import discover_skills as _discover_skills
    _skills = _discover_skills(
        project_dir=Path(".agenthicc"),
        user_dir=Path.home() / ".agenthicc",
    )
    renderer._skills = _skills

    # ── tool plugin discovery ─────────────────────────────────────────────
    from agenthicc.plugins.discovery import discover_project_tools, warn_conflicts  # noqa: PLC0415
    _project_plugins = discover_project_tools(
        project_dir=Path(".agenthicc"),
        user_dir=Path.home() / ".agenthicc",
    )
    warn_conflicts(_project_plugins)
    renderer._project_plugin_tools = _project_plugins.all_tools
    if _project_plugins.all_tools:
        from rich.console import Console as _C  # noqa: PLC0415
        _C().print(f"[dim]Loaded {len(_project_plugins.all_tools)} plugin "
                   f"tool(s) from .agenthicc/tools/[/dim]")

    # ── MCP server initialisation ─────────────────────────────────────────
    _mcp_registry = None
    if cfg.tools.mcp_servers:
        try:
            from agenthicc.tools.mcp import McpToolRegistry  # noqa: PLC0415
            _mcp_registry = McpToolRegistry(event_processor=processor)
            for srv_cfg in cfg.tools.mcp_servers:
                _mcp_registry.register_server(srv_cfg)
            discovered = await _mcp_registry.discover_all()
            if discovered:
                from rich.console import Console as _Con  # noqa: PLC0415
                _Con().print(f"[dim]MCP: {len(discovered)} tool(s) from {len(cfg.tools.mcp_servers)} server(s)[/dim]")
            renderer._mcp_registry = _mcp_registry
        except Exception as exc:  # noqa: BLE001
            import logging as _log  # noqa: PLC0415
            _log.getLogger(__name__).error("MCP init failed: %s", exc)

    # ── @mention cache ────────────────────────────────────────────────────
    from agenthicc.mentions.cache import MentionCache  # noqa: PLC0415
    _mention_cache = MentionCache()
    renderer._mention_cache = _mention_cache

    # ── conversation store + memory ───────────────────────────────────────
    from lauren_ai._memory import ShortTermMemory  # noqa: PLC0415
    _session_memory = ShortTermMemory(max_tokens=32_000)
    conv_store = ConversationStore()
    _turn_index = [conv_store.next_turn_index(session_id)]

    # On resume: replay history into the transcript and restore LLM memory
    if resume_id:
        import shutil as _sh  # noqa: PLC0415
        from rich.console import Console as _Con  # noqa: PLC0415
        from rich.markdown import Markdown as _Md  # noqa: PLC0415
        from rich.rule import Rule  # noqa: PLC0415
        _con = _Con(highlight=False, markup=True)
        _cols = _sh.get_terminal_size((80, 24)).columns
        _con.print(Rule(f"[dim]resumed session {resume_id[:12]}[/dim]"))
        past_turns = conv_store.load_turns(resume_id)
        for turn in past_turns[-20:]:  # display last 20 Q&A pairs
            if "user" in turn:
                _con.print(f"[dim]❯ {turn['user']}[/dim]")
                _con.print(f"[dim]{'─' * _cols}[/dim]")
            if "assistant" in turn:
                ms = turn.get("model_short", "assistant")
                ts = turn.get("timestamp", time.time())
                hhmmss = time.strftime("%H:%M:%S", time.localtime(ts))
                _con.print(
                    f"[bold cyan]●[/] [bold]assistant ({ms})[/]  [dim]{hhmmss}[/dim]"
                )
                _con.print(_Md(turn["assistant"]), highlight=False)
        # Restore LLM context so the agent remembers past turns
        snapshot = conv_store.load_memory_snapshot(resume_id)
        if snapshot:
            _session_memory.restore(snapshot)

    # Build the lauren-ai runner that calls the LLM
    agent_runner = _build_agent_runner(llm_cfg, transcript=model)

    _MD_SENTINEL = InlineRenderer._MD_SENTINEL
    _pending_queue: list[str] = []   # FIFO queue for messages submitted while agent runs

    async def on_intent(text: str) -> None:
        from rich.markup import escape as _markup_escape  # noqa: PLC0415

        async def _run_one(user_text: str, turn_idx: int) -> None:
            """Save user turn, run agent, save assistant turn for one message."""
            conv_store.save_turn(session_id, turn_idx, "user", user_text, time.time())
            turns_before = len(model.turns)
            await _run_agent_turn(
                user_text, agent_runner, model, renderer, processor,
                session_memory=_session_memory,
                max_agent_turns=cfg.execution.max_agent_turns,
                pending_queue=_pending_queue,
            )
            if len(model.turns) > turns_before:
                last = model.turns[-1]
                content = "\n".join(
                    ln.replace(_MD_SENTINEL, "") for ln in last.lines
                ).strip()
                ms = last.agent_name.replace("assistant (", "").rstrip(")")
                conv_store.save_turn(session_id, turn_idx, "assistant", content,
                                     time.time(), model_short=ms)

        await _run_one(text, _turn_index[0])
        _turn_index[0] += 1

        # Drain messages that arrived while the agent was running, one at a time.
        try:
            while _pending_queue:
                next_text = _pending_queue.pop(0)
                renderer.console.print(
                    f"[bold green]❯[/bold green] {_markup_escape(next_text)}",
                    markup=True, highlight=False,
                )
                renderer._flush_new_lines()
                await _run_one(next_text, _turn_index[0])
                _turn_index[0] += 1
        except asyncio.CancelledError:
            _pending_queue.clear()  # discard unprocessed messages on interrupt
            raise

        # Persist memory once after the whole burst is drained (not per-turn).
        conv_store.save_memory_snapshot(session_id, _session_memory.snapshot())

    # Start ad rotator for free-tier authenticated users
    ad_task: asyncio.Task | None = None
    try:
        from agenthicc.auth import AuthClient, NotLoggedInError  # noqa: PLC0415
        from agenthicc.ads import AdRotator  # noqa: PLC0415
        auth_client = AuthClient()
        bundle = auth_client.current_bundle()
        if bundle is not None and not bundle.is_pro:
            rotator = AdRotator(auth_client=auth_client, processor=processor)
            ad_task = asyncio.create_task(rotator.run())
    except Exception:
        pass  # ads never block startup

    proc_task = asyncio.create_task(processor.run())
    try:
        await renderer.run(on_intent)
    finally:
        proc_task.cancel()
        if ad_task is not None:
            ad_task.cancel()
        await asyncio.gather(proc_task, *(([ad_task] if ad_task else [])), return_exceptions=True)
        conv_store.close()
        if _mcp_registry is not None:
            await _mcp_registry.shutdown()


def _run_tui(args: argparse.Namespace) -> None:
    try:
        from rich.console import Console  # noqa: F401
    except ImportError:
        print(
            "error: TUI requires rich:\n"
            "  pip install agenthicc\n"
            "Or run headless: agenthicc --headless",
            file=sys.stderr,
        )
        sys.exit(1)

    cli_overrides = getattr(args, "set_overrides", [])

    resume_id: str | None = None
    if args.resume:
        resume_id = args.resume
    elif args.continue_session:
        resume_id = _find_latest_session_for_cwd()
        if resume_id is None:
            print("No previous session found for this directory. Starting fresh.")

    try:
        asyncio.run(_run_tui_session(resume_id=resume_id, cli_overrides=cli_overrides))
    except Exception as exc:
        print(f"TUI error: {exc}", file=sys.stderr)
        sys.exit(1)
