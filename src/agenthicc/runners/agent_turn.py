"""Single agent turn: LLM streaming, Textual message signals, tool signals, transcript wiring."""
from __future__ import annotations

import asyncio
import os
import time
import uuid
from pathlib import Path
from typing import Any


def _is_textual_app(renderer: Any) -> bool:
    """Return True when *renderer* is a Textual App instance (e.g. AgenthiccApp).

    Uses isinstance check against textual.app.App to be robust against
    attribute duck-typing false positives.  Falls back to False if textual
    is not installed.
    """
    try:
        from textual.app import App  # noqa: PLC0415
        return isinstance(renderer, App)
    except ImportError:
        return False


def _post_message_safe(renderer: Any, msg: Any) -> None:
    """Post a Textual message through the renderer, or no-op on failure.

    When *renderer* is an :class:`~agenthicc.tui.app.AgenthiccApp` (running
    inside Textual's event loop) ``post_message`` can be called directly —
    agent_turn coroutines are scheduled via ``asyncio.ensure_future`` from
    within Textual's event loop, so no thread-crossing is required.

    When running in headless mode the call is a graceful no-op.
    """
    if not _is_textual_app(renderer):
        return
    try:
        renderer.post_message(msg)
    except Exception:
        pass


def _renderer_print(renderer: Any, markup: str) -> None:
    """Print *markup* to the transcript / console, routing by renderer type.

    For Textual apps (AgenthiccApp) the markup is posted as a ConsolePrint
    message so the TranscriptView widget receives it on the event loop.

    For headless paths it falls back to
    ``renderer.console.print(markup, ...)``.
    """
    try:
        if _is_textual_app(renderer):
            from agenthicc.tui.messages import ConsolePrint  # noqa: PLC0415
            renderer.post_message(ConsolePrint(str(markup)))
        else:
            renderer.console.print(markup, markup=True, highlight=False)
    except Exception:
        pass


