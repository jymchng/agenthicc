"""Agenthicc adapter around lauren-ai's canonical tool executor.

Lauren-ai owns dispatch, hook ordering, approval signals, context injection,
and provider-facing result semantics. This module registers native lauren
``@tool()`` callables/instances alongside legacy Agenthicc ``Tool`` objects and
exposes a small envelope with Agenthicc identity, timing, and catalog metadata.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from enum import Enum
from typing import TYPE_CHECKING

from lauren_ai import ToolCall, ToolContext, ToolResult as LaurenToolResult
from lauren_ai._tools import TOOL_META, ToolMeta, ToolSchema
from lauren_ai._tools._executor import (
    ToolExecutor as LaurenToolExecutor,
    ToolExecutionError,
    ToolPendingApprovalSignal,
)

from agenthicc.tools.base import Tool, ToolBase, ToolResult, ToolResultEnvelope
from agenthicc.tools.capabilities import CAPABILITIES_KEY, get_tool_capabilities
from agenthicc.tools.context import ToolCallContext
from agenthicc.tools.sandbox import ToolSandbox

if TYPE_CHECKING:
    from agenthicc.tools.hooks import ToolHook

__all__ = [
    "ApprovalDecision",
    "AgenthiccToolExecutor",
    "ToolErrorKind",
    "ToolMetadata",
    "ToolExecutor",
    "normalize_result",
]


class ToolErrorKind(str, Enum):
    """Stable error categories exposed by the Agenthicc envelope."""

    unknown = "unknown"
    denied = "denied"
    approval_required = "approval_required"
    timeout = "timeout"
    network = "network"
    provider = "provider"
    execution = "execution"


class ApprovalDecision(str, Enum):
    """Decision returned by an optional Agenthicc-side approval callback."""

    approved = "approved"
    denied = "denied"


@dataclass(frozen=True, slots=True)
class ToolMetadata:
    """Catalog metadata independent of lauren-ai's internal ``ToolMeta``."""

    name: str
    description: str
    parameters: dict[str, object]
    capabilities: frozenset[str] = frozenset()
    source: str = "unknown"
    destructive: bool = False
    requires_approval: bool = False
    timeout_s: float = 30.0
    max_retries: int = 0


ApprovalHandler = Callable[
    [ToolMetadata, ToolCallContext],
    ApprovalDecision | bool | Awaitable[ApprovalDecision | bool],
]


@dataclass(frozen=True, slots=True)
class _RegisteredTool:
    implementation: object
    metadata: ToolMetadata
    lauren_meta: ToolMeta


def normalize_result(raw: object) -> ToolResult:
    """Normalize legacy and lauren result shapes for adapter hooks/tests."""
    if isinstance(raw, ToolResult):
        return raw
    if isinstance(raw, ToolResultEnvelope):
        return ToolResult(
            ok=raw.ok,
            value=raw.value,
            error=raw.error,
            duration_ms=raw.duration_ms,
            error_kind=raw.error_kind,
        )
    if isinstance(raw, LaurenToolResult):
        content = raw.content
        if raw.is_error:
            return ToolResult.failure(str(content), error_kind=ToolErrorKind.execution.value)
        if isinstance(content, str):
            try:
                decoded = json.loads(content)
            except (TypeError, ValueError):
                decoded = None
            if decoded is not None:
                if isinstance(decoded, dict) and decoded.get("ok") is False:
                    error = str(decoded.get("error", "tool returned an error"))
                    return ToolResult.failure(error, error_kind=_result_error_kind(error).value)
                return ToolResult.success(decoded)
        return ToolResult.success(content)
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except (TypeError, ValueError):
            decoded = None
        if decoded is not None:
            if isinstance(decoded, dict) and decoded.get("ok") is False:
                error = str(decoded.get("error", "tool returned an error"))
                return ToolResult.failure(error, error_kind=_result_error_kind(error).value)
            return ToolResult.success(decoded)
    if isinstance(raw, dict) and raw.get("ok") is False:
        error = str(raw.get("error", "tool returned an error"))
        return ToolResult.failure(error, error_kind=_result_error_kind(error).value)
    return ToolResult.success(raw)


