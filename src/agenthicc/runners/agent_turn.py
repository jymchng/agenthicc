"""Single agent turn: LLM streaming → ConversationStore events."""
from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from typing import Any


def _fmt_args(args: dict) -> str:
    from rich.markup import escape as _e  # noqa: PLC0415
    items = list(args.items())
    if not items:
        return ""
    if len(items) == 1:
        return f"[dim]({_e(repr(items[0][1])[:60])})[/dim]"
    return "[dim](" + ", ".join(
        f"{_e(k)}={_e(repr(v)[:25])}" for k, v in items[:3]
    ) + ")[/dim]"


async def _run_agent_turn(
    text: str,
    runner: Any,
    transcript: Any,        # unused — kept for API compat; pass None
    renderer: Any,          # unused — kept for API compat; pass None
    processor: Any,
    session_memory: Any = None,
    max_agent_turns: int = 200,
    conv_store: Any = None,
    exec_cfg: Any = None,
    skills: Any = None,
    mention_cache: Any = None,
    project_plugin_tools: Any = None,
    mcp_registry: Any = None,
    active_agent: str | None = None,
    completed_turns: int = 0,
) -> None:
    """Run one agent turn, publishing events directly to *conv_store*."""
    from lauren_ai._agents import agent as agent_decorator, use_tools  # noqa: PLC0415
    from lauren_ai.testing import _build_runner_for_agent              # noqa: PLC0415
    from agenthicc.kernel import Event                                 # noqa: PLC0415

    if runner is None:
        if conv_store:
            conv_store.append_event("error", {
                "message": "⚠ No LLM configured. Set ANTHROPIC_API_KEY or OPENAI_API_KEY."
            })
        return

    model_id    = getattr(getattr(runner, "_transport", None), "_config", None)
    model_id    = getattr(model_id, "model", "unknown") if model_id else "unknown"
    model_short = model_id.split("/")[-1]

    intent_id = uuid.uuid4().hex
    await processor.emit(
        Event.create("IntentCreated", {"intent_id": intent_id, "raw_text": text})
    )
    agent_id = f"agent-{intent_id[:8]}"

    if conv_store:
        conv_store.begin_turn(f"assistant ({model_short})", agent_id)
        conv_store.append_event("turn_start", {
            "turn_id": agent_id,
            "agent_name": f"assistant ({model_short})",
        })

    # Local tool-args store — maps tool_use_id → args dict so we can format
    # the completed call line without a separate transcript model.
    _tool_args: dict[str, dict] = {}
    _tool_names: dict[str, str] = {}
    _turn_active = [True]
    _FILE_EDIT_TOOLS = {"write_file", "patch_file", "append_file"}

    _signals = getattr(runner, "_signals", None)
    _file_snapshots: dict[str, tuple[str, str]] = {}

    if _signals is not None:
        from lauren_ai._signals import ModelCallComplete as _MCC  # noqa: PLC0415
        from lauren_ai._signals import ToolCallStarted as _TCS, ToolCallComplete as _TCC  # noqa: PLC0415

        @_signals.on(_MCC)
        async def _on_model_complete(sig: Any) -> None:
            if not _turn_active[0]:
                return
            usage = getattr(sig, "usage", None)
            inp   = getattr(usage, "input_tokens", 0) if usage else 0
            out   = getattr(usage, "output_tokens", 0) if usage else 0
            cost  = getattr(sig, "cost_usd", 0.0) or 0.0
            if conv_store:
                conv_store.add_tokens(inp, out, cost)

        @_signals.on(_TCS)
        async def _on_tool_started(sig: Any) -> None:
            if not _turn_active[0]:
                return
            args = dict(getattr(sig, "input", {}) or {})
            name = getattr(sig, "tool_name", "")
            tid  = getattr(sig, "tool_use_id", "")
            _tool_names[tid] = name
            _tool_args[tid]  = args
            if conv_store:
                conv_store.set_tool(name)
            if name in _FILE_EDIT_TOOLS and args.get("path"):
                rel_path = args["path"]
                full = os.path.join(os.getcwd(), rel_path) if not os.path.isabs(rel_path) else rel_path
                try:
                    original = await asyncio.to_thread(
                        lambda p=full: open(p).read() if os.path.exists(p) else ""
                    )
                    _file_snapshots[tid] = (rel_path, original)
                except Exception:  # noqa: BLE001
                    pass

        @_signals.on(_TCC)
        async def _on_tool_complete(sig: Any) -> None:
            if not _turn_active[0]:
                return
            import difflib as _dl  # noqa: PLC0415
            tid     = getattr(sig, "tool_use_id", "")
            success = bool(getattr(sig, "success", True))
            ms      = getattr(sig, "duration_ms", None)
            name    = _tool_names.pop(tid, tid)
            args    = _tool_args.pop(tid, {})
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
                        fromfile=f"a/{rel_path}", tofile=f"b/{rel_path}", lineterm="",
                    )) or None
                    if diff and conv_store:
                        conv_store.append_event("file_modified", {"path": rel_path})
                except Exception:  # noqa: BLE001
                    pass

            if conv_store:
                conv_store.clear_tool()
                conv_store.append_event("tool_complete", {
                    "tool_use_id":  tid,
                    "name":         name,
                    "success":      success,
                    "args_str":     _fmt_args(args),
                    "dur_str":      f"  [dim]{ms:.0f}ms[/dim]" if ms else "",
                    "output_lines": [],
                })

    # ── @mention injection ────────────────────────────────────────────────────
    from agenthicc.mentions.injector import build_context_prefix, InjectionConfig  # noqa: PLC0415
    _mention_cfg = InjectionConfig(
        mention_token_budget=getattr(exec_cfg, "mention_token_budget", 32_000),
        max_file_chars=getattr(exec_cfg, "mention_max_file_chars", 16_000),
        max_glob_files=getattr(exec_cfg, "mention_max_glob_files", 20),
        cwd=Path(os.getcwd()),
    )
    _mention_prefix, _injected = await build_context_prefix(
        text,
        cwd=_mention_cfg.cwd,
        cfg=_mention_cfg,
        cache=mention_cache,
        current_turn=completed_turns,
    )
    _agent_text = _mention_prefix + text if _mention_prefix else text

    if _injected and conv_store:
        chips = []
        for r in _injected:
            chips.append({
                "raw":             r.mention.raw,
                "content_preview": (r.block or "")[:80] if r.ok else "",
            })
        if chips:
            conv_store.append_event("mention_chips", {"chips": chips})

    # ── skills ────────────────────────────────────────────────────────────────
    from agenthicc.skills.runner import find_matching_skills, process_skill_body  # noqa: PLC0415
    _matched_skills = find_matching_skills(text, skills or {})
    _skill_suffix   = ""
    if _matched_skills:
        _skill_suffix = "\n\n---\n\n" + "\n\n".join(
            f"## Skill: {s.name}\n{process_skill_body(s, args=[], cwd=Path(os.getcwd()))}"
            for s in _matched_skills
        )

    # ── build agent ───────────────────────────────────────────────────────────
    from agenthicc.plugins.registry import build_registry  # noqa: PLC0415
    _mcp_tools = []
    if mcp_registry is not None:
        _mcp_tools = mcp_registry.all_tools()
    _registry = build_registry(
        agent_name=active_agent or "default",
        project_plugin_tools=(project_plugin_tools or []) + _mcp_tools,
    )

    @agent_decorator(
        model=model_id,
        system=(
            "You are a capable AI assistant with access to filesystem, shell, "
            "and git tools. Use them directly to complete tasks. "
            "Give concise responses. Show command output when relevant. "
            "Never invent file contents — always read them first."
            + (_skill_suffix or "")
            + (f"\n\n{_registry.describe()}" if _registry.describe() else "")
        ),
    )
    @use_tools(*_registry.tools)
    class _AgenthiccAgent: ...

    _agent_instance = _AgenthiccAgent()
    _active_runner  = _build_runner_for_agent(
        _agent_instance, runner._transport,
        signals=getattr(runner, "_signals", None),
    )

    # ── streaming loop ────────────────────────────────────────────────────────
    _current_turn: list[str] = []
    _all_turn_texts: list[str] = []

    try:
        from lauren_ai._config import AgentConfig as _AgentConfig  # noqa: PLC0415
        _stream = await _active_runner.run_stream(
            _agent_instance, _agent_text,
            memory=session_memory,
            config_override=_AgentConfig(
                max_turns=max_agent_turns, parallel_tool_calls=True
            ),
        )
        async for _chunk in _stream:
            if _chunk.delta:
                _current_turn.append(_chunk.delta)

            if _chunk.stop_reason is not None:
                _turn_text = "".join(_current_turn).strip()
                _current_turn = []
                if _turn_text:
                    _all_turn_texts.append(_turn_text)
                    if conv_store:
                        conv_store.append_event("text", {"text": _turn_text})

    except (asyncio.CancelledError, KeyboardInterrupt):
        _all_turn_texts = []
        if conv_store:
            conv_store.end_turn()
    except Exception as exc:
        _all_turn_texts = []
        if conv_store:
            conv_store.append_event("error", {
                "message": f"{type(exc).__name__}: {exc}"
            })
            conv_store.fail_turn(str(exc))
    finally:
        _turn_active[0] = False
        if conv_store:
            conv_store.end_turn()

    await processor.emit(
        Event.create("IntentStatusChanged",
                     {"intent_id": intent_id, "status": "complete"})
    )
