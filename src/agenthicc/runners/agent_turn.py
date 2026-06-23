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
from typing import TYPE_CHECKING

from agenthicc.runners.agent_turn_context import AgentTurnContext


# ── Permanent-error detection (PRD-117) ───────────────────────────────────────

def _http_status_code(exc: BaseException) -> int | None:
    """Return the HTTP status code carried by *exc*, or ``None``.

    Checks the exception itself and then its chained ``__cause__`` /
    ``__context__`` — necessary for SDK wrappers like ``TransportError``
    that wrap an inner ``BadRequestError`` which holds the real status.
    """
    for candidate in (exc, getattr(exc, "__cause__", None), getattr(exc, "__context__", None)):
        if candidate is None:
            continue
        code = getattr(candidate, "status_code", None)
        if isinstance(code, int):
            return code
    return None


def _is_transient_network_error(exc: BaseException) -> bool:
    """Return ``True`` for transient network errors that are safe to retry.

    Checks for :class:`~lauren_ai._exceptions.TransientTransportError` and
    common timeout / connection error type names anywhere in the exception
    chain (``__cause__`` and ``__context__``).  These errors are retriable
    with a memory-snapshot rollback (PRD-126).

    :param exc: The exception raised by the LLM transport or SDK.
    :return: ``True`` when the error is a retriable network-level failure.
    :rtype: bool
    """
    # Library-specific timeout / connection error type names.  We deliberately
    # do NOT match the bare builtin ``TimeoutError`` because in Python 3.11+
    # ``asyncio.TimeoutError is TimeoutError`` — matching it would retry
    # ``asyncio.wait_for`` timeouts (e.g. a tool's own watchdog), masking
    # programming errors.  Genuine network timeouts surface under the
    # httpx / anthropic names below.
    _TRANSIENT_NAMES = frozenset({
        # httpx
        "ReadTimeout", "ConnectTimeout", "WriteTimeout", "PoolTimeout",
        "ConnectError", "ReadError", "WriteError", "RemoteProtocolError",
        # anthropic / openai SDK
        "APITimeoutError", "APIConnectionError",
        # generic
        "NetworkError", "RemoteDisconnected",
    })
    try:
        from lauren_ai._exceptions import TransientTransportError  # noqa: PLC0415
        if isinstance(exc, TransientTransportError):
            return True
    except ImportError:
        pass
    for candidate in (exc, getattr(exc, "__cause__", None), getattr(exc, "__context__", None)):
        if candidate is None:
            continue
        if type(candidate).__name__ in _TRANSIENT_NAMES:
            return True
    return False


def _is_permanent_error(exc: BaseException) -> bool:
    """Return ``True`` for errors that will *never* succeed on retry.

    HTTP 4xx responses (except 429 rate-limit) are structurally permanent:
    the same request will always be rejected regardless of how many times
    it is retried.  HTTP 5xx, network timeouts, and connection errors are
    transient and worth retrying.

    Parameters
    ----------
    exc:
        The exception raised by the LLM transport or SDK.

    Returns
    -------
    bool
        ``True``  → exit the phase immediately, do not retry.
        ``False`` → swallow and let the phase loop decide.
    """
    # PRD-135: a context overflow that survived the proactive compaction ladder
    # AND the hard truncation guard is irreducible — retrying the identical
    # request always fails.  Treat as permanent so the phase surfaces the
    # actionable message and exits instead of looping on the same request.
    from lauren_ai import AgentContextOverflowError  # noqa: PLC0415

    if isinstance(exc, AgentContextOverflowError):
        return True
    status = _http_status_code(exc)
    if status is None:
        return False
    # 429 is rate-limited — transient, worth waiting and retrying.
    # All other 4xx are client errors (bad model name, bad API key, …) — permanent.
    return 400 <= status < 500 and status != 429

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from lauren_ai._agents._runner import AgentRunnerBase
    from lauren_ai._memory import ShortTermMemory
    from lauren_ai._signals import ToolCallStarted, ToolCallComplete
    from agenthicc.config import ExecutionSettings
    from agenthicc.kernel.processor import EventProcessor
    from agenthicc.memory.router import MemoryRouter
    from agenthicc.memory.vector import SemanticIndex
    from agenthicc.mentions.cache import MentionCache
    from agenthicc.plugins.registry import PluginTool
    from agenthicc.skills.loader import SkillDef
    from agenthicc.tools.approval import ApprovalGate, ApprovalService
    from agenthicc.tools.capability_gate import ToolCapabilityGate
    from agenthicc.tools.mcp import McpToolRegistry
    from agenthicc.tui.conversation_store import AppState, ConversationStore