async def _run_agent_turn(
    text: str,
    runner: Any,
    transcript: Any,
    renderer: Any,
    processor: Any,
    session_memory: Any = None,
    max_agent_turns: int = 200,
    pending_queue: list | None = None,
) -> None:
    """Run one agent turn: call LLM, stream response into transcript, emit Textual messages.

    Previously this function used ``rich.live.Live`` for the spinner and a raw
    terminal ``_watch_input`` loop for keystrokes.  Those have been replaced by
    Textual messages (``AgentRunStarted``, ``ToolCallStarted``, ``ToolCallComplete``,
    ``TokensUpdated``, ``AgentRunFinished``) so that Textual widgets
    (``SpinnerPanel``, ``StatusBar``) react reactively without touching the
    terminal directly.

    Backward-compat: when ``renderer`` has no ``.app`` attribute (headless
    mode) the message-posting is silently skipped and only the console-print
    / transcript paths execute.
    """
    import re as _re  # noqa: PLC0415
    from lauren_ai._agents import agent as agent_decorator, use_tools  # noqa: PLC0415
    from lauren_ai.testing import _build_runner_for_agent  # noqa: PLC0415
    from agenthicc.kernel import Event  # noqa: PLC0415
    from agenthicc.agent_tools import AGENT_TOOLS  # noqa: PLC0415
    from agenthicc.tui.messages import (  # noqa: PLC0415
        AgentRunFinished,
        AgentRunStarted,
        ToolCallComplete as _TCCMsg,
        ToolCallStarted as _TCSMsg,
        TokensUpdated,
    )

    if runner is None:
        transcript.append_turn("system", "system", time.monotonic())
        transcript.append_line("system", "⚠ No LLM configured. Set ANTHROPIC_API_KEY or OPENAI_API_KEY.")
        return

    # ── resolve model_id FIRST (fixes UnboundLocalError) ──────────────────
    model_id = (
        runner._transport._config.model
        if hasattr(runner._transport, "_config")
        else "unknown"
    )
    model_short = model_id.split("/")[-1]

    # ── kernel event ──────────────────────────────────────────────────────
    intent_id = uuid.uuid4().hex
    await processor.emit(Event.create("IntentCreated", {"intent_id": intent_id, "raw_text": text}))

    agent_id = f"agent-{intent_id[:8]}"
    transcript.append_turn(agent_id, f"assistant ({model_short})", time.monotonic())

    # ── let signal bridge know the current agent ──────────────────────────
    cell = getattr(getattr(runner, "_signals", None), "_current_agent_cell", None)
    if cell is not None:
        cell[0] = agent_id

    # ── status bar ────────────────────────────────────────────────────────
    # Update status state (used by headless / AgenthiccApp).
    renderer._status.active = True
    renderer._status.intent_started_at = time.monotonic()
    renderer._status.input_tokens = 0
    renderer._status.output_tokens = 0

    # Post Textual message so StatusBar / SpinnerPanel widgets activate.
    _post_message_safe(renderer, AgentRunStarted(agent_id, model_short))

    # Wire ModelCallComplete signal → live token count updates.
    # Fires once per LLM turn so counts accumulate in real-time during multi-turn runs.
    _signals = getattr(runner, "_signals", None)

    # Live tool call list for transcript display.
    # Each entry: {"id": str, "name": str, "args": str, "done": bool, "ok": bool, "ms": float|None}
    _live_calls: list[dict] = []
    # Keyed by tool_use_id: (relative_path, original_content) captured before the call.
    _file_snapshots: dict[str, tuple[str, str]] = {}
    # File-editing tool names that warrant a unified diff in the transcript.
    _FILE_EDIT_TOOLS = {"write_file", "patch_file", "append_file"}

    if _signals is not None:
        from lauren_ai._signals import ModelCallComplete as _MCC  # noqa: PLC0415
        from lauren_ai._signals import ToolCallStarted as _TCS, ToolCallComplete as _TCC  # noqa: PLC0415

        @_signals.on(_MCC)
        async def _on_model_complete(sig: Any) -> None:
            usage = getattr(sig, "usage", None)
            if usage:
                renderer._status.input_tokens += getattr(usage, "input_tokens", 0)
                renderer._status.output_tokens += getattr(usage, "output_tokens", 0)
            cost = getattr(sig, "cost_usd", 0.0) or 0.0
            renderer._status.session_cost_usd += cost
            # Post token update to Textual widgets.
            _post_message_safe(
                renderer,
                TokensUpdated(
                    input_tokens=getattr(usage, "input_tokens", 0) if usage else 0,
                    output_tokens=getattr(usage, "output_tokens", 0) if usage else 0,
                    cost_usd=cost,
                ),
            )

        @_signals.on(_TCS)
        async def _on_tool_started(sig: Any) -> None:
            args = dict(getattr(sig, "input", {}) or {})
            tool_name = getattr(sig, "tool_name", "")
            tid = getattr(sig, "tool_use_id", "")
            # Format args as Claude Code style: first value only if single, else key=val pairs.
            items = list(args.items())
            if len(items) == 1:
                args_str = repr(items[0][1])[:50]
            elif items:
                args_str = ", ".join(f"{k}={repr(v)[:25]}" for k, v in items[:3])
                if len(items) > 3:
                    args_str += ", …"
            else:
                args_str = ""
            _live_calls.append({
                "id": tid,
                "name": tool_name,
                "args": args_str,
                "done": False,
                "ok": True,
                "ms": None,
            })
            # Post Textual message so SpinnerPanel updates.
            _post_message_safe(renderer, _TCSMsg(tool_use_id=tid, name=tool_name, args=args))
            # Snapshot file content before write/patch so we can produce a diff later.
            if tool_name in _FILE_EDIT_TOOLS:
                rel_path = args.get("path", "")
                if rel_path:
                    full = os.path.join(os.getcwd(), rel_path) if not os.path.isabs(rel_path) else rel_path
                    try:
                        original = await asyncio.to_thread(
                            lambda p=full: open(p).read() if os.path.exists(p) else ""
                        )
                        _file_snapshots[tid] = (rel_path, original)
                    except Exception:
                        pass

        @_signals.on(_TCC)
        async def _on_tool_complete(sig: Any) -> None:
            import difflib as _dl  # noqa: PLC0415
            tid = getattr(sig, "tool_use_id", "")
            ok = bool(getattr(sig, "success", True))
            ms = getattr(sig, "duration_ms", None)
            diff: str | None = None
            for entry in _live_calls:
                if entry["id"] == tid:
                    entry["done"] = True
                    entry["ok"] = ok
                    entry["ms"] = ms
                    break
            # Generate unified diff for file-editing tools and store as transcript output.
            if tid in _file_snapshots:
                rel_path, original = _file_snapshots.pop(tid)
                full = os.path.join(os.getcwd(), rel_path) if not os.path.isabs(rel_path) else rel_path
                try:
                    new_content = await asyncio.to_thread(
                        lambda p=full: open(p).read() if os.path.exists(p) else ""
                    )
                    diff = "".join(_dl.unified_diff(
                        original.splitlines(keepends=True),
                        new_content.splitlines(keepends=True),
                        fromfile=f"a/{rel_path}",
                        tofile=f"b/{rel_path}",
                        lineterm="",
                    )) or None
                    if diff:
                        transcript.finish_tool_call(tool_use_id=tid, output=diff)
                        for entry in _live_calls:
                            if entry["id"] == tid:
                                entry["diff"] = diff
                                break
                except Exception:
                    pass
            # Post Textual message so SpinnerPanel updates.
            _post_message_safe(
                renderer,
                _TCCMsg(
                    tool_use_id=tid,
                    success=ok,
                    duration_ms=ms,
                    error=None,
                    diff=diff,
                ),
            )

    # ── @mention injection ────────────────────────────────────────────
    from agenthicc.mentions.injector import build_context_prefix, InjectionConfig  # noqa: PLC0415
    from agenthicc.mentions.parser import parse_mentions as _parse_mentions  # noqa: PLC0415
    from agenthicc.tui.transcript import MentionChip  # noqa: PLC0415
    _exec_cfg = getattr(renderer, "_exec_cfg", None)
    _mention_cfg = InjectionConfig(
        mention_token_budget=getattr(_exec_cfg, "mention_token_budget", 32_000),
        max_file_chars=getattr(_exec_cfg, "mention_max_file_chars", 16_000),
        max_glob_files=getattr(_exec_cfg, "mention_max_glob_files", 20),
        cwd=Path(os.getcwd()),
    )
    _mention_cache_ref = getattr(renderer, "_mention_cache", None)
    _mention_prefix, _injected = await build_context_prefix(
        text,
        cwd=_mention_cfg.cwd,
        cfg=_mention_cfg,
        cache=_mention_cache_ref,
        current_turn=renderer._status.completed_agents,
    )
    _agent_text = _mention_prefix + text if _mention_prefix else text

    # Add @mention chips to transcript
    if _injected:
        from agenthicc.mentions.parser import MentionKind as _MK  # noqa: PLC0415
        for r in _injected:
            if r.mention.kind == _MK.FILE:
                chip = MentionChip(raw=r.mention.raw, kind="file",
                                   display_size=f"{r.chars_used/1024:.1f} KB", ok=r.ok, error=r.error)
            elif r.mention.kind == _MK.DIRECTORY:
                chip = MentionChip(raw=r.mention.raw, kind="dir", display_size="", ok=r.ok)
            elif r.mention.kind == _MK.GLOB:
                count = r.block.count("<file ")
                chip = MentionChip(raw=r.mention.raw, kind="glob",
                                   display_size=f"→ {count} file{'s' if count!=1 else ''}", ok=r.ok)
            elif r.mention.kind == _MK.URL:
                chip = MentionChip(raw=r.mention.raw, kind="url",
                                   display_size=f"{r.chars_used:,} chars" if r.ok else "", ok=r.ok)
            else:
                chip = MentionChip(raw=r.mention.raw, kind="unresolved",
                                   display_size="", ok=False, error="not found")
            transcript.add_mention_chips(agent_id, [chip])
            if r.block and r.ok:
                transcript.set_mention_content(agent_id, r.mention.raw, r.block)

    # ── skills auto-triggering ────────────────────────────────────────
    from agenthicc.skills.runner import find_matching_skills, process_skill_body  # noqa: PLC0415
    _matched_skills = find_matching_skills(text, getattr(renderer, "_skills", {}) or {})
    _skill_suffix = ""
    if _matched_skills:
        _addenda = "\n\n".join(
            f"## Skill: {s.name}\n{process_skill_body(s, args=[], cwd=Path(os.getcwd()))}"
            for s in _matched_skills
        )
        _skill_suffix = f"\n\n---\n\n{_addenda}"

    # Build the agent class with tools so it can actually DO things
    from agenthicc.plugins.registry import build_registry  # noqa: PLC0415
    _mcp_tools = []
    _mcp_reg = getattr(renderer, "_mcp_registry", None)
    if _mcp_reg is not None:
        _mcp_tools = _mcp_reg.all_tools()
    _registry = build_registry(
        agent_name=getattr(renderer, "_active_agent", None) or "default",
        project_plugin_tools=(getattr(renderer, "_project_plugin_tools", None) or []) + _mcp_tools,
    )
    _tool_description = _registry.describe()

    @agent_decorator(
        model=model_id,
        system=(
            "You are a capable AI assistant with access to filesystem, shell, "
            "and git tools. Use them directly to complete tasks. "
            "Give concise responses. Show command output when relevant. "
            "Never invent file contents — always read them first."
            + (_skill_suffix if _skill_suffix else "")
            + (f"\n\n{_tool_description}" if _tool_description else "")
        ),
    )
    @use_tools(*_registry.tools)
    class _AgenthiccAgent: ...

    # Use _build_runner_for_agent so tools are resolved correctly from @use_tools metadata
    _agent_instance = _AgenthiccAgent()
    _active_runner = _build_runner_for_agent(
        _agent_instance,
        runner._transport,
        signals=getattr(runner, "_signals", None),
    )

    # Per-turn text accumulation (run_stream yields chunks across all turns).
    _current_turn: list[str] = []
    _all_turn_texts: list[str] = []     # one entry per LLM turn that produced text
    content = ""

    try:
        from lauren_ai._config import AgentConfig as _AgentConfig  # noqa: PLC0415
        _cfg = _AgentConfig(
            max_turns=max_agent_turns,
            parallel_tool_calls=True,
        )
        _stream = await _active_runner.run_stream(
            _agent_instance,
            _agent_text,
            memory=session_memory,
            config_override=_cfg,
        )
        async for _chunk in _stream:
            # Accumulate text delta into the current turn buffer.
            if _chunk.delta:
                _current_turn.append(_chunk.delta)

            # A chunk with stop_reason marks the end of one LLM turn.
            if _chunk.stop_reason is not None:
                _turn_text = "".join(_current_turn).strip()
                if _turn_text:
                    _all_turn_texts.append(_turn_text)
                    # Add text to transcript NOW — before _stream_loop() executes
                    # tools for this turn.  ToolCallStarted/Complete signals fire
                    # after this yield, so the transcript order becomes:
                    #   turn-1 text → turn-1 tools → turn-2 text → turn-2 tools …
                    transcript.append_line(agent_id, "\x00md\x00" + _turn_text)
                    # Immediately print the completed turn text to the scroll buffer
                    # so it persists above the tool-call block.
                    renderer._flush_new_lines()
                _current_turn = []

        # Build final content from the last turn (replaces response.content).
        content = _all_turn_texts[-1] if _all_turn_texts else ""
        # Strip any residual XML that slipped through.
        content = _re.sub(r"<tool_call>.*?</tool_call>", "", content, flags=_re.DOTALL)
        content = _re.sub(r"<[^>]+>", "", content).strip()
        # If tools ran but the final turn produced no prose, that's fine — skip it.
        if not content and not _live_calls:
            content = "(no response)"

    except (asyncio.CancelledError, KeyboardInterrupt):
        # Ctrl+C pressed while the agent was running.  Clean exit — nothing to
        # append to the transcript; the input loop will resume normally.
        content = ""
        _all_turn_texts = []
    except Exception as exc:
        _all_turn_texts = []
        content = ""
        _err_line = f"[red bold]⚠ {type(exc).__name__}:[/red bold] [red]{exc}[/red]"
        transcript.append_line(agent_id, _err_line)
        renderer._flush_new_lines()
    finally:
        renderer._status.active = False
        renderer._status.completed_agents += 1
        # Notify Textual widgets that this agent turn has finished.
        _post_message_safe(renderer, AgentRunFinished())

    if content == "(no response)":
        transcript.append_line(agent_id, "\x00md\x00" + content)

    await processor.emit(
        Event.create("IntentStatusChanged", {"intent_id": intent_id, "status": "complete"})
    )