class AgenthiccToolExecutor:
    """Register and execute native lauren tools and legacy Tool objects."""

    def __init__(
        self,
        tools: Iterable[object] | None = None,
        *,
        sandbox: ToolSandbox | None = None,
        global_hooks: list["ToolHook"] | None = None,
        approval_handler: ApprovalHandler | None = None,
        event_sink: object | None = None,
        default_timeout_s: float = 30.0,
    ) -> None:
        self._tools: dict[str, _RegisteredTool] = {}
        self._sandbox = sandbox
        self._approval_handler = approval_handler
        self._event_sink = event_sink
        self._default_timeout_s = default_timeout_s
        self._global_hooks = list(global_hooks or [])
        if tools is not None:
            for tool in tools:
                self.register(tool)

    @property
    def names(self) -> list[str]:
        """Return registered names in insertion order."""
        return list(self._tools)

    def register(
        self,
        tool: object,
        *,
        name: str | None = None,
        source: str = "unknown",
        capabilities: frozenset[str] | None = None,
        destructive: bool | None = None,
        requires_approval: bool | None = None,
        timeout_s: float | None = None,
        max_retries: int = 0,
    ) -> ToolMetadata:
        """Register a native lauren tool or an Agenthicc Tool object."""
        registered, metadata = self._prepare(
            tool,
            name=name,
            source=source,
            capabilities=capabilities,
            destructive=destructive,
            requires_approval=requires_approval,
            timeout_s=timeout_s,
            max_retries=max_retries,
        )
        self._tools[metadata.name] = registered
        return metadata

    def get_metadata(self, name: str) -> ToolMetadata | None:
        """Return catalog metadata for a registered name."""
        entry = self._tools.get(name)
        return entry.metadata if entry is not None else None

    def catalog(self) -> list[dict[str, object]]:
        """Return generated tool catalog records for prompts and diagnostics."""
        return [
            {
                "name": entry.metadata.name,
                "description": entry.metadata.description,
                "capabilities": sorted(entry.metadata.capabilities),
                "source": entry.metadata.source,
                "destructive": entry.metadata.destructive,
                "requires_approval": entry.metadata.requires_approval,
                "timeout_s": entry.metadata.timeout_s,
                "parameters": entry.metadata.parameters,
            }
            for entry in self._tools.values()
        ]

    async def execute(
        self,
        tool_name: str,
        args: dict[str, object],
        tool_use_id: str = "",
        context: ToolCallContext | None = None,
    ) -> ToolResultEnvelope:
        """Execute one call through lauren-ai and normalize its envelope."""
        entry = self._tools.get(tool_name)
        started = time.perf_counter()
        if entry is None:
            result = ToolResultEnvelope(
                tool_call_id=tool_use_id,
                tool_name=tool_name,
                ok=False,
                error=f"Unknown tool: '{tool_name}'",
                duration_ms=0.0,
                error_kind=ToolErrorKind.unknown.value,
            )
            await self._emit_complete(result)
            return result

        base_context = context or ToolCallContext(
            agent_context=None,
            tool_use_id=tool_use_id,
            turn=0,
            metadata={},
            state={},
            tool_state={},
            dependencies={},
            extras={},
            tool_name=tool_name,
            tool_input=dict(args),
        )
        call_context = ToolCallContext(
            agent_context=base_context.agent_context,
            tool_use_id=tool_use_id,
            turn=base_context.turn,
            metadata=dict(base_context.metadata),
            state=dict(base_context.state),
            tool_state=dict(base_context.tool_state),
            dependencies=dict(base_context.dependencies),
            extras=dict(base_context.extras),
            tool_name=tool_name,
            tool_input=dict(args),
        )
        call_context.metadata.update(
            {
                CAPABILITIES_KEY: entry.metadata.capabilities,
                "source": entry.metadata.source,
                "destructive": entry.metadata.destructive,
            }
        )
        if self._sandbox is not None:
            call_context.extras["sandbox"] = self._sandbox
            if self._sandbox.workspace is not None:
                call_context.extras["workspace"] = self._sandbox.workspace
                call_context.extras["workspace_root"] = str(self._sandbox.workspace.root)
            call_context.extras["network"] = self._sandbox.network

        await self._emit_started(tool_name, tool_use_id, call_context)
        result: ToolResultEnvelope | None = None
        lauren_executor = self._lauren_executor()
        call = ToolCall(tool_use_id=tool_use_id, name=tool_name, input=dict(args))
        try:
            raw = await lauren_executor.execute(call, call_context)
            result = self._success_result(tool_name, tool_use_id, started, raw)
        except ToolPendingApprovalSignal as exc:
            if self._approval_handler is None:
                result = self._failed_result(
                    tool_name,
                    tool_use_id,
                    started,
                    str(exc),
                    ToolErrorKind.approval_required,
                )
            else:
                call_context.tool_input = dict(exc.tool_input)
                decision = self._approval_handler(entry.metadata, call_context)
                resolved = await decision if inspect.isawaitable(decision) else decision
                if resolved is False or resolved == ApprovalDecision.denied:
                    result = self._failed_result(
                        tool_name,
                        tool_use_id,
                        started,
                        "Permission denied by approval policy.",
                        ToolErrorKind.denied,
                    )
                else:
                    approved_call = ToolCall(
                        tool_use_id=tool_use_id,
                        name=tool_name,
                        input=dict(exc.tool_input),
                    )
                    raw = await lauren_executor.execute_approved(
                        approved_call,
                        call_context,
                        approved_input=dict(exc.tool_input),
                    )
                    result = self._success_result(tool_name, tool_use_id, started, raw)
        except asyncio.TimeoutError:
            result = self._failed_result(
                tool_name,
                tool_use_id,
                started,
                f"Tool '{tool_name}' timed out after {entry.metadata.timeout_s:.3g}s.",
                ToolErrorKind.timeout,
            )
        except Exception as exc:  # noqa: BLE001
            original = exc.original if isinstance(exc, ToolExecutionError) else exc
            kind = _classify_error(original)
            result = self._failed_result(tool_name, tool_use_id, started, str(original), kind)
        finally:
            if result is not None:
                await self._emit_complete(result)
        if result is None:
            raise RuntimeError("lauren-ai executor returned without a result")
        return result

    async def execute_parallel(
        self,
        calls: Sequence[tuple[str, dict[str, object], str]],
        context: ToolCallContext | None = None,
    ) -> list[ToolResultEnvelope]:
        """Execute calls concurrently while preserving input order."""
        return list(
            await asyncio.gather(
                *(self.execute(name, args, call_id, context) for name, args, call_id in calls)
            )
        )

    def _lauren_executor(self) -> LaurenToolExecutor:
        tools = {
            name: (entry.implementation, entry.lauren_meta) for name, entry in self._tools.items()
        }
        return LaurenToolExecutor(tools, global_hooks=self._global_hooks)

    def _prepare(
        self,
        tool: object,
        *,
        name: str | None,
        source: str,
        capabilities: frozenset[str] | None,
        destructive: bool | None,
        requires_approval: bool | None,
        timeout_s: float | None,
        max_retries: int,
    ) -> tuple[_RegisteredTool, ToolMetadata]:
        original_meta = getattr(tool, TOOL_META, None)
        native_instance = original_meta is not None and callable(getattr(tool, "run", None))
        if not isinstance(tool, (Tool, ToolBase)) and not callable(tool) and not native_instance:
            raise TypeError(f"Unsupported tool registration: {tool!r}")
        tool_name, description, parameters = _describe(tool, name)
        caps = capabilities if capabilities is not None else _capabilities(tool)
        if isinstance(tool, ToolBase):
            is_destructive = destructive if destructive is not None else tool.destructive
            needs_approval = (
                requires_approval if requires_approval is not None else tool.requires_approval
            )
        else:
            is_destructive = (
                destructive
                if destructive is not None
                else bool(caps & {"write", "execute", "git_write", "network"})
            )
            needs_approval = requires_approval if requires_approval is not None else False
            if isinstance(original_meta, ToolMeta) and requires_approval is None:
                needs_approval = original_meta.requires_confirmation
        deadline = timeout_s if timeout_s is not None else self._default_timeout_s
        metadata = ToolMetadata(
            name=tool_name,
            description=description,
            parameters=parameters,
            capabilities=caps,
            source=source,
            destructive=is_destructive,
            requires_approval=needs_approval,
            timeout_s=deadline,
            max_retries=max_retries,
        )

        if isinstance(tool, Tool) or isinstance(tool, ToolBase):
            implementation = self._legacy_adapter(tool, deadline)
            lauren_meta = _manual_lauren_meta(
                metadata,
                requires_confirmation=needs_approval,
            )
        elif original_meta is not None:
            implementation = self._callable_adapter(
                tool,
                deadline,
                context_param_name=original_meta.context_param_name,
            )
            lauren_meta = replace(
                original_meta,
                name=tool_name,
                description=description,
                parameters=parameters,
                is_async=True,
                reads_context=True,
                context_param_name="ctx",
                requires_confirmation=needs_approval,
            )
        elif callable(tool):
            implementation = self._callable_adapter(tool, deadline)
            lauren_meta = _manual_lauren_meta(
                metadata,
                requires_confirmation=needs_approval,
            )
        else:
            raise TypeError(f"Unsupported tool registration: {tool!r}")
        return _RegisteredTool(implementation, metadata, lauren_meta), metadata

    def _legacy_adapter(self, tool: ToolBase, timeout_s: float) -> Callable[..., Awaitable[object]]:
        async def _adapter(ctx: ToolContext, **kwargs: object) -> object:
            if isinstance(tool, Tool):
                result = tool.execute(dict(kwargs), _legacy_context(ctx))
            else:
                result = tool.execute(ctx, dict(kwargs))
            if inspect.isawaitable(result):
                result = await self._run_with_limits(result, timeout_s)
            return _unwrap_agenthicc_result(result)

        return _adapter

    def _callable_adapter(
        self,
        tool: object,
        timeout_s: float,
        *,
        context_param_name: str | None = None,
    ) -> Callable[..., Awaitable[object]]:
        async def _adapter(ctx: ToolContext, **kwargs: object) -> object:
            call_kwargs = dict(kwargs)
            fn = getattr(tool, "run", tool)
            context_name = context_param_name or _context_parameter(fn)
            if context_name is not None:
                call_kwargs[context_name] = ctx
            if inspect.iscoroutinefunction(fn):
                result = fn(**call_kwargs)
                result = await self._run_with_limits(result, timeout_s)
            else:
                result = await self._run_with_limits(
                    asyncio.to_thread(fn, **call_kwargs),
                    timeout_s,
                )
            return _unwrap_agenthicc_result(result)

        return _adapter

    async def _run_with_limits(
        self,
        operation: Awaitable[object],
        timeout_s: float,
    ) -> object:
        if self._sandbox is not None:
            return await self._sandbox.run(operation, timeout_s)
        return await asyncio.wait_for(operation, timeout=timeout_s)

    def _success_result(
        self,
        tool_name: str,
        tool_use_id: str,
        started: float,
        raw: LaurenToolResult,
    ) -> ToolResultEnvelope:
        duration = round((time.perf_counter() - started) * 1000, 3)
        if raw.is_error:
            return ToolResultEnvelope(
                tool_call_id=tool_use_id,
                tool_name=tool_name,
                ok=False,
                error=str(raw.content),
                duration_ms=duration,
                error_kind=ToolErrorKind.execution.value,
            )
        normalized = normalize_result(raw.content)
        if not normalized.ok:
            return ToolResultEnvelope(
                tool_call_id=tool_use_id,
                tool_name=tool_name,
                ok=False,
                error=normalized.error,
                duration_ms=duration,
                error_kind=normalized.error_kind or ToolErrorKind.execution.value,
            )
        return ToolResultEnvelope(
            tool_call_id=tool_use_id,
            tool_name=tool_name,
            ok=True,
            value=normalized.value,
            duration_ms=duration,
        )

    def _failed_result(
        self,
        tool_name: str,
        tool_use_id: str,
        started: float,
        error: str,
        kind: ToolErrorKind,
    ) -> ToolResultEnvelope:
        result = ToolResultEnvelope(
            tool_call_id=tool_use_id,
            tool_name=tool_name,
            ok=False,
            error=error,
            duration_ms=round((time.perf_counter() - started) * 1000, 3),
            error_kind=kind.value,
        )
        return result

    async def _emit_started(
        self,
        tool_name: str,
        tool_use_id: str,
        context: ToolCallContext,
    ) -> None:
        from lauren_ai._signals import ToolCallStarted  # noqa: PLC0415

        await _emit_signal(
            self._event_sink,
            ToolCallStarted(
                tool_name=tool_name,
                tool_use_id=tool_use_id,
                agent_id=getattr(context.agent_context, "agent_id", None),
                input=dict(context.tool_input),
            ),
        )

    async def _emit_complete(self, result: ToolResultEnvelope) -> None:
        from lauren_ai._signals import ToolCallComplete  # noqa: PLC0415

        await _emit_signal(
            self._event_sink,
            ToolCallComplete(
                tool_name=result.tool_name,
                tool_use_id=result.tool_call_id,
                duration_ms=result.duration_ms,
                success=result.ok,
                error=result.error,
            ),
        )