# ── formatting helper (module-level, unchanged) ───────────────────────────────

def _fmt_args(args: dict[str, object]) -> str:
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
        self._tool_args:      dict[str, dict[str, object]]  = {}
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

        # Always emit IntentStatusChanged — success or failure — so the kernel
        # intent never stays permanently at "pending" after an exception.
        _intent_status = "complete"
        try:
            await self._stream(agent_instance, agent_text, active_runner)
        except (asyncio.CancelledError, KeyboardInterrupt):
            _intent_status = "failed"
            raise
        except Exception:
            _intent_status = "failed"
            raise
        finally:
            await self._emit_intent_complete(status=_intent_status)

    # ── step 1: model resolution ──────────────────────────────────────────────

    def _resolve_model(self) -> None:
        ctx = self._ctx
        # PRD-115: exec_cfg.model carries per-phase overrides from WorkflowParams
        # (PRD-111) and CodePlanRunner class attributes / _run_turn(model_override).
        # Use it when non-empty; fall back to the transport's baked-in config.
        override = getattr(ctx.exec_cfg, "model", "") if ctx.exec_cfg else ""
        if override:
            self._model_id = override
        else:
            transport = getattr(ctx.runner, "_transport", None)
            cfg       = getattr(transport, "_config", None)
            self._model_id = getattr(cfg, "model", "unknown") if cfg else "unknown"
        self._model_short = self._model_id.split("/")[-1]

    # ── step 2: kernel event ──────────────────────────────────────────────────

    async def _emit_intent_created(self) -> None:
        from agenthicc.kernel import Event  # noqa: PLC0415
        # PRD-129 Phase 3: a resumed turn reuses its original id so the durable
        # tool ledger and journal turn markers line up.
        self._intent_id = self._ctx.resume_turn_id or uuid.uuid4().hex
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
        async def _on_tool_started(sig: ToolCallStarted) -> None:
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
        async def _on_tool_complete(sig: ToolCallComplete) -> None:
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

    async def _handle_tool_complete(self, sig: ToolCallComplete) -> None:
        tid:     str        = getattr(sig, "tool_use_id", "")
        success: bool       = bool(getattr(sig, "success", True))
        ms:      float | None = getattr(sig, "duration_ms", None)
        name    = self._tool_names.pop(tid, tid)
        args    = self._tool_args.pop(tid, {})
        conv    = self._ctx.conv_store

        showed_diff = False
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
                old_lines = original.splitlines()
                new_lines = new_content.splitlines()
                changed   = old_lines != new_lines
                if changed and conv:
                    conv.append_event("file_modified", {
                        "path":      rel_path,
                        "old_lines": old_lines,
                        "new_lines": new_lines,
                        "tool":      name,
                    })
                    showed_diff = True
            except Exception:  # noqa: BLE001
                pass

        if conv:
            conv.clear_tool(success=success)
            # Skip the generic tool_complete line when a file-diff was already
            # rendered — the diff is more informative and the duplicate line
            # ("⎿ write_file(...)  ✓  4ms") is visual noise below the diff.
            if not showed_diff:
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
                    "raw":  r.mention.raw,
                    "kind": r.mention.kind.value,   # "file" | "directory" | "url" | "glob" | "unresolved"
                    "ok":   getattr(r, "ok", True),
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

    def _build_agent(self) -> tuple[object, AgentRunnerBase]:
        """Construct the @agent-decorated class, populate meta.tools, build runner.

        Returns (agent_instance, active_runner).
        """
        from lauren_ai._agents import agent as agent_decorator, use_tools  # noqa: PLC0415
        from lauren_ai._agents._runner import AgentRunnerBase as _RunnerBase  # noqa: PLC0415
        from agenthicc.plugins.registry import build_registry              # noqa: PLC0415
        from agenthicc.agents.plugin import BASE_SYSTEM_PROMPT as _BASE   # noqa: PLC0415
        from agenthicc.runners.tool_populator import populate_agent_tools  # noqa: PLC0415

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
        # Populate meta.tools from the registered tool classes.
        populate_agent_tools(agent_instance, registry.tools)

        # Global hooks
        hooks: list[ToolCapabilityGate | ApprovalGate] = []
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

        # PRD-124: inject spawn_subagents into every turn so any agent can
        # optionally spawn a concurrent subagent pool.
        if ctx.runner is not None:
            from agenthicc.subagents.tool import make_spawn_subagents_tool  # noqa: PLC0415
            from agenthicc.runners.retry import RetryConfig  # noqa: PLC0415
            _ec = ctx.exec_cfg
            _subagent_retry = RetryConfig(
                max_retries=int(getattr(_ec, "transport_max_retries", 3)),
                base_delay_s=float(getattr(_ec, "transport_retry_base_delay_s", 1.0)),
                max_total_duration_s=float(getattr(_ec, "transport_retry_max_total_s", 0.0)),
            )
            spawn_tool = make_spawn_subagents_tool(
                parent_runner=ctx.runner,
                parent_model=self._model_id,
                all_tools=list(registry.tools),
                app_state=ctx.app_state,
                processor=ctx.processor,
                conv_store=ctx.conv_store,
                tool_registry=registry,
                retry_config=_subagent_retry,
            )
            registry.register(spawn_tool, source="builtin")
            populate_agent_tools(agent_instance, registry.tools)

        return agent_instance, active_runner

    # ── step 8: streaming loop ────────────────────────────────────────────────

    async def _stream(
        self,
        agent_instance: object,
        agent_text: str,
        active_runner: AgentRunnerBase,
    ) -> None:
        from lauren_ai._config import AgentConfig as _AgentConfig  # noqa: PLC0415
        ctx           = self._ctx

        # Heal any dangling tool_calls left by an interrupted previous turn
        # (e.g. a plan-approval that was cancelled while awaiting the user's
        # second review).  ensure_valid() is unconditional — unlike messages()
        # it does not wait for a subsequent user message to confirm the turn
        # is complete, making it safe to call right before run_stream().
        if ctx.session_memory is not None:
            ctx.session_memory.ensure_valid()

        # PRD-135: auto-compaction is driven *inside* the run loop by lauren-ai's
        # exact-count compaction ladder (rung 1 — proactive LLM summarisation —
        # then the hard pre-send guard).  It fires at ``summarize_at`` of the live
        # window on every turn, including turn 0 (the resumed/prior history plus
        # the just-added user message), so the old pre-run `should_compact` pass
        # is redundant and has been removed.  The manual `/compact` command still
        # uses `compact_memory` directly.
        #
        # PRD-133/136: the live-context budget is derived from the model's real
        # context window — resolved from the [memory.context_windows] map (per
        # model) → registry → default (ExecutionSettings.effective_context_window).
        # usable = window − completion reservation − head-room; summarisation fires
        # at ``summarize_at`` of that live window, and the same window feeds
        # lauren-ai's hard pre-send guard via AgentConfig.context_window
        # (PRD-133 D/E) so a request can never exceed the window.
        _auto_compact = bool(getattr(ctx.exec_cfg, "auto_compact", True))
        if ctx.exec_cfg is not None:
            _window = ctx.exec_cfg.effective_context_window()
            _window_tokens = ctx.exec_cfg.effective_usable_budget()
        else:
            from lauren_ai._config import context_window_for  # noqa: PLC0415
            _window = context_window_for(self._model_id)
            _window_tokens = max(1, _window - _AgentConfig().max_tokens_per_turn - max(4_000, _window // 25))

        # PRD-129 Phase 1/3: one idempotency ledger per turn, created OUTSIDE the
        # retry loop so it survives across attempts.  When a transient failure
        # rolls session_memory back to its pre-turn snapshot and the turn re-runs,
        # any tool that already completed successfully (write_file, run_bash,
        # git_commit, …) is replayed from the ledger instead of re-executed.
        #
        # When session memory is journaled, the ledger is DURABLE — every record
        # is fsync'd to the journal keyed by this turn's id — so even a process
        # crash mid-turn can be resumed (Phase 3) with completed tools replayed.
        # A resumed turn arrives with a pre-seeded ledger (ctx.resume_ledger).
        _journal = getattr(ctx.session_memory, "journal", None)
        if ctx.resume_ledger is not None:
            turn_ledger = ctx.resume_ledger
        elif _journal is not None:
            from agenthicc.runners.durable_ledger import DurableIdempotencyLedger  # noqa: PLC0415
            turn_ledger = DurableIdempotencyLedger(_journal, self._intent_id)
        else:
            from lauren_ai import IdempotencyLedger  # noqa: PLC0415
            turn_ledger = IdempotencyLedger()
        self._turn_ledger = turn_ledger

        # PRD-129 Phase 3: mark the turn's start + rollback point in the journal.
        # On a crash the absence of a matching turn_completed (written in the
        # finally below) flags this turn for resumption.
        if _journal is not None and ctx.session_memory is not None:
            _journal.turn_started(
                self._intent_id, agent_text, len(ctx.session_memory._messages)
            )

        # PRD-126: one streaming attempt — the unit retried on transient network
        # errors.  The user message is added inside run_stream(), so the retry
        # helper snapshots session_memory before each attempt and restores it on
        # a transient failure, guaranteeing a clean pre-turn history every time.
        async def _stream_once() -> None:
            local_turn: list[str] = []
            stream = await active_runner.run_stream(
                agent_instance, agent_text,
                memory=ctx.session_memory,
                idempotency_ledger=turn_ledger,
                config_override=_AgentConfig(
                    max_turns=ctx.max_agent_turns,
                    parallel_tool_calls=True,
                    memory_window_tokens=_window_tokens,
                    summarize_at=0.8 if _auto_compact else None,
                    summary_model=self._model_id,
                    context_window=_window,
                ),
            )
            async for chunk in stream:
                if chunk.delta:
                    local_turn.append(chunk.delta)
                    if ctx.output_collector is not None:
                        ctx.output_collector.append(chunk.delta)

                # PRD-135: surface auto-compaction (and other out-of-band status)
                # to the user — it is NOT part of the assistant's content.
                if chunk.system_notice is not None and ctx.conv_store:
                    ctx.conv_store.append_event("system", {"text": chunk.system_notice})

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
                    turn_text = "".join(local_turn).strip()
                    local_turn = []
                    if turn_text and ctx.conv_store:
                        ctx.conv_store.append_event("text", {"text": turn_text})
                    # Auto-index completed turn text for semantic search (PRD-101).
                    if turn_text and ctx.semantic_index is not None:
                        doc_id = f"{self._intent_id}_{ctx.completed_turns}"
                        asyncio.create_task(
                            ctx.semantic_index.add(doc_id, turn_text)
                        )

        try:
            await self._stream_with_retry(_stream_once)
        except (asyncio.CancelledError, KeyboardInterrupt):
            if ctx.conv_store:
                ctx.conv_store.close_turn()
            raise   # must propagate so task.cancel() terminates the workflow runner
        except Exception as exc:
            if ctx.conv_store:
                # Emit one well-formatted error event with the exception class name.
                # Do NOT call fail_turn/close_turn here — the finally block handles
                # state cleanup idempotently, preventing the double-fail bug.
                ctx.conv_store.append_event("error", {
                    "message": f"{type(exc).__name__}: {exc}"
                })
            if _is_permanent_error(exc):
                # PRD-117: HTTP 4xx errors are structurally permanent — retrying
                # will always produce the same failure.  Re-raise so the phase
                # loop can exit immediately instead of exhausting its retry cap.
                # _stream()'s finally block still runs → close_turn() is called.
                raise
            # Transient errors that survive _stream_with_retry are swallowed here
            # (PRD-117): the phase loop re-runs the whole turn and decides.
        finally:
            self._turn_active = False
            if ctx.conv_store:
                # close_turn() is idempotent — safe even when CancelledError path
                # already called it above.
                ctx.conv_store.close_turn()
            # PRD-129 Phase 3: mark the turn durably complete.  This runs for
            # success, handled errors, and cancellation — only a hard process
            # death (SIGKILL) skips it, leaving the turn flagged for resume.
            if _journal is not None:
                try:
                    _journal.turn_completed(self._intent_id)
                except OSError:
                    pass

    # ── transport retry wrapper (PRD-126) ─────────────────────────────────────

    async def _stream_with_retry(self, stream_once: Callable[[], Awaitable[None]]) -> None:
        """Run one streaming attempt with snapshot-rollback retry.

        Delegates to the shared :func:`~agenthicc.runners.retry.run_with_transport_retry`.
        Snapshots ``session_memory`` before each attempt; on a transient network
        error it restores the snapshot, resets approval-turn state so any gate is
        re-presented, then retries.  Reads bounds from ``ctx.exec_cfg``.
        """
        from agenthicc.runners.retry import RetryConfig, run_with_transport_retry  # noqa: PLC0415

        ctx = self._ctx
        exec_cfg = ctx.exec_cfg
        config = RetryConfig(
            max_retries=int(getattr(exec_cfg, "transport_max_retries", 3)),
            base_delay_s=float(getattr(exec_cfg, "transport_retry_base_delay_s", 1.0)),
            max_total_duration_s=float(getattr(exec_cfg, "transport_retry_max_total_s", 0.0)),
        )

        # PRD-129: on a rollback, promote the just-executed (now rolled-back)
        # tool results so the next attempt replays them instead of re-executing
        # their side effects.  Promotion happens ONLY here (on a real rollback),
        # so a legitimate repeat call within a single forward attempt still runs
        # live and sees fresh data.
        reset_fns: list[Callable[[], None]] = []
        _ledger = getattr(self, "_turn_ledger", None)
        if _ledger is not None:
            reset_fns.append(_ledger.promote)
        if ctx.approval_svc is not None:
            reset_fns.append(ctx.approval_svc.reset_turn_memory)

        await run_with_transport_retry(
            stream_once,
            config=config,
            memory=ctx.session_memory,
            deadline_monotonic=ctx.retry_deadline_monotonic,
            on_retry=self._emit_retry,
            reset_fns=reset_fns,
        )

    async def _emit_retry(self, attempt: int, max_retries: int, delay: float, exc: BaseException) -> None:
        """Observability + user notification for a scheduled transport retry."""
        ctx = self._ctx
        if ctx.conv_store is not None:
            ctx.conv_store.append_event("system", {
                "text": f"⟳ Network error — retrying ({attempt}/{max_retries})…",
            })
        import logging as _logging  # noqa: PLC0415
        _logging.getLogger(__name__).warning(
            "Transient network error on attempt %d/%d, retrying in %.1fs: %s: %s",
            attempt, max_retries, delay, type(exc).__name__, exc,
        )
        if ctx.processor is not None:
            from agenthicc.kernel import Event  # noqa: PLC0415
            await ctx.processor.emit(Event.create("TransportRetryScheduled", {
                "scope":       "agent_turn",
                "attempt":     attempt,
                "max_retries": max_retries,
                "delay_s":     delay,
                "error_type":  type(exc).__name__,
            }))

    # ── step 9: kernel completion event ───────────────────────────────────────

    async def _emit_intent_complete(self, status: str = "complete") -> None:
        from agenthicc.kernel import Event  # noqa: PLC0415
        await self._ctx.processor.emit(
            Event.create("IntentStatusChanged", {
                "intent_id": self._intent_id,
                "status":    status,
            })
        )


# ── compatibility shim ────────────────────────────────────────────────────────

async def _run_agent_turn(
    text: str,
    runner: AgentRunnerBase,
    processor: EventProcessor,
    session_memory: ShortTermMemory | None = None,
    max_agent_turns: int = 200,
    conv_store: ConversationStore | None = None,
    app_state: AppState | None = None,
    exec_cfg: ExecutionSettings | None = None,
    skills: dict[str, SkillDef] | None = None,
    mention_cache: MentionCache | None = None,
    project_plugin_tools: list[PluginTool] | None = None,
    mcp_registry: McpToolRegistry | None = None,
    active_agent: str | None = None,
    completed_turns: int = 0,
    approval_svc: ApprovalService | None = None,
    output_collector: list[str] | None = None,
    system_prompt_suffix: str = "",
    memory_router: MemoryRouter | None = None,
    semantic_index: SemanticIndex | None = None,
    retry_deadline_monotonic: float | None = None,
    resume_turn_id: str | None = None,
    resume_ledger: object | None = None,
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
        memory_router=memory_router,
        semantic_index=semantic_index,
        retry_deadline_monotonic=retry_deadline_monotonic,
        resume_turn_id=resume_turn_id,
        resume_ledger=resume_ledger,
    )
    await AgentTurnRunner(ctx).run()
