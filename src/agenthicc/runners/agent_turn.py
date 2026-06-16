"""AgentTurnRunner — executes one agent turn (PRD-92).

``AgentTurnContext`` (see agent_turn_context.py) carries all configuration.
``AgentTurnRunner`` executes it via composable private methods, each
independently testable.

``_run_agent_turn`` is kept as a thin compatibility shim so all existing
call sites continue to work without modification.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agenthicc.runners.agent_turn_context import AgentTurnContext

if TYPE_CHECKING:
    pass   # runtime imports are deferred below to match existing pattern


# ── formatting helper (module-level, unchanged) ───────────────────────────────

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


# ── AgentTurnRunner ───────────────────────────────────────────────────────────

class AgentTurnRunner:
    """Executes a single agent turn described by an ``AgentTurnContext``.

    Each private method handles one concern; they can be tested with a
    mock context independently of the others.
    """

    _FILE_EDIT_TOOLS = frozenset({"write_file", "patch_file", "append_file"})

    def __init__(self, ctx: AgentTurnContext) -> None:
        self._ctx = ctx

        # Mutable state shared between methods and signal-handler closures.
        self._intent_id:      str  = ""
        self._agent_id:       str  = ""
        self._model_id:       str  = ""
        self._model_short:    str  = ""
        self._turn_active:    bool = True

        # Tool tracking — populated by signal handlers.
        self._tool_args:      dict[str, dict]           = {}
        self._tool_names:     dict[str, str]            = {}
        self._file_snapshots: dict[str, tuple[str, str]] = {}

        # Content produced during the turn.
        self._skill_suffix: str = ""

    # ── public entry point ────────────────────────────────────────────────────

    async def run(self) -> None:
        """Execute the full agent turn end-to-end."""
        ctx = self._ctx
        if ctx.runner is None:
            if ctx.conv_store:
                ctx.conv_store.append_event("error", {
                    "message": "⚠ No LLM configured. Set ANTHROPIC_API_KEY or OPENAI_API_KEY."
                })
            return

        self._resolve_model()
        await self._emit_intent_created()
        self._begin_conv_turn()
        self._register_signal_handlers()

        agent_text     = await self._inject_mentions()
        self._inject_skills()

        agent_instance, active_runner = self._build_agent()
        await self._stream(agent_instance, agent_text, active_runner)

        await self._emit_intent_complete()

    # ── step 1: model resolution ──────────────────────────────────────────────

    def _resolve_model(self) -> None:
        transport  = getattr(self._ctx.runner, "_transport", None)
        cfg        = getattr(transport, "_config", None)
        self._model_id    = getattr(cfg, "model", "unknown") if cfg else "unknown"
        self._model_short = self._model_id.split("/")[-1]

    # ── step 2: kernel event ──────────────────────────────────────────────────

    async def _emit_intent_created(self) -> None:
        from agenthicc.kernel import Event  # noqa: PLC0415
        self._intent_id = uuid.uuid4().hex
        self._agent_id  = f"agent-{self._intent_id[:8]}"
        await self._ctx.processor.emit(
            Event.create("IntentCreated", {
                "intent_id": self._intent_id,
                "raw_text":  self._ctx.text,
            })
        )

    # ── step 3: conversation turn lifecycle ───────────────────────────────────

    def _begin_conv_turn(self) -> None:
        conv = self._ctx.conv_store
        if conv:
            conv.begin_turn(f"assistant ({self._model_short})", self._agent_id)
            conv.append_event("turn_start", {
                "turn_id":    self._agent_id,
                "agent_name": f"assistant ({self._model_short})",
            })

    # ── step 4: signal handlers ───────────────────────────────────────────────

    def _register_signal_handlers(self) -> None:
        """Register ToolCallStarted / ToolCallComplete handlers on the signal bus."""
        signals = getattr(self._ctx.runner, "_signals", None)
        if signals is None:
            return

        from lauren_ai._signals import (  # noqa: PLC0415
            ToolCallStarted as _TCS,
            ToolCallComplete as _TCC,
        )

        @signals.on(_TCS)
        async def _on_tool_started(sig: Any) -> None:
            if not self._turn_active:
                return
            args = dict(getattr(sig, "input", {}) or {})
            name = getattr(sig, "tool_name", "")
            tid  = getattr(sig, "tool_use_id", "")
            self._tool_names[tid] = name
            self._tool_args[tid]  = args
            if self._ctx.conv_store:
                self._ctx.conv_store.set_tool(name)
            if name in self._FILE_EDIT_TOOLS and args.get("path"):
                await self._snapshot_file(tid, args["path"])

        @signals.on(_TCC)
        async def _on_tool_complete(sig: Any) -> None:
            if not self._turn_active:
                return
            await self._handle_tool_complete(sig)

    async def _snapshot_file(self, tid: str, rel_path: str) -> None:
        full = (
            os.path.join(os.getcwd(), rel_path)
            if not os.path.isabs(rel_path)
            else rel_path
        )
        try:
            original = await asyncio.to_thread(
                lambda p=full: open(p).read() if os.path.exists(p) else ""
            )
            self._file_snapshots[tid] = (rel_path, original)
        except Exception:  # noqa: BLE001
            pass

    async def _handle_tool_complete(self, sig: Any) -> None:
        import difflib as _dl  # noqa: PLC0415
        tid     = getattr(sig, "tool_use_id", "")
        success = bool(getattr(sig, "success", True))
        ms      = getattr(sig, "duration_ms", None)
        name    = self._tool_names.pop(tid, tid)
        args    = self._tool_args.pop(tid, {})
        conv    = self._ctx.conv_store

        if tid in self._file_snapshots:
            rel_path, original = self._file_snapshots.pop(tid)
            full = (
                os.path.join(os.getcwd(), rel_path)
                if not os.path.isabs(rel_path)
                else rel_path
            )
            try:
                new_content = await asyncio.to_thread(
                    lambda p=full: open(p).read() if os.path.exists(p) else ""
                )
                diff = "".join(_dl.unified_diff(
                    original.splitlines(keepends=True),
                    new_content.splitlines(keepends=True),
                    fromfile=f"a/{rel_path}", tofile=f"b/{rel_path}", lineterm="",
                )) or None
                if diff and conv:
                    conv.append_event("file_modified", {"path": rel_path})
            except Exception:  # noqa: BLE001
                pass

        if conv:
            conv.clear_tool(success=success)
            conv.append_event("tool_complete", {
                "tool_use_id":  tid,
                "name":         name,
                "success":      success,
                "args_str":     _fmt_args(args),
                "dur_str":      f"  [dim]{ms:.0f}ms[/dim]" if ms else "",
                "output_lines": [],
            })

    # ── step 5: @mention injection ────────────────────────────────────────────

    async def _inject_mentions(self) -> str:
        """Resolve @mentions and emit mention chips. Returns agent_text."""
        from agenthicc.mentions.injector import (  # noqa: PLC0415
            build_context_prefix, InjectionConfig,
        )
        ctx = self._ctx
        mention_cfg = InjectionConfig(
            mention_token_budget=getattr(ctx.exec_cfg, "mention_token_budget", 32_000),
            max_file_chars=getattr(ctx.exec_cfg, "mention_max_file_chars", 16_000),
            max_glob_files=getattr(ctx.exec_cfg, "mention_max_glob_files", 20),
            cwd=Path(os.getcwd()),
        )
        prefix, injected = await build_context_prefix(
            ctx.text,
            cwd=mention_cfg.cwd,
            cfg=mention_cfg,
            cache=ctx.mention_cache,
            current_turn=ctx.completed_turns,
        )
        agent_text = prefix + ctx.text if prefix else ctx.text
        if injected and ctx.conv_store:
            chips = [
                {
                    "raw":             r.mention.raw,
                    "content_preview": (r.block or "")[:80] if r.ok else "",
                }
                for r in injected
            ]
            if chips:
                ctx.conv_store.append_event("mention_chips", {"chips": chips})
        return agent_text

    # ── step 6: skills ────────────────────────────────────────────────────────

    def _inject_skills(self) -> None:
        """Find matching skills and build self._skill_suffix."""
        from agenthicc.skills.runner import (  # noqa: PLC0415
            find_matching_skills, process_skill_body,
        )
        ctx = self._ctx
        matched = find_matching_skills(ctx.text, ctx.skills or {})
        if matched:
            self._skill_suffix = "\n\n---\n\n" + "\n\n".join(
                f"## Skill: {s.name}\n"
                f"{process_skill_body(s, args=[], cwd=Path(os.getcwd()))}"
                for s in matched
            )

    # ── step 7: build @agent class and runner ─────────────────────────────────

    def _build_agent(self) -> tuple[Any, Any]:
        """Construct the @agent-decorated class, populate meta.tools, build runner.

        Returns (agent_instance, active_runner).
        """
        from lauren_ai._agents import agent as agent_decorator, use_tools  # noqa: PLC0415
        from lauren_ai.testing import _build_runner_for_agent              # noqa: PLC0415
        from lauren_ai._agents._runner import AgentRunnerBase as _RunnerBase  # noqa: PLC0415
        from agenthicc.plugins.registry import build_registry              # noqa: PLC0415
        from agenthicc.agents.plugin import BASE_SYSTEM_PROMPT as _BASE   # noqa: PLC0415

        ctx = self._ctx

        # Tool registry
        mcp_tools = ctx.mcp_registry.all_tools() if ctx.mcp_registry is not None else []
        registry  = build_registry(
            agent_name=ctx.active_agent or "default",
            project_plugin_tools=(ctx.project_plugin_tools or []) + mcp_tools,
        )

        # System prompt
        cfg_base       = (getattr(ctx.exec_cfg, "base_system_prompt", None) or "")
        effective_base = cfg_base or _BASE
        system = (
            effective_base
            + (f"\n\n{ctx.system_prompt_suffix}" if ctx.system_prompt_suffix else "")
            + (self._skill_suffix or "")
            + (f"\n\n{registry.describe()}" if registry.describe() else "")
        )

        @agent_decorator(model=self._model_id, system=system)
        @use_tools(*registry.tools)
        class _AgenthiccAgent: ...

        agent_instance = _AgenthiccAgent()
        # Populate meta.tools from tool_classes (side-effect on class).
        _build_runner_for_agent(
            agent_instance,
            ctx.runner._transport,
            signals=getattr(ctx.runner, "_signals", None),
        )

        # Global hooks
        hooks: list = []
        if ctx.app_state is not None:
            from agenthicc.tools.capability_gate import ToolCapabilityGate  # noqa: PLC0415
            hooks.append(ToolCapabilityGate(ctx.app_state))
            if ctx.approval_svc is not None:
                from agenthicc.tools.approval import ApprovalGate           # noqa: PLC0415
                hooks.append(ApprovalGate(ctx.app_state, ctx.approval_svc))

        active_runner = _RunnerBase(
            transport=ctx.runner._transport,
            signals=getattr(ctx.runner, "_signals", None),
            global_hooks=hooks or None,
        )
        return agent_instance, active_runner

    # ── step 8: streaming loop ────────────────────────────────────────────────

    async def _stream(
        self,
        agent_instance: Any,
        agent_text: str,
        active_runner: Any,
    ) -> None:
        from lauren_ai._config import AgentConfig as _AgentConfig  # noqa: PLC0415
        ctx           = self._ctx
        current_turn: list[str] = []

        try:
            stream = await active_runner.run_stream(
                agent_instance, agent_text,
                memory=ctx.session_memory,
                config_override=_AgentConfig(
                    max_turns=ctx.max_agent_turns, parallel_tool_calls=True
                ),
            )
            async for chunk in stream:
                if chunk.delta:
                    current_turn.append(chunk.delta)
                    if ctx.output_collector is not None:
                        ctx.output_collector.append(chunk.delta)

                # Live token update — PRD-83.
                if chunk.usage is not None and ctx.conv_store:
                    u   = chunk.usage
                    cst = (
                        u.cost_usd(self._model_id)
                        if callable(getattr(u, "cost_usd", None))
                        else 0.0
                    )
                    ctx.conv_store.add_tokens(u.input_tokens, u.output_tokens, cst)

                if chunk.stop_reason is not None:
                    turn_text = "".join(current_turn).strip()
                    current_turn = []
                    if turn_text and ctx.conv_store:
                        ctx.conv_store.append_event("text", {"text": turn_text})

        except (asyncio.CancelledError, KeyboardInterrupt):
            if ctx.conv_store:
                ctx.conv_store.end_turn()
        except Exception as exc:
            if ctx.conv_store:
                ctx.conv_store.append_event("error", {
                    "message": f"{type(exc).__name__}: {exc}"
                })
                ctx.conv_store.fail_turn(str(exc))
        finally:
            self._turn_active = False
            if ctx.conv_store:
                ctx.conv_store.end_turn()

    # ── step 9: kernel completion event ───────────────────────────────────────

    async def _emit_intent_complete(self) -> None:
        from agenthicc.kernel import Event  # noqa: PLC0415
        await self._ctx.processor.emit(
            Event.create("IntentStatusChanged", {
                "intent_id": self._intent_id,
                "status":    "complete",
            })
        )


# ── compatibility shim ────────────────────────────────────────────────────────

async def _run_agent_turn(
    text: str,
    runner: Any,
    processor: Any,
    session_memory: Any = None,
    max_agent_turns: int = 200,
    conv_store: Any = None,
    app_state: Any = None,
    exec_cfg: Any = None,
    skills: Any = None,
    mention_cache: Any = None,
    project_plugin_tools: Any = None,
    mcp_registry: Any = None,
    active_agent: str | None = None,
    completed_turns: int = 0,
    approval_svc: Any = None,
    output_collector: list[str] | None = None,
    system_prompt_suffix: str = "",
) -> None:
    """Thin shim — constructs AgentTurnContext and delegates to AgentTurnRunner.

    All existing call sites continue to work without modification.
    """
    ctx = AgentTurnContext(
        text=text,
        runner=runner,
        processor=processor,
        session_memory=session_memory,
        max_agent_turns=max_agent_turns,
        conv_store=conv_store,
        app_state=app_state,
        exec_cfg=exec_cfg,
        skills=skills,
        mention_cache=mention_cache,
        project_plugin_tools=project_plugin_tools,
        mcp_registry=mcp_registry,
        active_agent=active_agent,
        completed_turns=completed_turns,
        approval_svc=approval_svc,
        output_collector=output_collector,
        system_prompt_suffix=system_prompt_suffix,
    )
    await AgentTurnRunner(ctx).run()