ToolExecutor = AgenthiccToolExecutor


def _describe(tool: object, explicit_name: str | None) -> tuple[str, str, dict[str, object]]:
    meta = getattr(tool, TOOL_META, None)
    name = explicit_name or str(getattr(meta, "name", "") or getattr(tool, "name", ""))
    if not name:
        name = getattr(tool, "__name__", "")
    if not name:
        raise ValueError(f"Tool has no name: {tool!r}")
    doc_lines = (getattr(tool, "__doc__", "") or "").strip().splitlines()
    description = str(
        getattr(meta, "description", "")
        or getattr(tool, "description", "")
        or (doc_lines[0] if doc_lines else "")
        or name
    )
    parameters = getattr(meta, "parameters", None) or getattr(tool, "parameters", {})
    return name, description, dict(parameters) if isinstance(parameters, dict) else {}


def _capabilities(tool: object) -> frozenset[str]:
    values = get_tool_capabilities(tool)
    return frozenset(str(getattr(value, "value", value)) for value in values)


def _manual_lauren_meta(
    metadata: ToolMetadata,
    *,
    requires_confirmation: bool,
) -> ToolMeta:
    return ToolMeta(
        name=metadata.name,
        description=metadata.description,
        parameters=ToolSchema(
            name=metadata.name,
            description=metadata.description,
            input_schema=metadata.parameters,
        ),
        is_async=True,
        reads_context=True,
        context_param_name="ctx",
        requires_confirmation=requires_confirmation,
    )


