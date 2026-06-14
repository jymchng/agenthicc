"""Single agent turn: LLM streaming, tool signals, transcript wiring.

Renderer must be AgenthiccTUI.  All tool/token events are published to
renderer.bus so the LivePanel and SpinnerState update reactively.
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid
from pathlib import Path
from typing import Any


def _require_tui(renderer: Any) -> Any:
    """Return renderer if it is AgenthiccTUI, otherwise raise TypeError."""
    from agenthicc.tui.tui import AgenthiccTUI  # noqa: PLC0415
    if not isinstance(renderer, AgenthiccTUI):
        raise TypeError(
            f"renderer must be AgenthiccTUI, got {type(renderer).__name__}. "
            "Remove legacy InlineRenderer usage from tui_session.py."
        )
    return renderer


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
    """Run one agent turn against AgenthiccTUI."""
    import re as _re  # noqa: PLC0415
    from lauren_ai._agents import agent as agent_decorator, use_tools  # noqa: PLC0415
    from lauren_ai.testing import _build_runner_for_agent  # noqa: PLC0415
    from agenthicc.kernel import Event  # noqa: PLC0415
    from agenthicc.tui.tui_events import (  # noqa: PLC0415
        AssistantStartEvent,
        AssistantChunkEvent,
        AssistantCompleteEvent,
        ToolStartEvent,
        ToolCompleteEvent,
        TokenUpdateEvent,
        FileModifiedEvent,
        ErrorEvent,
    )

    tui = _require_tui(renderer)

    if runner is None:
        transcript.append_turn("system", "system", time.monotonic())
        transcript.append_line("system", "⚠ No LLM configured. Set ANTHROPIC_API_KEY or OPENAI_API_KEY.")
        return

    model_id = (
        runner._transport._config.model
        if hasattr(runner._transport, "_config")
        else "unknown"
    )
    model_short = model_id.split("/")[-1]

    intent_id = uuid.uuid4().hex
    await processor.emit(Event.create("IntentCreated", {"intent_id": intent_id, "raw_text": text}))

    agent_id = f"agent-{intent_id[:8]}"
    transcript.append_turn(agent_id, f"assistant ({model_short})", time.monotonic())

    cell = getattr(getattr(runner, "_signals", None), "_current_agent_cell", None)
    if cell is not None:
        cell[0] = agent_id

    tui.bus.publish(AssistantStartEvent(agent_id=agent_id, model_short=model_short))

    _signals = getattr(runner, "_signals", None)
    _file_snapshots: dict[str, tuple[str, str]] = {}
    _tool_names: dict[str, str] = {}
    _FILE_EDIT_TOOLS = {"write_file", "patch_file", "append_file"}

    if _signals is not None:
        from lauren_ai._signals import ModelCallComplete as _MCC  # noqa: PLC0415
        from lauren_ai._signals import ToolCallStarted as _TCS, ToolCallComplete as _TCC  # noqa: PLC0415

        @_signals.on(_MCC)
        async def _on_model_complete(sig: Any) -> None:
            usage = getattr(sig, "usage", None)
            inp = getattr(usage, "input_tokens", 0) if usage else 0
            out = getattr(usage, "output_tokens", 0) if usage else 0
            cost = getattr(sig, "cost_usd", 0.0) or 0.0
            renderer._status.input_tokens += inp
            renderer._status.output_tokens += out
            renderer._status.session_cost_usd += cost
            tui.bus.publish(TokenUpdateEvent(input_tokens=inp, output_tokens=out, cost_usd=cost))

        @_signals.on(_TCS)
        async def _on_tool_started(sig: Any) -> None:
            args = dict(getattr(sig, "input", {}) or {})
            name = getattr(sig, "tool_name", "")
            tid = getattr(sig, "tool_use_id", "")
            _tool_names[tid] = name
            tui.bus.publish(ToolStartEvent(tool_use_id=tid, name=name, args=args))
            if name in _FILE_EDIT_TOOLS:
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
            success = bool(getattr(sig, "success", True))
            ms = getattr(sig, "duration_ms", None)
            name = _tool_names.pop(tid, tid)
            diff: str | None = None
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
                        tui.bus.publish(FileModifiedEvent(path=rel_path))
                except Exception:
                    pass
            tui.bus.publish(ToolCompleteEvent(
                tool_use_id=tid, name=name, success=success, duration_ms=ms, diff=diff
            ))

    # ── @mention injection ────────────────────────────────────────────────────
    from agenthicc.mentions.injector import build_context_prefix, InjectionConfig  # noqa: PLC0415
    from agenthicc.tui.transcript import MentionChip  # noqa: PLC0415
    _exec_cfg = getattr(renderer, "_exec_cfg", None)
    _mention_cfg = InjectionConfig(
        mention_token_budget=getattr(_exec_cfg, "mention_token_budget", 32_000),
        max_file_chars=getattr(_exec_cfg, "mention_max_file_chars", 16_000),
        max_glob_files=getattr(_exec_cfg, "mention_max_glob_files", 20),
        cwd=Path(os.getcwd()),
    )
    _mention_prefix, _injected = await build_context_prefix(
        text,
        cwd=_mention_cfg.cwd,
        cfg=_mention_cfg,
        cache=getattr(renderer, "_mention_cache", None),
        current_turn=renderer._status.completed_agents,
    )
    _agent_text = _mention_prefix + text if _mention_prefix else text

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

    # ── skills ────────────────────────────────────────────────────────────────
    from agenthicc.skills.runner import find_matching_skills, process_skill_body  # noqa: PLC0415
    _matched_skills = find_matching_skills(text, getattr(renderer, "_skills", {}) or {})
    _skill_suffix = ""
    if _matched_skills:
        _skill_suffix = "\n\n---\n\n" + "\n\n".join(
            f"## Skill: {s.name}\n{process_skill_body(s, args=[], cwd=Path(os.getcwd()))}"
            for s in _matched_skills
        )

    # ── build agent with tools ────────────────────────────────────────────────
    from agenthicc.plugins.registry import build_registry  # noqa: PLC0415
    _mcp_tools = []
    _mcp_reg = getattr(renderer, "_mcp_registry", None)
    if _mcp_reg is not None:
        _mcp_tools = _mcp_reg.all_tools()
    _registry = build_registry(
        agent_name=getattr(renderer, "_active_agent", None) or "default",
        project_plugin_tools=(getattr(renderer, "_project_plugin_tools", None) or []) + _mcp_tools,
    )

    @agent_decorator(
        model=model_id,
        system=(
            "You are a capable AI assistant with access to filesystem, shell, "
            "and git tools. Use them directly to complete tasks. "
            "Give concise responses. Show command output when relevant. "
            "Never invent file contents — always read them first."
            + (_skill_suffix if _skill_suffix else "")
            + (f"\n\n{_registry.describe()}" if _registry.describe() else "")
        ),
    )
    @use_tools(*_registry.tools)
    class _AgenthiccAgent: ...

    _agent_instance = _AgenthiccAgent()
    _active_runner = _build_runner_for_agent(
        _agent_instance,
        runner._transport,
        signals=getattr(runner, "_signals", None),
    )

    # ── streaming loop ────────────────────────────────────────────────────────
    _current_turn: list[str] = []
    _all_turn_texts: list[str] = []
    content = ""

    try:
        from lauren_ai._config import AgentConfig as _AgentConfig  # noqa: PLC0415
        _stream = await _active_runner.run_stream(
            _agent_instance,
            _agent_text,
            memory=session_memory,
            config_override=_AgentConfig(max_turns=max_agent_turns, parallel_tool_calls=True),
        )
        async for _chunk in _stream:
            if _chunk.delta:
                _current_turn.append(_chunk.delta)
                tui.bus.publish(AssistantChunkEvent(
                    agent_id=agent_id, chunk="".join(_current_turn)
                ))

            if _chunk.stop_reason is not None:
                _turn_text = "".join(_current_turn).strip()
                _current_turn = []

                if _turn_text:
                    _all_turn_texts.append(_turn_text)
                    transcript.append_line(agent_id, "\x00md\x00" + _turn_text)

                # Always publish so the spinner clears its streaming-text preview.
                # The live spinner also clears its tool-call list here so the next
                # batch of calls renders fresh — but the scroll buffer keeps everything.
                tui.bus.publish(AssistantCompleteEvent(agent_id=agent_id))

        content = _all_turn_texts[-1] if _all_turn_texts else ""
        content = _re.sub(r"<tool_call>.*?</tool_call>", "", content, flags=_re.DOTALL)
        content = _re.sub(r"<[^>]+>", "", content).strip()
        if not content:
            content = "(no response)"

    except (asyncio.CancelledError, KeyboardInterrupt):
        content = ""
        _all_turn_texts = []
    except Exception as exc:
        _all_turn_texts = []
        content = ""
        tui.bus.publish(ErrorEvent(message=f"{type(exc).__name__}: {exc}"))
    finally:
        renderer._status.active = False
        renderer._status.completed_agents += 1

    if content == "(no response)":
        transcript.append_line(agent_id, "\x00md\x00" + content)

    await processor.emit(
        Event.create("IntentStatusChanged", {"intent_id": intent_id, "status": "complete"})
    )
