"""AgentTurnRunner — executes one agent turn (PRD-92).

``AgentTurnContext`` (see agent_turn_context.py) carries all configuration.
``AgentTurnRunner`` executes it via composable private methods, each
independently testable.

``_run_agent_turn`` is kept as a thin compatibility shim so all existing
call sites continue to work without modification.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, cast

from agenthicc.runners.agent_turn_context import AgentTurnContext
from agenthicc.tools.hooks import (
    AfterToolHookDecision,
    ErrorToolHookDecision,
    LifecycleHook,
    ToolCallContext,
)


def _config_value(config: object | None, name: str, default: object) -> object:
    """Read an optional execution setting from real configs and test doubles."""
    if config is None:
        return default
    try:
        return object.__getattribute__(config, name)
    except AttributeError:
        return default


def _read_text_if_exists(path: str) -> str:
    """Read a file for the edit-preview adapter, returning empty when absent."""
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _preserve_interrupted_memory(memory: object | None) -> None:
    """Keep valid completed context while healing an interrupted tool tail.

    Cancelling a turn must not roll the shared session conversation back to its
    pre-turn count: that would erase the tool calls and assistant reasoning the
    next user message needs. The memory boundary heals any unanswered final
    tool call into a provider-valid interruption result and, when supported,
    persists that repair in the session journal.
    """
    if memory is None:
        return
    persist_heal = getattr(memory, "ensure_valid_and_persist", None)
    if callable(persist_heal):
        persist_heal()
        return
    ensure_valid = getattr(memory, "ensure_valid", None)
    if callable(ensure_valid):
        ensure_valid()


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
    _TRANSIENT_NAMES = frozenset(
        {
            # httpx
            "ReadTimeout",
            "ConnectTimeout",
            "WriteTimeout",
            "PoolTimeout",
            "ConnectError",
            "ReadError",
            "WriteError",
            "RemoteProtocolError",
            # anthropic / openai SDK
            "APITimeoutError",
            "APIConnectionError",
            # generic
            "NetworkError",
            "RemoteDisconnected",
        }
    )
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
    try:
        from lauren_ai import _exceptions  # noqa: PLC0415

        AgentContextOverflowError = getattr(
            _exceptions,
            "AgentContextOverflowError",
            None,
        )
    except ImportError:
        AgentContextOverflowError = None

    if AgentContextOverflowError is not None and isinstance(exc, AgentContextOverflowError):
        return True
    status = _http_status_code(exc)
    if status is None:
        return False
    # 429 is rate-limited — transient, worth waiting and retrying.
    # All other 4xx are client errors (bad model name, bad API key, …) — permanent.
    return 400 <= status < 500 and status != 429


def _is_context_overflow_error(exc: BaseException) -> bool:
    """Return whether *exc* is a provider context-length rejection.

    Lauren versions before the context-window guard exposed this as a generic
    ``TransportError`` wrapping an SDK ``BadRequestError``.  Do not rely only
    on Lauren's newer ``AgentContextOverflowError``: the provider error in
    practice is often the only signal available at this integration boundary.
    """
    try:
        from lauren_ai import _exceptions  # noqa: PLC0415

        overflow_type = getattr(_exceptions, "AgentContextOverflowError", None)
    except ImportError:
        overflow_type = None
    if overflow_type is not None and isinstance(exc, overflow_type):
        return True

    # Walk the short exception chain used by SDK wrappers.  Include the type
    # name because several SDKs put the useful text only on repr-like output.
    seen: set[int] = set()
    current: BaseException | None = exc
    markers = (
        "context length",
        "maximum context",
        "context window",
        "too many tokens",
        "prompt is too long",
        "requested tokens",
    )
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        description = f"{type(current).__name__}: {current}".lower()
        if any(marker in description for marker in markers):
            return True
        current = getattr(current, "__cause__", None) or getattr(current, "__context__", None)
    return False


if TYPE_CHECKING:
    from lauren_ai import AgentContext
    from lauren_ai._tools import ToolResult
    from collections.abc import Awaitable, Callable
    from lauren_ai._agents._runner import AgentRunnerBase
    from lauren_ai._memory import ShortTermMemory
    from lauren_ai._transport import ToolCall
    from lauren_ai import IdempotencyLedger
    from lauren_ai._signals import ToolCallStarted, ToolCallComplete
    from agenthicc.config import ExecutionSettings
    from agenthicc.kernel.processor import EventProcessor
    from agenthicc.memory.router import MemoryRouter
    from agenthicc.memory.vector import SemanticIndex
    from agenthicc.mentions.cache import MentionCache
    from agenthicc.skills.loader import SkillDef, SkillPermissionSet
    from agenthicc.tools.approval import ApprovalService
    from agenthicc.tools.base import ToolLike
    from agenthicc.tools.mcp import McpToolRegistry
    from agenthicc.tui.conversation_store import AppState, ConversationStore
    from agenthicc.runners.usage_ledger import UsageLedger, UsageRunTracker


# ── formatting helper (module-level, unchanged) ───────────────────────────────


def _fmt_args(args: dict[str, object]) -> str:
    from rich.markup import escape as _e  # noqa: PLC0415

    items = list(args.items())
    if not items:
        return ""
    if len(items) == 1:
        return f"[dim]({_e(repr(items[0][1])[:60])})[/dim]"

    def format_item(key: str, value: object) -> str:
        # Module and path arguments are identifiers users need to verify. A
        # silent 25-character cut makes a valid module look invalid in the TUI
        # even though the complete value is sent to the tool executor.
        limit = 80 if key in {"module", "path", "symbol"} else 25
        if key.casefold() in {
            "value",
            "password",
            "passwd",
            "secret",
            "token",
            "api_key",
            "authorization",
            "cookie",
        }:
            rendered = "<redacted>"
        else:
            rendered = repr(value)
        if len(rendered) > limit:
            rendered = rendered[: limit - 1] + "…"
        return f"{_e(key)}={_e(rendered)}"

    return "[dim](" + ", ".join(format_item(k, v) for k, v in items[:3]) + ")[/dim]"


_EXPLORATION_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "cookie",
        "password",
        "passwd",
        "secret",
        "token",
        "value",
    }
)
_EXPLORATION_SENSITIVE_MARKERS = (
    "api_key=",
    "authorization:",
    "bearer ",
    "cookie=",
    "password=",
    "passwd=",
    "secret=",
    "token=",
)


def _exploration_value(key: str, value: object, *, limit: int = 80) -> str:
    """Return one bounded, non-secret value for an exploration label."""
    if key.casefold() in _EXPLORATION_SENSITIVE_KEYS:
        return "<redacted>"
    if isinstance(value, str):
        rendered = value
    elif isinstance(value, (int, float, bool)):
        rendered = str(value)
    elif value is None:
        rendered = "None"
    elif isinstance(value, list):
        rendered = ", ".join(_exploration_value(key, item) for item in value[:3])
        if len(value) > 3:
            rendered += ", …"
    else:
        return ""
    if any(marker in rendered.casefold() for marker in _EXPLORATION_SENSITIVE_MARKERS):
        return "<redacted>"
    return rendered[:limit] + ("…" if len(rendered) > limit else "")


def _exploration_extra_arguments(args: Mapping[str, object], used_keys: set[str]) -> str:
    """Format unconsumed tool arguments for a safe exploration label."""
    details: list[str] = []
    for key, value in args.items():
        if key in used_keys:
            continue
        safe_value = _exploration_value(key, value, limit=40)
        if safe_value:
            details.append(f"{key}={safe_value}")
    rendered = ", ".join(details)
    return rendered[:79] + "…" if len(rendered) > 80 else rendered


def _exploration_with_arguments(
    target: str, args: Mapping[str, object], used_keys: set[str]
) -> str:
    """Append safe, bounded arguments that are not part of *target*."""
    details = _exploration_extra_arguments(args, used_keys)
    if not details:
        return target
    combined = f"{target} ({details})" if target else details
    return combined[:79] + "…" if len(combined) > 80 else combined


def _exploration_target(name: str, args: Mapping[str, object]) -> str:
    """Build a safe, bounded target label for an exploratory tool call."""
    search_names = {
        "grep_file",
        "grep_files",
        "search_agenthicc_docs",
        "search_agenthicc_source",
        "search_files",
        "git_grep",
    }
    if name in search_names:
        query_key = "pattern" if "pattern" in args else "query"
        query = _exploration_value(query_key, args.get(query_key, ""))
        location_key = next(
            (key for key in ("path", "module", "section", "ref") if key in args),
            "",
        )
        location = _exploration_value(location_key, args.get(location_key, ""))
        if query and location and location != ".":
            target = f"{query} in {location}"
        else:
            target = query or location
        return _exploration_with_arguments(target, args, {query_key, location_key})

    for key in ("path", "paths", "target", "ref", "section", "module"):
        if key in args:
            return _exploration_with_arguments(_exploration_value(key, args[key]), args, {key})
    return _exploration_with_arguments("", args, set())


def _exploration_presentation(name: str, args: Mapping[str, object]) -> dict[str, object]:
    """Return bounded presentation metadata persisted on a tool event."""
    presentation: dict[str, object] = {"exploratory": True}

    # ``batch_read`` needs structured presentation metadata. Turning the
    # complete path list into one string would lose the count when the target
    # is abbreviated (and would make the TUI guess whether an ellipsis meant
    # omitted files). Keep only the first two safe paths and persist the
    # remainder explicitly. The executor still receives and returns the full
    # argument/result; this is only a bounded display projection.
    if name == "batch_read":
        raw_paths = args.get("paths")
        if isinstance(raw_paths, list):
            paths = [
                safe_path
                for path in raw_paths
                if isinstance(path, str)
                for safe_path in [_exploration_value("path", path)]
                if safe_path
            ]
            if paths:
                presentation["files"] = paths[:2]
                more_files = len(paths) - 2
                if more_files > 0:
                    presentation["more_files"] = more_files
                extra_arguments = _exploration_extra_arguments(args, {"paths"})
                if extra_arguments:
                    presentation["arguments"] = extra_arguments
                return presentation

    target = _exploration_target(name, args)
    if target:
        presentation["target"] = target
    return presentation


def _tool_output_preview(result: object) -> tuple[list[str], int]:
    """Return a bounded preview and omitted-line count for a tool result."""
    if isinstance(result, dict):
        for key in ("content", "output", "stdout", "result", "error"):
            value = result.get(key)
            if isinstance(value, str):
                text = value
                break
        else:
            text = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    elif isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, ensure_ascii=False, indent=2, default=str)

    lines = text.splitlines()
    if not lines and text:
        lines = [text]
    return lines[:4], max(0, len(lines) - 4)


class _ToolOutputCaptureHook(LifecycleHook):
    """Capture tool results for the scroll-buffer completion event."""

    def __init__(
        self,
        outputs: dict[str, tuple[list[str], int]],
        command_outcomes: list[dict[str, object]] | None = None,
        command_states: dict[str, str] | None = None,
        command_ready: dict[str, bool] | None = None,
    ) -> None:
        self._outputs = outputs
        self._command_outcomes = command_outcomes
        self._command_states = command_states
        self._command_ready = command_ready

    async def after_tool_call(
        self,
        result: object,
        ctx: ToolCallContext,
    ) -> AfterToolHookDecision:
        self._outputs[ctx.tool_use_id] = _tool_output_preview(result)
        if self._command_outcomes is not None and ctx.tool_name in {
            "run_bash",
            "run_command",
            "shell",
            "run_python",
            "run_python_expr",
            "run_tests",
            "wait_terminal",
            "wait_terminal_ready",
            "stop_terminal",
        }:
            candidate = result
            if isinstance(candidate, Mapping) and isinstance(candidate.get("state"), str):
                self._command_outcomes.append(dict(candidate))
                if self._command_states is not None:
                    self._command_states[ctx.tool_use_id] = str(candidate["state"])
                if self._command_ready is not None and isinstance(candidate.get("ready"), bool):
                    self._command_ready[ctx.tool_use_id] = bool(candidate["ready"])
        return AfterToolHookDecision.proceed()

    async def on_tool_error(
        self,
        exc: Exception,
        ctx: ToolCallContext,
    ) -> ErrorToolHookDecision:
        self._outputs[ctx.tool_use_id] = ([f"{type(exc).__name__}: {exc}"], 0)
        return ErrorToolHookDecision.reraise()


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
        self._intent_id: str = ""
        self._agent_id: str = ""
        self._model_id: str = ""
        self._model_short: str = ""
        self._turn_active: bool = True

        # Tool tracking — populated by signal handlers.
        self._tool_args: dict[str, dict[str, object]] = {}
        self._tool_names: dict[str, str] = {}
        self._tool_outputs: dict[str, tuple[list[str], int]] = {}
        self._exploratory_tools: set[str] = set()
        self._tool_states: dict[str, str] = {}
        self._tool_ready: dict[str, bool] = {}
        self._file_snapshots: dict[str, tuple[str, str]] = {}

        # Content produced during the turn.
        self._skill_suffix: str = ""
        self._usage_tracker: UsageRunTracker | None = None

    # ── public entry point ────────────────────────────────────────────────────

    async def run(self) -> None:
        """Execute the full agent turn end-to-end."""
        ctx = self._ctx
        reset_browser_budget = getattr(ctx.browser_manager, "reset_turn_budget", None)
        if callable(reset_browser_budget):
            reset_browser_budget()
        if ctx.runner is None:
            if ctx.conv_store:
                ctx.conv_store.append_event(
                    "error",
                    {"message": "⚠ No LLM configured. Set ANTHROPIC_API_KEY or OPENAI_API_KEY."},
                )
            return

        self._resolve_model()
        await self._emit_intent_created()
        self._begin_conv_turn()
        self._register_signal_handlers()

        agent_text = await self._inject_mentions()
        self._inject_skills()

        agent_instance, active_runner = self._build_agent()

        # Keep cancellation active through the entire provider/tool loop. The
        # foreground command tool performs child-process cleanup before raising
        # the cancellation back to this task; without this scope it would turn
        # ESC into an ordinary ``cancelled`` tool result and the model would
        # continue with another turn.
        from agenthicc.tools.exec import _PROPAGATE_TOOL_CANCELLATION  # noqa: PLC0415

        cancellation_token = _PROPAGATE_TOOL_CANCELLATION.set(True)
        try:
            # Always emit IntentStatusChanged — success or failure — so the
            # kernel intent never stays permanently at "pending" after an
            # exception.
            _intent_status = "complete"
            try:
                await self._stream(agent_instance, agent_text, active_runner)
            except (asyncio.CancelledError, KeyboardInterrupt):
                _intent_status = "failed"
                await self._close_browser_after_failure()
                raise
            except Exception:
                _intent_status = "failed"
                await self._close_browser_after_failure()
                raise
            finally:
                await self._emit_intent_complete(status=_intent_status)
        finally:
            _PROPAGATE_TOOL_CANCELLATION.reset(cancellation_token)

    async def _close_browser_after_failure(self) -> None:
        """Release browser resources when the provider turn cannot continue."""
        closer = getattr(self._ctx.browser_manager, "close_session", None)
        if not callable(closer):
            return
        try:
            await closer()
        except Exception:  # noqa: BLE001 — preserve the original provider failure
            pass

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
            cfg = getattr(transport, "_config", None)
            self._model_id = getattr(cfg, "model", "unknown") if cfg else "unknown"
        self._model_short = self._model_id.split("/")[-1]

    def _provider_label(self) -> str:
        """Return the configured provider label for usage diagnostics."""
        transport = getattr(self._ctx.runner, "_transport", None)
        config = getattr(transport, "_config", None)
        provider = getattr(config, "provider", None)
        return provider if isinstance(provider, str) and provider else "unknown"

    # ── step 2: kernel event ──────────────────────────────────────────────────

    async def _emit_intent_created(self) -> None:
        from agenthicc.kernel import Event  # noqa: PLC0415

        # PRD-129 Phase 3: a resumed turn reuses its original id so the durable
        # tool ledger and journal turn markers line up.
        self._intent_id = self._ctx.resume_turn_id or uuid.uuid4().hex
        self._agent_id = f"agent-{self._intent_id[:8]}"
        await self._ctx.processor.emit(
            Event.create(
                "IntentCreated",
                {
                    "intent_id": self._intent_id,
                    "raw_text": self._ctx.text,
                },
            )
        )

    # ── step 3: conversation turn lifecycle ───────────────────────────────────

    def _begin_conv_turn(self) -> None:
        conv = self._ctx.conv_store
        if conv:
            conv.begin_turn(f"assistant ({self._model_short})", self._agent_id)
            conv.append_event(
                "turn_start",
                {
                    "turn_id": self._agent_id,
                    "agent_name": f"assistant ({self._model_short})",
                },
            )

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

        signal_decorator = cast(
            Callable[
                [type[object]],
                Callable[[Callable[..., Awaitable[None]]], Callable[..., Awaitable[None]]],
            ],
            signals.on,
        )

        @signal_decorator(_TCS)
        async def _on_tool_started(sig: ToolCallStarted) -> None:
            if not self._turn_active:
                return
            args = dict(getattr(sig, "input", {}) or {})
            name = getattr(sig, "tool_name", "")
            tid = getattr(sig, "tool_use_id", "")
            self._tool_names[tid] = name
            self._tool_args[tid] = args
            self._tool_outputs.pop(tid, None)
            self._tool_states.pop(tid, None)
            self._tool_ready.pop(tid, None)
            if self._ctx.conv_store:
                self._ctx.conv_store.set_tool(name)
            if name in self._FILE_EDIT_TOOLS and args.get("path"):
                await self._snapshot_file(tid, args["path"])

        @signal_decorator(_TCC)
        async def _on_tool_complete(sig: ToolCallComplete) -> None:
            if not self._turn_active:
                return
            await self._handle_tool_complete(sig)

    async def _snapshot_file(self, tid: str, rel_path: str) -> None:
        full = os.path.join(os.getcwd(), rel_path) if not os.path.isabs(rel_path) else rel_path
        try:
            original = await asyncio.to_thread(_read_text_if_exists, full)
            self._file_snapshots[tid] = (rel_path, original)
        except Exception:  # noqa: BLE001
            pass

    async def _handle_tool_complete(self, sig: ToolCallComplete) -> None:
        tid: str = getattr(sig, "tool_use_id", "")
        success: bool = bool(getattr(sig, "success", True))
        ms: float | None = getattr(sig, "duration_ms", None)
        name = self._tool_names.pop(tid, tid)
        args = self._tool_args.pop(tid, {})
        state = self._tool_states.pop(tid, None)
        ready = self._tool_ready.pop(tid, None)
        conv = self._ctx.conv_store

        showed_diff = False
        if tid in self._file_snapshots:
            rel_path, original = self._file_snapshots.pop(tid)
            full = os.path.join(os.getcwd(), rel_path) if not os.path.isabs(rel_path) else rel_path
            try:
                new_content = await asyncio.to_thread(_read_text_if_exists, full)
                old_lines = original.splitlines()
                new_lines = new_content.splitlines()
                changed = old_lines != new_lines
                if changed and conv:
                    conv.append_event(
                        "file_modified",
                        {
                            "path": rel_path,
                            "old_lines": old_lines,
                            "new_lines": new_lines,
                            "tool": name,
                        },
                    )
                    showed_diff = True
            except Exception:  # noqa: BLE001
                pass

        if conv:
            conv.clear_tool(success=success)
            output_lines, output_more = self._tool_outputs.pop(tid, ([], 0))
            if not success and not output_lines and getattr(sig, "error", None):
                output_lines = [str(sig.error)]
            # Skip the generic tool_complete line when a file-diff was already
            # rendered — the diff is more informative and the duplicate line
            # ("⎿ write_file(...)  ✓  4ms") is visual noise below the diff.
            if not showed_diff:
                payload: dict[str, object] = {
                    "tool_use_id": tid,
                    "name": name,
                    "success": success,
                    "state": state,
                    "ready": ready,
                    "args_str": _fmt_args(args),
                    "dur_str": f"  [dim]{ms:.0f}ms[/dim]" if ms else "",
                    "output_lines": output_lines,
                    "output_more": output_more,
                }
                if name in self._exploratory_tools:
                    payload["presentation"] = _exploration_presentation(name, args)
                conv.append_event("tool_complete", payload)

    # ── step 5: @mention injection ────────────────────────────────────────────

    async def _inject_mentions(self) -> str:
        """Resolve @mentions and emit mention chips. Returns agent_text."""
        from agenthicc.mentions.injector import (  # noqa: PLC0415
            build_context_prefix,
            InjectionConfig,
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
                    "raw": r.mention.raw,
                    "kind": r.mention.kind.value,  # "file" | "directory" | "url" | "glob" | "unresolved"
                    "ok": getattr(r, "ok", True),
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
            find_matching_skills,
            process_skill_body,
        )
        from agenthicc.skills.loader import filter_skills_for_agent  # noqa: PLC0415

        ctx = self._ctx
        available_skills = filter_skills_for_agent(
            ctx.skills or {},
            ctx.active_agent or "default",
            ctx.skill_permissions,
        )
        matched = find_matching_skills(ctx.text, available_skills)
        if matched:
            self._skill_suffix = "\n\n---\n\n" + "\n\n".join(
                f"## Skill: {s.name}\n{process_skill_body(s, args=[], cwd=Path(os.getcwd()))}"
                for s in matched
            )

    # ── step 7: build @agent class and runner ─────────────────────────────────

    def _build_agent(self) -> tuple[object, AgentRunnerBase]:
        """Construct the @agent-decorated class, populate meta.tools, build runner.

        Returns (agent_instance, active_runner).
        """
        from lauren_ai._agents import agent as agent_decorator, use_tools  # noqa: PLC0415
        from lauren_ai._agents._runner import AgentRunnerBase as _RunnerBase  # noqa: PLC0415
        from agenthicc.plugins.registry import build_registry  # noqa: PLC0415
        from agenthicc.agents.plugin import BASE_SYSTEM_PROMPT as _BASE  # noqa: PLC0415
        from agenthicc.runners.tool_populator import populate_agent_tools  # noqa: PLC0415
        from agenthicc.tools.capabilities import (  # noqa: PLC0415
            get_tool_capabilities,
            is_exploratory_tool,
        )

        ctx = self._ctx

        # Tool registry
        mcp_tools = ctx.mcp_registry.all_tools() if ctx.mcp_registry is not None else []
        project_tools: list[ToolLike] = [*(ctx.project_plugin_tools or []), *mcp_tools]
        registry = build_registry(
            agent_name=ctx.active_agent or "default",
            project_plugin_tools=project_tools,
        )
        excluded_capabilities = ctx.excluded_capabilities
        allowed_tool_names = ctx.allowed_tool_names
        visible_tools = [
            tool
            for tool in registry.tools
            if (
                allowed_tool_names is None
                or getattr(tool, "__name__", getattr(tool, "name", "")) in allowed_tool_names
            )
            and not (get_tool_capabilities(tool) & excluded_capabilities)
        ]
        visible_names = {
            getattr(tool, "__name__", getattr(tool, "name", "")) for tool in visible_tools
        }
        excluded_names = [name for name in registry.names if name not in visible_names]

        # System prompt
        cfg_base = getattr(ctx.exec_cfg, "base_system_prompt", None) or ""
        effective_base = cfg_base or _BASE
        system = (
            effective_base
            + (f"\n\n{ctx.system_prompt_suffix}" if ctx.system_prompt_suffix else "")
            + (self._skill_suffix or "")
            + (
                f"\n\n{registry.describe(excluded_names=excluded_names)}"
                if registry.describe(excluded_names=excluded_names)
                else ""
            )
        )
        if allowed_tool_names is not None:
            system += (
                "\n\n### Phase-local tool policy\n"
                "Only the tools listed above are available in this phase. Do not call shell, "
                "run_bash, run_command, or any other omitted tool. When calling write_file, "
                "provide both required arguments: path and complete content."
            )

        @agent_decorator(model=self._model_id, system=system)
        @use_tools(*visible_tools)
        class _AgenthiccAgent:  # type: ignore[type-var]  # lauren-ai decorator cannot infer dynamic class
            pass

        agent_instance = _AgenthiccAgent()
        # Populate meta.tools from the registered tool classes.
        populate_agent_tools(agent_instance, visible_tools)

        # Global hooks
        hooks: list[object] = [
            _ToolOutputCaptureHook(
                self._tool_outputs,
                self._ctx.command_outcomes,
                self._tool_states,
                self._tool_ready,
            )
        ]
        self._exploratory_tools = {
            str(getattr(tool, "__name__", getattr(tool, "name", "")))
            for tool in visible_tools
            if is_exploratory_tool(tool)
        }
        if ctx.app_state is not None:
            from agenthicc.tools.capability_gate import ToolCapabilityGate  # noqa: PLC0415

            hooks.append(ToolCapabilityGate(ctx.app_state))
            if ctx.approval_svc is not None:
                from agenthicc.tools.approval import ApprovalGate  # noqa: PLC0415

                hooks.append(ApprovalGate(ctx.app_state, ctx.approval_svc))

        runner_class: type[AgentRunnerBase] = _RunnerBase
        next_queued_message = ctx.next_queued_message
        if next_queued_message is not None:
            claim_queued_message = next_queued_message

            class _QueuedInputRunner(_RunnerBase):
                """Commit tool results before injecting the next queued input.

                Lauren-ai's stream loop normally commits the returned tool
                results immediately after ``_execute_tools`` returns. This
                override performs that same commit before adding a queued user
                message, preserving the valid
                ``assistant(tool_use) -> tool_result -> user`` sequence.
                """

                async def _execute_tools(
                    self,
                    tool_calls: list[ToolCall],
                    *,
                    ctx: "AgentContext",
                    agent: object,
                    model: str,
                    run_sinks: tuple[object, ...] = (),
                ) -> list[ToolResult]:
                    results = await super()._execute_tools(
                        tool_calls,
                        ctx=ctx,
                        agent=agent,
                        model=model,
                        run_sinks=run_sinks,
                    )
                    if not results or ctx.turn >= ctx.config.max_turns - 1:
                        return results
                    queued = claim_queued_message()
                    if queued is None:
                        return results
                    ctx.memory.add_tool_results(results)
                    ctx.memory.add_user(queued)
                    return []

            runner_class = _QueuedInputRunner

        active_runner = runner_class(
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
                all_tools=list(visible_tools),
                app_state=ctx.app_state,
                processor=ctx.processor,
                conv_store=ctx.conv_store,
                usage_ledger=ctx.usage_ledger,
                conversation_id=ctx.conversation_id,
                parent_run_id=self._intent_id,
                tool_registry=registry,
                retry_config=_subagent_retry,
                provider_options={
                    "temperature": _config_value(_ec, "temperature", 1.0),
                    "top_p": _config_value(_ec, "top_p", None),
                    "max_completion_tokens": _config_value(_ec, "max_completion_tokens", None),
                    "request_options": _config_value(_ec, "request_options", None),
                },
            )
            registry.register(spawn_tool, source="builtin")
            spawn_name = getattr(spawn_tool, "__name__", getattr(spawn_tool, "name", ""))
            if (allowed_tool_names is None or spawn_name in allowed_tool_names) and not (
                get_tool_capabilities(spawn_tool) & excluded_capabilities
            ):
                visible_tools.append(spawn_tool)
                populate_agent_tools(agent_instance, visible_tools)

        return agent_instance, active_runner

    # ── step 8: streaming loop ────────────────────────────────────────────────

    async def _auto_compact_if_needed(
        self,
        active_runner: AgentRunnerBase,
        agent_text: str,
        *,
        max_input_tokens: int,
        force: bool = False,
    ) -> bool:
        """Compact the shared session memory before an oversized turn.

        Recent Lauren releases own this operation and perform an exact
        ``count_tokens`` check.  Older releases (including 1.3.1) expose only
        the sliding-window heuristic, so agenthicc keeps this compatibility
        fallback at the send boundary.  The fallback uses the bounded,
        map-reduce compactor and is deliberately conservative: it compacts at
        80% of the usable input budget, leaving room for the current user
        message and provider/system/tool framing.

        ``force`` is used only after a provider explicitly rejects the request
        for context length.  The caller restores the pre-attempt snapshot
        before invoking this method, so the retried turn cannot duplicate its
        user message.
        """
        ctx = self._ctx
        memory = ctx.session_memory
        if not bool(getattr(ctx.exec_cfg, "auto_compact", True)) or memory is None:
            return False

        messages = getattr(memory, "_messages", None)
        if not isinstance(messages, list) or not messages:
            # A single enormous new user message has no older history to
            # summarise.  It needs a user-facing irreducible-overflow error.
            return False

        estimate = getattr(memory, "token_estimate", 0)
        if not isinstance(estimate, int):
            return False
        projected = estimate + max(1, len(agent_text) // 4)
        threshold = max(1, int(max_input_tokens * 0.8))
        if not force and projected < threshold:
            return False

        transport = getattr(active_runner, "_transport", None)
        if transport is None or not callable(getattr(transport, "complete", None)):
            return False

        before = estimate
        from agenthicc.memory.compactor import compact_memory  # noqa: PLC0415

        await compact_memory(
            memory,
            transport,
            model=self._model_id,
            conv_store=ctx.conv_store,
            max_input_tokens=max_input_tokens,
            usage_ledger=ctx.usage_ledger,
            session_id=ctx.conversation_id,
            run_id=self._intent_id,
        )
        after = getattr(memory, "token_estimate", before)
        if isinstance(after, int) and after < before:
            return True

        # A provider may accept the summarisation request but return no text
        # (some OpenAI-compatible reasoning endpoints do this for a
        # stream=False completion).  Never continue with the same oversized
        # memory in that case.  Use a deterministic, journal-aware lossy
        # fallback as the final safety rung; it is better to retain recent
        # context and continue than to resubmit an identical context-length
        # rejection.
        return self._local_compaction_fallback(max_input_tokens=max_input_tokens)

    def _local_compaction_fallback(self, *, max_input_tokens: int) -> bool:
        """Bound memory locally when an LLM summary is unavailable."""
        memory = self._ctx.session_memory
        if memory is None:
            return False
        before = getattr(memory, "token_estimate", 0)
        if not isinstance(before, int):
            return False

        # Keep a generous recent window but leave ample room for system/tool
        # schemas and the requested completion.  This path is only reached
        # after LLM compaction failed or produced no usable reduction.
        target = max(1, int(max_input_tokens * 0.75))
        trim_to_fit = getattr(memory, "trim_to_fit", None)
        if callable(trim_to_fit):
            trim_to_fit(target)
        after = getattr(memory, "token_estimate", before)

        if not isinstance(after, int) or after >= before:
            messages = getattr(memory, "_messages", None)
            if not isinstance(messages, list) or not messages:
                return False
            from agenthicc.memory.compactor import _format_transcript  # noqa: PLC0415

            # Keep the replacement strictly smaller than the oversized source
            # even when the source is one indivisible user/tool message.
            excerpt = _format_transcript(messages)[-1_000:]
            memory._messages = [
                {
                    "role": "user",
                    "content": (
                        "[COMPACT FALLBACK]\n"
                        "The prior history exceeded the model context and its summary was empty. "
                        "Continue from this recent transcript excerpt:\n"
                        f"{excerpt}"
                    ),
                },
                {
                    "role": "assistant",
                    "content": "Understood. Continuing from the retained context.",
                },
            ]
            after = getattr(memory, "token_estimate", before)

        journal_reset = getattr(memory, "journal_reset", None)
        if callable(journal_reset):
            journal_reset()
        if self._ctx.conv_store is not None:
            self._ctx.conv_store.append_event(
                "system", {"text": "⎋ Compaction fallback: retained recent history"}
            )
        return isinstance(after, int) and after < before

    async def _stream(
        self,
        agent_instance: object,
        agent_text: str,
        active_runner: AgentRunnerBase,
    ) -> None:
        from lauren_ai._config import AgentConfig as _AgentConfig  # noqa: PLC0415

        ctx = self._ctx

        usage_tracker = None
        if ctx.usage_ledger is not None:
            from agenthicc.runners.usage_ledger import UsageRunTracker  # noqa: PLC0415

            usage_tracker = UsageRunTracker(
                ctx.usage_ledger,
                run_id=self._intent_id,
                provider=self._provider_label(),
                model=self._model_id,
                agent_id=self._agent_id,
                agent_name=ctx.active_agent or "default",
            )
            self._usage_tracker = usage_tracker

        # Heal any dangling tool_calls left by an interrupted previous turn
        # (e.g. a plan-approval that was cancelled while awaiting the user's
        # second review).  ensure_valid() is unconditional — unlike messages()
        # it does not wait for a subsequent user message to confirm the turn
        # is complete, making it safe to call right before run_stream().
        if ctx.session_memory is not None:
            ctx.session_memory.ensure_valid()

        turn_completed = False

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
        # Completion ceiling for one round-trip.  lauren-ai defaults this to 4096,
        # which silently truncates a single large tool call (e.g. write_file with a
        # whole source file): the partial call is discarded, the turn yields nothing,
        # and the calling phase retries forever.  ExecutionSettings.max_output_tokens
        # is the configurable ceiling and is what effective_usable_budget() reserves.
        _max_output = int(getattr(ctx.exec_cfg, "max_output_tokens", 0) or 0) or int(
            _AgentConfig().max_tokens_per_turn
        )
        if ctx.exec_cfg is not None:
            _window = ctx.exec_cfg.effective_context_window()
            _window_tokens = ctx.exec_cfg.effective_usable_budget()
        else:
            from agenthicc.config import _context_window_for  # noqa: PLC0415

            _window = _context_window_for(self._model_id)
            _window_tokens = max(1, _window - _max_output - max(4_000, _window // 25))

        # Lauren 1.3.1 has the summarisation fields but not the hard context
        # guard.  Newer versions add ``context_window`` and exact counting.
        # Pass it when available; the dynamic field check keeps the published
        # agenthicc package compatible with both API generations.
        _agent_config_fields = getattr(_AgentConfig, "__dataclass_fields__", {})
        _supports_context_guard = "context_window" in _agent_config_fields

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
            _journal.turn_started(self._intent_id, agent_text, len(ctx.session_memory._messages))

        # PRD-126: one streaming attempt — the unit retried on transient network
        # errors.  The user message is added inside run_stream(), so the retry
        # helper snapshots session_memory before each attempt and restores it on
        # a transient failure, guaranteeing a clean pre-turn history every time.
        _attempt_number = [0]

        async def _stream_once() -> None:
            local_turn: list[str] = []
            _attempt_number[0] += 1
            if usage_tracker is not None and _attempt_number[0] > 1:
                usage_tracker.finalize("failed")
            config_kwargs = _AgentConfig(
                max_turns=ctx.max_agent_turns,
                max_tokens_per_turn=_max_output,
                parallel_tool_calls=True,
                memory_window_tokens=_window_tokens,
                # Old Lauren's heuristic summariser is intentionally disabled
                # here: the agenthicc fallback is bounded, journal-aware, and
                # emits the same user-visible compaction event.
                summarize_at=0.8 if _auto_compact and _supports_context_guard else None,
                summary_model=self._model_id,
            )
            # Provider profiles carry sampling and vendor-specific request
            # options into every turn, including workflow phases.  Use
            # dataclass field detection so a downstream runner double or an
            # older lauren-ai installation remains import-compatible.
            from dataclasses import replace  # noqa: PLC0415

            provider_options: dict[str, object] = {
                "temperature": _config_value(ctx.exec_cfg, "temperature", 1.0),
                "top_p": _config_value(ctx.exec_cfg, "top_p", None),
                "max_completion_tokens": _config_value(ctx.exec_cfg, "max_completion_tokens", None),
                "request_options": _config_value(ctx.exec_cfg, "request_options", None),
            }
            if provider_options["request_options"] is not None:
                from agenthicc.config import RequestOptionSettings  # noqa: PLC0415

                if isinstance(provider_options["request_options"], RequestOptionSettings):
                    provider_options["request_options"] = provider_options[
                        "request_options"
                    ].resolve()
            supported_provider_options = {
                name: value
                for name, value in provider_options.items()
                if name in _agent_config_fields and value is not None
            }
            if supported_provider_options:
                replace_config = cast(Callable[..., _AgentConfig], replace)
                config_kwargs = replace_config(config_kwargs, **supported_provider_options)
            if _supports_context_guard:
                # ``context_window`` is absent from Lauren 1.3.1.  Use the
                # dataclass replacement only on versions that advertise the
                # field, while keeping the old constructor statically typed.
                replace_config = cast(Callable[..., _AgentConfig], replace)
                config_kwargs = replace_config(config_kwargs, context_window=_window)

            if usage_tracker is not None:
                usage_tracker.ensure_call()
            stream = await active_runner.run_stream(
                agent_instance,
                agent_text,
                conversation_id=ctx.conversation_id or None,
                run_id=self._intent_id,
                memory=ctx.session_memory,
                idempotency_ledger=turn_ledger,
                config_override=config_kwargs,
                event_sinks=[usage_tracker.sink] if usage_tracker is not None else None,
            )
            async for chunk in stream:
                if chunk.delta:
                    local_turn.append(chunk.delta)
                    if ctx.output_collector is not None:
                        ctx.output_collector.append(chunk.delta)

                # PRD-135: surface auto-compaction (and other out-of-band status)
                # to the user — it is NOT part of the assistant's content.
                system_notice = getattr(chunk, "system_notice", None)
                if system_notice is not None and ctx.conv_store:
                    ctx.conv_store.append_event("system", {"text": system_notice})

                # Live token update — PRD-83.
                if chunk.usage is not None and usage_tracker is not None:
                    usage_tracker.observe_chunk(chunk.usage)
                elif chunk.usage is not None and ctx.conv_store:
                    u = chunk.usage
                    cst = (
                        u.cost_usd(self._model_id)
                        if callable(getattr(u, "cost_usd", None))
                        else 0.0
                    )
                    ctx.conv_store.add_tokens(u.input_tokens, u.output_tokens, cst)

                if chunk.stop_reason is not None:
                    # A max_tokens stop means the completion was cut off. If it was
                    # cut mid-tool-call the partial call is discarded and the
                    # sub-turn produces nothing at all, which otherwise looks like
                    # the model doing nothing. Say so, and name the setting to raise.
                    if str(chunk.stop_reason) == "max_tokens" and ctx.conv_store:
                        ctx.conv_store.append_event(
                            "system",
                            {
                                "text": (
                                    f"⚠ Response hit the {_max_output}-token output limit and was "
                                    "truncated. A tool call cut off this way is discarded. Raise "
                                    "[execution].max_output_tokens, or write large files in "
                                    "chunks with write_file then append_file."
                                )
                            },
                        )
                    turn_text = "".join(local_turn).strip()
                    local_turn = []
                    if turn_text and ctx.conv_store:
                        ctx.conv_store.append_event("text", {"text": turn_text})
                    # Auto-index completed turn text for semantic search (PRD-101).
                    if turn_text and ctx.semantic_index is not None:
                        doc_id = f"{self._intent_id}_{ctx.completed_turns}"
                        asyncio.create_task(ctx.semantic_index.add(doc_id, turn_text))

        # Snapshot after any proactive compaction, immediately before Lauren
        # adds this turn's user message.  It is the safe rollback point for the
        # reactive compact-then-retry path below.
        turn_snapshot = (
            ctx.session_memory.snapshot()
            if ctx.session_memory is not None and hasattr(ctx.session_memory, "snapshot")
            else None
        )

        try:
            # Older Lauren releases do not run the exact-count compaction
            # ladder.  This preflight is a compatibility fallback; newer
            # releases also receive context_window above and keep their exact
            # guard as the source of truth.
            if not _supports_context_guard:
                await self._auto_compact_if_needed(
                    active_runner,
                    agent_text,
                    max_input_tokens=_window_tokens,
                )
                if ctx.session_memory is not None and hasattr(ctx.session_memory, "snapshot"):
                    turn_snapshot = ctx.session_memory.snapshot()

            await self._stream_with_retry(_stream_once)
            turn_completed = True
        except (asyncio.CancelledError, KeyboardInterrupt):
            # Preserve completed sub-turns and tool results so the next user
            # message can ask what was in progress. Only an unanswered final
            # tool call is healed; rolling back to ``turn_base_count`` would
            # erase the useful context from the shared session conversation.
            _preserve_interrupted_memory(ctx.session_memory)
            if ctx.conv_store:
                ctx.conv_store.close_turn()
            raise  # must propagate so task.cancel() terminates the workflow runner
        except Exception as exc:
            # A generic 400 from older Lauren/OpenAI integrations is still
            # recoverable when its only cause is an overlong context. Restore
            # the pre-attempt memory (run_stream has already appended the user
            # message), compact once, and retry the same turn exactly once.
            # This prevents the previous behaviour: the phase loop retried an
            # identical oversized request until the user had to intervene.
            if _auto_compact and _is_context_overflow_error(exc) and turn_snapshot is not None:
                memory = ctx.session_memory
                if memory is not None and hasattr(memory, "restore"):
                    memory.restore(turn_snapshot)
                compacted = await self._auto_compact_if_needed(
                    active_runner,
                    agent_text,
                    max_input_tokens=_window_tokens,
                    force=True,
                )
                if compacted:
                    try:
                        await self._stream_with_retry(_stream_once)
                    except Exception as retry_exc:  # noqa: BLE001
                        exc = retry_exc
                    else:
                        turn_completed = True

            if turn_completed:
                return
            if ctx.conv_store:
                # Emit one well-formatted error event with the exception class name.
                # Do NOT call fail_turn/close_turn here — the finally block handles
                # state cleanup idempotently, preventing the double-fail bug.
                ctx.conv_store.append_event("error", {"message": f"{type(exc).__name__}: {exc}"})
            if _is_permanent_error(exc):
                # PRD-117: HTTP 4xx errors are structurally permanent — retrying
                # will always produce the same failure.  Re-raise so the phase
                # loop can exit immediately instead of exhausting its retry cap.
                # _stream()'s finally block still runs → close_turn() is called.
                raise
            # Transient errors that survive _stream_with_retry are swallowed here
            # (PRD-117): the phase loop re-runs the whole turn and decides.
        finally:
            if usage_tracker is not None:
                usage_tracker.finalize("cancelled" if not turn_completed else "completed")
            self._turn_active = False
            if ctx.conv_store:
                # close_turn() is idempotent — safe even when CancelledError path
                # already called it above.
                ctx.conv_store.close_turn()
            # PRD-129/156: only a natural provider run is completed.  Explicit
            # cancellation closes with an abort marker after rolling back the
            # incomplete message tail, while a hard process death leaves the
            # turn_started marker open for crash recovery.
            if _journal is not None:
                try:
                    if turn_completed:
                        _journal.turn_completed(self._intent_id)
                    else:
                        _journal.turn_aborted(self._intent_id, reason="cancelled-or-failed")
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

    async def _emit_retry(
        self, attempt: int, max_retries: int, delay: float, exc: BaseException
    ) -> None:
        """Observability + user notification for a scheduled transport retry."""
        ctx = self._ctx
        if ctx.conv_store is not None:
            ctx.conv_store.append_event(
                "system",
                {
                    "text": f"⟳ Network error — retrying ({attempt}/{max_retries})…",
                },
            )
        import logging as _logging  # noqa: PLC0415

        _logging.getLogger(__name__).warning(
            "Transient network error on attempt %d/%d, retrying in %.1fs: %s: %s",
            attempt,
            max_retries,
            delay,
            type(exc).__name__,
            exc,
        )
        if ctx.processor is not None:
            from agenthicc.kernel import Event  # noqa: PLC0415

            await ctx.processor.emit(
                Event.create(
                    "TransportRetryScheduled",
                    {
                        "scope": "agent_turn",
                        "attempt": attempt,
                        "max_retries": max_retries,
                        "delay_s": delay,
                        "error_type": type(exc).__name__,
                    },
                )
            )

    # ── step 9: kernel completion event ───────────────────────────────────────

    async def _emit_intent_complete(self, status: str = "complete") -> None:
        from agenthicc.kernel import Event  # noqa: PLC0415

        await self._ctx.processor.emit(
            Event.create(
                "IntentStatusChanged",
                {
                    "intent_id": self._intent_id,
                    "status": status,
                },
            )
        )


# ── compatibility shim ────────────────────────────────────────────────────────


async def _run_agent_turn(
    text: str,
    runner: AgentRunnerBase,
    processor: EventProcessor,
    session_memory: ShortTermMemory | None = None,
    conversation_id: str = "",
    max_agent_turns: int = 200,
    conv_store: ConversationStore | None = None,
    app_state: AppState | None = None,
    exec_cfg: ExecutionSettings | None = None,
    skills: dict[str, SkillDef] | None = None,
    mention_cache: MentionCache | None = None,
    project_plugin_tools: list[ToolLike] | None = None,
    mcp_registry: McpToolRegistry | None = None,
    active_agent: str | None = None,
    completed_turns: int = 0,
    approval_svc: ApprovalService | None = None,
    output_collector: list[str] | None = None,
    system_prompt_suffix: str = "",
    excluded_capabilities: frozenset[str] = frozenset(),
    allowed_tool_names: frozenset[str] | None = None,
    memory_router: MemoryRouter | None = None,
    semantic_index: SemanticIndex | None = None,
    skill_permissions: SkillPermissionSet | None = None,
    retry_deadline_monotonic: float | None = None,
    resume_turn_id: str | None = None,
    resume_ledger: IdempotencyLedger | None = None,
    command_outcomes: list[dict[str, object]] | None = None,
    next_queued_message: Callable[[], str | None] | None = None,
    usage_ledger: UsageLedger | None = None,
    browser_manager: object | None = None,
) -> None:
    """Thin shim — constructs AgentTurnContext and delegates to AgentTurnRunner.

    All existing call sites continue to work without modification.
    """
    ctx = AgentTurnContext(
        text=text,
        runner=runner,
        processor=processor,
        session_memory=session_memory,
        conversation_id=conversation_id,
        max_agent_turns=max_agent_turns,
        conv_store=conv_store,
        app_state=app_state,
        exec_cfg=exec_cfg,
        skills=skills,
        skill_permissions=skill_permissions,
        mention_cache=mention_cache,
        project_plugin_tools=project_plugin_tools,
        mcp_registry=mcp_registry,
        active_agent=active_agent,
        completed_turns=completed_turns,
        approval_svc=approval_svc,
        output_collector=output_collector,
        system_prompt_suffix=system_prompt_suffix,
        excluded_capabilities=excluded_capabilities,
        allowed_tool_names=allowed_tool_names,
        memory_router=memory_router,
        semantic_index=semantic_index,
        retry_deadline_monotonic=retry_deadline_monotonic,
        resume_turn_id=resume_turn_id,
        resume_ledger=resume_ledger,
        command_outcomes=command_outcomes,
        next_queued_message=next_queued_message,
        usage_ledger=usage_ledger,
        browser_manager=browser_manager,
    )
    await AgentTurnRunner(ctx).run()