def _legacy_context(ctx: ToolContext) -> dict[str, object]:
    context = {
        "tool_name": ctx.tool_name,
        "tool_use_id": ctx.tool_use_id,
        "tool_call_id": ctx.tool_use_id,
        "agent_context": ctx.agent_context,
        "metadata": ctx.metadata,
        "state": ctx.state,
        "tool_state": ctx.tool_state,
        "dependencies": ctx.dependencies,
        "extras": ctx.extras,
    }
    context.update(ctx.extras)
    return context


def _context_parameter(tool: object) -> str | None:
    """Find the context parameter in an undecorated callable."""
    try:
        parameters = inspect.signature(tool).parameters
    except (TypeError, ValueError):
        return None
    for name, parameter in parameters.items():
        if name in {"ctx", "context", "tool_context"} or parameter.annotation is ToolContext:
            return name
    return None


def _unwrap_agenthicc_result(result: object) -> object:
    if isinstance(result, ToolResult):
        if result.ok:
            return result.value
        return {"ok": False, "error": result.error or "tool failed"}
    return result


def _classify_error(exc: BaseException) -> ToolErrorKind:
    from agenthicc.tools.http import is_network_error  # noqa: PLC0415

    if isinstance(exc, asyncio.TimeoutError):
        return ToolErrorKind.timeout
    if isinstance(exc, PermissionError):
        return ToolErrorKind.denied
    if is_network_error(exc):
        return ToolErrorKind.network
    module = type(exc).__module__.lower()
    name = type(exc).__name__.lower()
    if any(
        token in module or token in name for token in ("anthropic", "openai", "provider", "mcp")
    ):
        return ToolErrorKind.provider
    return ToolErrorKind.execution


def _result_error_kind(error: str) -> ToolErrorKind:
    """Classify a structured ``{"ok": false}`` result without rethrowing it."""
    lowered = error.lower()
    if any(
        token in lowered for token in ("permission", "denied", "forbidden", "outside workspace")
    ):
        return ToolErrorKind.denied
    if "timeout" in lowered or "timed out" in lowered:
        return ToolErrorKind.timeout
    if any(token in lowered for token in ("network", "connection", "dns")):
        return ToolErrorKind.network
    if any(token in lowered for token in ("provider", "anthropic", "openai", "mcp")):
        return ToolErrorKind.provider
    return ToolErrorKind.execution


async def _emit_signal(sink: object | None, signal: object) -> None:
    if sink is None:
        return
    try:
        emit = getattr(sink, "emit", None)
        if callable(emit):
            result = emit(signal)
            if inspect.isawaitable(result):
                await result
    except Exception:  # noqa: BLE001
        return
