"""AgentTurnRunner — executes one agent turn (PRD-92).

``AgentTurnContext`` (see agent_turn_context.py) carries all configuration.
``AgentTurnRunner`` executes it via composable private methods, each
independently testable.

``_run_agent_turn`` is kept as a thin compatibility shim so all existing
call sites continue to work without modification.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
import logging
import os
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypeVar, cast

from agenthicc.runners.agent_turn_context import AgentTurnContext
from agenthicc.runners.prompt_contract import PromptBlock
from agenthicc.runners.recovery_projection import ToolRecoveryProjector
from agenthicc.memory.tool_history import repair_non_adjacent_tool_history
from agenthicc.tools.base import tool_name
from agenthicc.tools.hooks import (
    AfterToolHookDecision,
    ErrorToolHookDecision,
    LifecycleHook,
    ToolCallContext,
)

logger = logging.getLogger(__name__)
_T = TypeVar("_T")


def _message_field(message: object, name: str, default: object = None) -> object:
    if isinstance(message, Mapping):
        return message.get(name, default)
    return getattr(message, name, default)


def _has_trailing_user_message(messages: list[object], text: str) -> bool:
    if not messages:
        return False
    last = messages[-1]
    return (
        _message_field(last, "role", "") == "user" and _message_field(last, "content", None) == text
    )


def _conservative_message_estimate(
    messages: list[object], *, system: str = "", tools: list[object] | None = None
) -> int:
    """Estimate request tokens with safety headroom for provider framing."""
    from lauren_ai._transport import ToolSchema, estimate_message_tokens

    baseline = estimate_message_tokens(
        messages,
        system=system or None,
        tools=cast("list[ToolSchema] | None", tools),
    )
    # The transport heuristic is 4 chars/token. A 25% margin corresponds to
    # ~3.2 chars/token and protects non-English/code-heavy contexts.
    return max(1, (baseline * 5 + 3) // 4 + 32 * len(messages))


def _execution(config: ExecutionSettings | None) -> ExecutionSettings:
    """Return the turn's execution settings; a turn built without them uses the defaults."""
    if config is not None:
        return config
    from agenthicc.config import ExecutionSettings

    return ExecutionSettings()


def _optional_attribute(value: object | None, name: str, default: _T) -> _T:
    """Read an optional attribute without weakening the static type surface."""
    if value is None:
        return default
    try:
        return cast(_T, value.__getattribute__(name))
    except AttributeError:
        try:
            dynamic_getter = object.__getattribute__(value, "__getattr__")
            return cast(_T, dynamic_getter(name))
        except AttributeError:
            return default


def _require_tool_transaction_api(memory: object | None) -> None:
    """Fail closed when an unsafe lauren-ai runtime is installed.

    PRD-169 deliberately does not keep a second result-correlation
    implementation in agenthicc. A missing shared contract is therefore an
    actionable compatibility error, not a reason to silently use the legacy
    serializer path.
    """
    required = (
        "validate_tool_history",
        "repair_tool_history",
        "begin_tool_exchange",
        "commit_tool_exchange",
        "abort_tool_exchange",
    )
    missing = [name for name in required if not callable(getattr(memory, name, None))]
    if missing:
        try:
            import lauren_ai

            installed = getattr(lauren_ai, "__version__", "unknown")
        except ImportError:
            installed = "not installed"
        missing_text = ", ".join(missing)
        raise RuntimeError(
            "agenthicc requires a transaction-capable lauren-ai release for "
            f"PRD-169; installed={installed!r}, missing={missing_text}. "
            "Upgrade lauren-ai before starting an agent turn."
        )


def _read_text_if_exists(path: str) -> str:
    """Read a file for the edit-preview adapter, returning empty when absent."""
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _preserve_interrupted_memory(memory: object | None) -> bool:
    """Keep valid completed context while healing an interrupted tool tail.

    Cancelling a turn must not roll the shared session conversation back to its
    pre-turn count: that would erase the tool calls and assistant reasoning the
    next user message needs. The memory boundary heals any unanswered final
    tool call into a provider-valid interruption result and, when supported,
    persists that repair in the session journal.
    """
    if memory is None:
        return False
    from lauren_ai._memory import ShortTermMemory as _ShortTermMemory

    if not isinstance(memory, _ShortTermMemory):
        # Older/lightweight SessionContext implementations may expose an
        # append-only memory facade. There is no provider-history repair to do
        # unless the object implements Lauren-ai's canonical memory contract.
        return False
    if repair_non_adjacent_tool_history(memory):
        return True
    before = copy.deepcopy(memory._messages)
    from agenthicc.memory.journaled import JournaledShortTermMemory

    if isinstance(memory, JournaledShortTermMemory):
        return memory.ensure_valid_and_persist()
    memory.ensure_valid()
    return before != memory._messages


# ── Permanent-error detection (PRD-117) ───────────────────────────────────────


def _http_status_code(exc: BaseException) -> int | None:
    """Return the HTTP status code carried by *exc*, or ``None``.

    Checks the exception itself and then its chained ``__cause__`` /
    ``__context__`` — necessary for SDK wrappers like ``TransportError``
    that wrap an inner ``BadRequestError`` which holds the real status.
    """
    for candidate in (exc, exc.__cause__, exc.__context__):
        if candidate is None:
            continue
        code = getattr(candidate, "status_code", None)
        if isinstance(code, int):
            return code
    return None


def _exception_chain(exc: BaseException) -> tuple[BaseException, ...]:
    """Return the short, identity-deduplicated provider exception chain."""
    chain: list[BaseException] = []
    pending: list[BaseException | None] = [exc]
    seen: set[int] = set()
    while pending and len(chain) < 8:
        candidate = pending.pop(0)
        if candidate is None or id(candidate) in seen:
            continue
        seen.add(id(candidate))
        chain.append(candidate)
        pending.extend(
            (
                candidate.__cause__,
                candidate.__context__,
            )
        )
    return tuple(chain)


def _safe_provider_identifier(value: object) -> str | None:
    """Bound provider metadata to a small non-secret identifier alphabet."""
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return None
    text = re.sub(r"[^A-Za-z0-9_.:-]", "", str(value))[:128]
    return text or None


def _provider_attribute(value: object | None, name: str) -> object | None:
    """Read optional SDK exception metadata through the typed attribute adapter."""
    return _optional_attribute(value, name, None)


def _provider_error_identifiers(exc: BaseException) -> dict[str, str]:
    """Extract bounded type/code/request IDs without serializing response bodies."""
    chain = _exception_chain(exc)
    error_type: str | None = None
    error_code: str | None = None
    request_id: str | None = None
    for candidate in chain:
        if error_type is None:
            for name in ("error_type", "type"):
                error_type = _safe_provider_identifier(_provider_attribute(candidate, name))
                if error_type:
                    break
        if error_code is None:
            error_code = _safe_provider_identifier(_provider_attribute(candidate, "code"))
        if request_id is None:
            for name in ("request_id", "requestId", "x_request_id"):
                request_id = _safe_provider_identifier(_provider_attribute(candidate, name))
                if request_id:
                    break
        response = _provider_attribute(candidate, "response")
        headers = _provider_attribute(response, "headers")
        if request_id is None and isinstance(headers, Mapping):
            request_id = _safe_provider_identifier(
                headers.get("x-request-id") or headers.get("request-id")
            )

    raw = " ".join(f"{type(item).__name__} {item}" for item in chain)
    if error_type is None:
        types = re.findall(r"[\"'](?:error_type|type)[\"']\s*:\s*[\"']([^\"']+)", raw)
        error_type = next(
            (
                identifier
                for value in reversed(types)
                if (identifier := _safe_provider_identifier(value))
                and identifier.casefold() != "error"
            ),
            None,
        )
    if error_code is None:
        match = re.search(r"[\"']code[\"']\s*:\s*[\"']([^\"']+)", raw)
        if match:
            error_code = _safe_provider_identifier(match.group(1))
    if request_id is None:
        match = re.search(
            r"(?:x-request-id|request[_-]?id)[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9_.:-]+)",
            raw,
            re.IGNORECASE,
        )
        if match:
            request_id = _safe_provider_identifier(match.group(1))

    result: dict[str, str] = {}
    if error_type:
        result["provider_error_type"] = error_type
    if error_code:
        result["provider_error_code"] = error_code
    if request_id:
        result["provider_request_id"] = request_id
    return result


def _provider_failure_metadata(
    exc: BaseException,
    ctx: AgentTurnContext,
    *,
    provider: str,
    model: str,
    turn_id: str,
) -> dict[str, str | int]:
    """Build a bounded error projection without retaining provider payloads.

    Provider messages often embed request fragments, user content, or secrets.
    Persist only identifiers and fixed execution-state fields; never exception
    text, headers, prompts, or serialized request/response bodies.
    """
    metadata: dict[str, str | int] = {
        "provider": _safe_provider_identifier(provider) or "unknown",
        "model": _safe_provider_identifier(model) or "unknown",
        "session_id": _safe_provider_identifier(ctx.conversation_id) or "unknown",
        "turn_id": _safe_provider_identifier(turn_id or ctx.resume_turn_id or "") or "unknown",
        "exception_type": _safe_provider_identifier(type(exc).__name__) or "unknown",
        "phase": "agent_turn",
    }
    profile = _safe_provider_identifier(_provider_attribute(ctx.exec_cfg, "profile"))
    metadata["profile"] = profile or "default"
    status = _http_status_code(exc)
    if status is not None:
        metadata["http_status"] = status
    metadata.update(_provider_error_identifiers(exc))

    context = ctx.provider_diagnostic_context or {}
    allowed_context_fields = {
        "background_attempt",
        "interrupted_turn_id",
        "input_message_id",
        "phase",
    }
    for key in allowed_context_fields:
        value = context.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            metadata[key] = max(0, value)
        elif isinstance(value, str):
            safe_value = _safe_provider_identifier(value)
            if safe_value:
                metadata[key] = safe_value
    return metadata


def _network_retry_reason(exc: BaseException) -> str:
    """Return a short user-facing category for a transient provider error."""
    status = _http_status_code(exc)
    raw = f"{type(exc).__name__} {exc}".casefold()

    if "timeout" in raw:
        return "Request timed out"
    if "unavailable" in raw or "overloaded" in raw or (status is not None and 500 <= status < 600):
        return "Provider temporarily unavailable"
    if status == 429 or "rate_limit" in raw or "rate limit" in raw:
        return "Rate limit reached"
    if any(term in raw for term in ("connect", "connection", "remote disconnected")):
        return "Connection interrupted"
    return "Temporary network problem"


def _network_retry_detail(exc: BaseException) -> str:
    """Extract a bounded provider message without exposing wrapper reprs.

    SDK exception strings commonly contain a useful nested ``message`` field
    followed by transport/provider metadata.  Only that field (or a short
    plain exception message) is sent to the UI; the complete exception remains
    available through debug logging.
    """
    raw = " ".join(str(exc).split())
    if not raw:
        return ""

    match = re.search(r"[\"']message[\"']\s*:\s*[\"']([^\"']+)[\"']", raw)
    if match:
        return match.group(1).strip()[:240]

    if len(raw) <= 160 and not any(
        marker in raw.casefold() for marker in ("provider=", "caused by:", "status_code=")
    ):
        return raw
    return ""


def _missing_opencode_session_diagnostic(exc: BaseException) -> str | None:
    """Return a bounded diagnostic when OpenCode Go rejects session identity."""
    status = _http_status_code(exc)
    raw = " ".join(f"{type(item).__name__} {item}" for item in _exception_chain(exc)).casefold()
    structured_type = "missingsessionid" in raw.replace("_", "").replace("-", "")
    missing_header = "x-opencode-session" in raw and "missing" in raw
    if status != 400 or not (structured_type or missing_header):
        return None
    return (
        "OpenCode Go reported a missing x-opencode-session even though Agenthicc binds that "
        "header to the stable conversation ID. Check that the configured provider endpoint "
        "and any proxy preserve the header."
    )


def _is_transient_network_error(exc: BaseException) -> bool:
    """Return ``True`` for transient network errors that are safe to retry.

    Checks for :class:`~lauren_ai._exceptions.TransientTransportError` and
    common timeout / connection error type names anywhere in the exception
    chain (``__cause__`` and ``__context__``).  HTTP 408, 425, 429, and 5xx
    responses are also transient.  The streaming turn retries from its latest
    safe provider-step boundary, without a destructive whole-turn rollback.

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
            "RateLimitError",
            # generic
            "NetworkError",
            "RemoteDisconnected",
        }
    )
    try:
        from lauren_ai._exceptions import TransientTransportError

        if isinstance(exc, TransientTransportError):
            return True
    except ImportError:
        pass
    status = _http_status_code(exc)
    if status in {408, 425, 429} or (status is not None and 500 <= status < 600):
        return True
    for candidate in (exc, exc.__cause__, exc.__context__):
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
        from lauren_ai import _exceptions

        AgentContextOverflowError = getattr(
            _exceptions,
            "AgentContextOverflowError",
            None,
        )
    except ImportError:
        AgentContextOverflowError = None

    try:
        from lauren_ai._exceptions import ToolConversationIntegrityError

        if isinstance(exc, ToolConversationIntegrityError):
            return True
    except ImportError:
        pass

    if AgentContextOverflowError is not None and isinstance(exc, AgentContextOverflowError):
        return True
    status = _http_status_code(exc)
    if status is None:
        return False
    # 429 is rate-limited — transient, worth waiting and retrying.
    # All other 4xx are client errors (bad model name, bad API key, …) — permanent.
    return 400 <= status < 500 and status != 429


def _is_irrecoverable_provider_error(exc: BaseException) -> bool:
    """Return whether *exc* is a provider-originated permanent HTTP error.

    ``_is_permanent_error`` also covers local integrity and context-overflow
    failures that must propagate immediately. PRD-198's extra retry budget is
    intentionally narrower: it applies only to an HTTP 4xx response from the
    provider (except 429), while preserving immediate propagation for local
    invariants and compaction failures.
    """
    status = _http_status_code(exc)
    if status is None or not 400 <= status < 500 or status == 429:
        return False
    # PRD-240: a missing required conversation header is deterministic. Retrying
    # the identical request cannot fix configuration and only delays diagnosis.
    return _missing_opencode_session_diagnostic(exc) is None


def _is_context_overflow_error(exc: BaseException) -> bool:
    """Return whether *exc* is a provider context-length rejection.

    Lauren versions before the context-window guard exposed this as a generic
    ``TransportError`` wrapping an SDK ``BadRequestError``.  Do not rely only
    on Lauren's newer ``AgentContextOverflowError``: the provider error in
    practice is often the only signal available at this integration boundary.
    """
    try:
        from lauren_ai import _exceptions

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
        current = current.__cause__ or current.__context__
    return False


if TYPE_CHECKING:
    from contextvars import Token
    from lauren_ai import AgentContext
    from lauren_ai._agents import AgentMeta
    from lauren_ai._tools import ToolResult
    from collections.abc import Awaitable, Callable
    from lauren_ai._agents._runner import AgentRunnerBase
    from lauren_ai._memory import ShortTermMemory
    from lauren_ai._transport import ToolCall
    from lauren_ai import IdempotencyLedger
    from lauren_ai._signals import ToolCallStarted, ToolCallComplete
    from agenthicc.config import ExecutionSettings, AgenthiccConfig
    from agenthicc.agents.definitions import AgentSpec
    from agenthicc.agents.registry import AgentsRegistry
    from agenthicc.subagents.types import SubagentTypeRegistry
    from agenthicc.kernel.processor import EventProcessor
    from agenthicc.memory.router import MemoryRouter
    from agenthicc.memory.vector import SemanticIndex
    from agenthicc.mentions.cache import MentionCache
    from agenthicc.mentions.parser import Mention
    from agenthicc.skills.loader import SkillDef, SkillPermissionSet
    from agenthicc.tools.approval import ApprovalBroker
    from agenthicc.tools.base import ToolLike
    from agenthicc.tools.mcp import McpToolRegistry
    from agenthicc.tools.mcp_manager import McpSessionManager
    from agenthicc.tui.conversation_store import AppState, ConversationStore
    from agenthicc.runners.usage_ledger import UsageLedger, UsageRunTracker
    from agenthicc.runners.prompt_contract import PromptContract
    from agenthicc.tools.workspace_access import WorkspaceAccessPolicy
    from agenthicc.plugins.registry import ToolRegistry


# ── primary-agent tool restrictions (AGX-09) ──────────────────────────────────


def _agent_restrictions(
    spec: AgentSpec,
    registry: ToolRegistry,
    allowed: frozenset[str] | None,
    excluded: frozenset[str],
    app_state: AppState | None,
) -> tuple[frozenset[str] | None, frozenset[str], AppState | None, str]:
    """Intersect an agent definition's restrictions with the current session."""
    from agenthicc.agents.definitions import CAPABILITY_NAMES

    if spec.tools is not None:
        names: set[str] = set()
        for pattern in spec.tools:
            expanded = registry.glob_expand(pattern)
            names.update(expanded if expanded else {pattern})
        tool_names = frozenset(names)
        allowed = tool_names if allowed is None else allowed & tool_names
    if spec.capabilities is not None:
        excluded |= frozenset({*CAPABILITY_NAMES, "undeclared"}) - frozenset(spec.capabilities)

    gate_state = app_state
    sections = [f"## AGENT: {spec.name}\n{spec.prompt}"] if spec.prompt else []
    if spec.ceiling and app_state is not None:
        from agenthicc.tui.conversation_store import AppState as PresentationState
        from agenthicc.tui.runtime.mode_manager import builtin_mode, restrict_mode

        ceiling = builtin_mode(spec.ceiling)
        restricted = restrict_mode(app_state.active_mode(), ceiling)
        if restricted is not app_state.active_mode():
            gate_state = PresentationState.create()
            gate_state.active_mode.set(restricted)
            gate_state.cli_flags = app_state.cli_flags
            excluded |= frozenset(str(item) for item in restricted.blocked_capabilities)
            if ceiling.system_prompt_suffix:
                sections.append(ceiling.system_prompt_suffix)
    return allowed, excluded, gate_state, "\n\n".join(sections)


def _visible_tools_for_agent(
    registry: ToolRegistry,
    allowed: frozenset[str] | None,
    excluded: frozenset[str],
) -> list[ToolLike]:
    """Return model-visible tools after tool-name and capability filtering."""
    from agenthicc.tools.capabilities import get_tool_capabilities

    return [
        tool
        for tool in registry.tools
        if (allowed is None or tool_name(tool) in allowed)
        and not (get_tool_capabilities(tool) & excluded)
    ]


def _effective_agent_tool_names(
    spec: AgentSpec,
    registry: ToolRegistry,
    app_state: AppState | None,
    *,
    allowed_tool_names: frozenset[str] | None = None,
    excluded_capabilities: frozenset[str] = frozenset(),
) -> tuple[str, ...]:
    """List a definition's model-visible tools against a session registry."""
    allowed, excluded, _, _ = _agent_restrictions(
        spec, registry, allowed_tool_names, excluded_capabilities, app_state
    )
    return tuple(
        sorted(tool_name(tool) for tool in _visible_tools_for_agent(registry, allowed, excluded))
    )


# ── formatting helper (module-level, unchanged) ───────────────────────────────


def _fmt_args(args: dict[str, object]) -> str:
    from rich.markup import escape as _e

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


def _workspace_result_projection(result: object) -> dict[str, object]:
    """Extract bounded, non-content workspace diagnostics from a tool result."""

    value: object = result
    if not isinstance(value, Mapping):
        try:
            value = object.__getattribute__(result, "value")
        except AttributeError:
            value = None
    if not isinstance(value, Mapping):
        return {}

    projection: dict[str, object] = {}
    for key in ("path", "source", "destination", "cwd"):
        item = value.get(key)
        if isinstance(item, str):
            projection[key] = item[:1024]
    for key in ("ok", "code", "state", "returncode", "timed_out", "cancelled"):
        item = value.get(key)
        if isinstance(item, (bool, int, float, str)):
            projection[key] = item
    return projection


class _ToolOutputCaptureHook(LifecycleHook):
    """Capture tool results for the scroll-buffer completion event."""

    def __init__(
        self,
        outputs: dict[str, tuple[list[str], int]],
        command_outcomes: list[dict[str, object]] | None = None,
        command_states: dict[str, str] | None = None,
        command_ready: dict[str, bool] | None = None,
        workspace_access: dict[str, dict[str, object]] | None = None,
    ) -> None:
        self._outputs = outputs
        self._command_outcomes = command_outcomes
        self._command_states = command_states
        self._command_ready = command_ready
        self._workspace_access = workspace_access

    async def after_tool_call(
        self,
        result: object,
        ctx: ToolCallContext,
    ) -> AfterToolHookDecision:
        self._outputs[ctx.tool_use_id] = _tool_output_preview(result)
        if self._workspace_access is not None:
            metadata = ctx.get_metadata("workspace_access")
            if isinstance(metadata, dict):
                result_projection = _workspace_result_projection(result)
                if result_projection:
                    metadata["actual_result"] = result_projection
                self._workspace_access[ctx.tool_use_id] = metadata
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


class _ToolRecoveryEventSink:
    """Translate shared recovery signals into headless/session events."""

    def __init__(self, projector: ToolRecoveryProjector) -> None:
        self._projector = projector

    async def on_signal(self, signal: object) -> None:
        if type(signal).__name__ != "ToolExchangeRepaired":
            return
        self._projector.project_repaired(signal)


class _TurnRecoveryEventSink:
    """Persist provider-step boundaries and advance the safe retry point."""

    def __init__(
        self,
        journal: object,
        conv_store: object | None,
        on_step_committed: Callable[[str], None],
        on_step_started: Callable[[str], None] | None = None,
    ) -> None:
        self._journal = journal
        self._conv_store = conv_store
        self._on_step_committed = on_step_committed
        self._on_step_started = on_step_started

    async def on_signal(self, signal: object) -> None:
        name = type(signal).__name__
        turn_id = str(_optional_attribute(signal, "run_id", "") or "")
        step_id = str(_optional_attribute(signal, "step_id", "") or "")
        if name == "AgentStepStarted":
            record = _optional_attribute(self._journal, "step_started", None)
            if callable(record):
                record(
                    turn_id,
                    step_id,
                    str(_optional_attribute(signal, "attempt_id", "") or ""),
                    step_index=int(_optional_attribute(signal, "step_index", 0) or 0),
                    base_cursor=int(_optional_attribute(self._journal, "cursor", 0) or 0),
                )
            if self._on_step_started is not None:
                self._on_step_started(step_id)
        elif name == "AgentStepRetryScheduled":
            append_event = _optional_attribute(self._conv_store, "append_event", None)
            if callable(append_event):
                append_event(
                    "system",
                    {
                        "text": (
                            "⟳ Provider step failed; retrying "
                            f"({int(_optional_attribute(signal, 'retry_number', 0) or 0)}/"
                            f"{int(_optional_attribute(signal, 'max_retries', 0) or 0)})…"
                        )
                    },
                )
        elif name == "AgentStepInterrupted":
            record = _optional_attribute(self._journal, "step_interrupted", None)
            if callable(record):
                record(
                    turn_id,
                    step_id,
                    str(_optional_attribute(signal, "attempt_id", "") or ""),
                    error_kind=str(_optional_attribute(signal, "error_kind", "error") or "error"),
                    partial_chars=int(_optional_attribute(signal, "partial_chars", 0) or 0),
                    retryable=bool(_optional_attribute(signal, "retryable", False)),
                )
            partial_text = str(_optional_attribute(signal, "partial_text", "") or "")[:16_384]
            if partial_text:
                partial_record = _optional_attribute(self._journal, "partial_fragment", None)
                if callable(partial_record):
                    try:
                        partial_record(
                            turn_id,
                            step_id=step_id,
                            attempt_id=str(_optional_attribute(signal, "attempt_id", "") or ""),
                            text=partial_text,
                        )
                    except Exception:  # noqa: BLE001 - diagnostics never stop the agent
                        logger.warning(
                            "agenthicc: failed to persist provider partial fragment",
                            exc_info=True,
                        )
                append_event = _optional_attribute(self._conv_store, "append_event", None)
                if callable(append_event):
                    try:
                        append_event(
                            "assistant_partial",
                            {"text": partial_text, "interrupted": True},
                        )
                    except Exception:  # noqa: BLE001 - diagnostics never stop the agent
                        logger.warning(
                            "agenthicc: failed to project provider partial fragment",
                            exc_info=True,
                        )
        elif name == "AgentStepCommitted":
            record = _optional_attribute(self._journal, "step_committed", None)
            if callable(record):
                record(
                    turn_id,
                    step_id,
                    step_index=int(_optional_attribute(signal, "step_index", 0) or 0),
                    cursor=int(_optional_attribute(self._journal, "cursor", 0) or 0),
                    message_count=int(_optional_attribute(signal, "message_count", 0) or 0),
                )
            self._on_step_committed(step_id)


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
        self._workspace_access: dict[str, dict[str, object]] = {}
        self._file_snapshots: dict[str, tuple[str, str]] = {}
        self._recovery_projector: ToolRecoveryProjector | None = (
            ToolRecoveryProjector(
                ctx.conv_store,
                getattr(ctx.session_memory, "journal", None),
            )
            if ctx.conv_store is not None
            else None
        )

        # Content produced during the turn.
        self._skill_suffix: str = ""
        self._runtime_dynamic_blocks: list[PromptBlock] = []
        self._cache_summary: str | None = None
        self._usage_tracker: UsageRunTracker | None = None
        self._step_recovery_supported = False
        self._safe_snapshot_provider: Callable[[], object | None] | None = None
        self._reset_attempt_output: Callable[[], None] | None = None
        self._last_committed_step = ""
        self._active_step_id = ""
        self._agent_mentions: list[Mention] = []
        self._spawn_tool: Callable[..., Awaitable[dict[str, object]]] | None = None
        self._definition_agent_errors: dict[str, str] = {}

    # ── public entry point ────────────────────────────────────────────────────

    def _project_legacy_recovery(self) -> None:
        """Project a repair found outside lauren-ai's signal bus once."""
        projector = self._recovery_projector
        if projector is None:
            return
        exchange = getattr(self._ctx.session_memory, "active_tool_exchange", None)
        exchange_id = str(getattr(exchange, "exchange_id", "") or "")
        if exchange_id:
            projector.project_legacy_repair(
                exchange_id=exchange_id,
                call_count=len(getattr(exchange, "call_ids", ()) or ()),
            )

    async def run(self) -> None:
        """Execute the full agent turn under its session workspace policy."""
        ctx = self._ctx
        workspace_access = ctx.workspace_access
        if workspace_access is None:
            from agenthicc.tools.workspace_access import current_workspace_access

            workspace_access = current_workspace_access()
        if workspace_access is not None:
            workspace_access.reset_turn_memory()
        policy_token: object | None = None
        if ctx.workspace_access is not None:
            from agenthicc.tools.workspace_access import set_current_workspace_access

            policy_token = set_current_workspace_access(ctx.workspace_access)
        try:
            await self._run_impl()
        finally:
            if policy_token is not None:
                from agenthicc.tools.workspace_access import reset_current_workspace_access

                reset_current_workspace_access(
                    cast("Token[WorkspaceAccessPolicy | None]", policy_token)
                )

    async def _run_impl(self) -> None:
        """Run the turn body after its task-local policy has been bound."""
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
        _require_tool_transaction_api(ctx.session_memory)
        self._register_signal_handlers()

        agent_text = await self._inject_mentions()
        self._inject_skills()

        agent_instance, active_runner = self._build_agent()
        agent_text = await self._run_agent_mentions(agent_text)

        # Keep cancellation active through the entire provider/tool loop. The
        # foreground command tool performs child-process cleanup before raising
        # the cancellation back to this task; without this scope it would turn
        # ESC into an ordinary ``cancelled`` tool result and the model would
        # continue with another turn.
        from agenthicc.tools.exec import _PROPAGATE_TOOL_CANCELLATION

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
        """Release browser resources without ending the session-owned tool set."""
        manager = self._ctx.browser_manager
        # A failed provider turn is recoverable: the next turn may still use
        # the same injected Playwright/CloakBrowser closures. Only the outer
        # TUI/headless session owner should call terminal ``close_session()``.
        closer = getattr(manager, "close_runtime", None)
        if not callable(closer):
            # Compatibility for third-party browser-manager implementations
            # that predate the recoverable runtime-cleanup boundary.
            closer = getattr(manager, "close_session", None)
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
        override = ctx.exec_cfg.model if ctx.exec_cfg is not None else ""
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
        from agenthicc.kernel import Event

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

        from lauren_ai._signals import (
            ToolCallStarted as _TCS,
            ToolCallComplete as _TCC,
            ToolExchangeRepaired as _TER,
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

        @signal_decorator(_TER)
        async def _on_tool_exchange_repaired(sig: object) -> None:
            if self._recovery_projector is None:
                return
            self._recovery_projector.project_repaired(sig)

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
                workspace_access = self._workspace_access.pop(tid, None)
                if workspace_access is not None:
                    payload["workspace_access"] = workspace_access
                if name in self._exploratory_tools:
                    payload["presentation"] = _exploration_presentation(name, args)
                conv.append_event("tool_complete", payload)

    # ── step 5: @mention injection ────────────────────────────────────────────

    async def _inject_mentions(self) -> str:
        """Resolve file mentions and collect ``@agent:NAME`` invocations."""
        from agenthicc.mentions.injector import (
            build_context_prefix,
            InjectionConfig,
        )
        from agenthicc.mentions.parser import agent_mentions

        ctx = self._ctx
        self._agent_mentions = agent_mentions(ctx.text)
        workspace_access = ctx.workspace_access
        if workspace_access is None:
            from agenthicc.tools.workspace_access import current_workspace_access

            workspace_access = current_workspace_access()
        mention_cfg = InjectionConfig(
            cwd=(
                workspace_access.scope.primary_root
                if workspace_access is not None
                else Path(os.getcwd())
            ),
            workspace_access=workspace_access,
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
        from agenthicc.skills.runner import (
            find_matching_skills,
            process_skill_body,
        )
        from agenthicc.skills.loader import filter_skills_for_agent

        ctx = self._ctx
        available_skills = filter_skills_for_agent(
            ctx.skills or {},
            ctx.active_agent or "default",
            ctx.skill_permissions,
        )
        matched = find_matching_skills(ctx.text, available_skills)
        if matched:
            self._skill_suffix = "\n\n---\n\n" + "\n\n".join(
                f"## Skill: {s.name}\n{process_skill_body(s, args=[])}" for s in matched
            )

    # ── step 7: build @agent class and runner ─────────────────────────────────

    def _build_agent(self) -> tuple[object, AgentRunnerBase]:
        """Construct the @agent-decorated class, populate meta.tools, build runner.

        Returns (agent_instance, active_runner).
        """
        from lauren_ai._agents import agent as agent_decorator, use_tools
        from lauren_ai._agents._runner import AgentRunnerBase as _RunnerBase
        from agenthicc.plugins.registry import build_registry
        from agenthicc.agents.plugin import BASE_SYSTEM_PROMPT as _BASE
        from agenthicc.runners.tool_populator import populate_agent_tools
        from agenthicc.runners.prompt_contract import PromptBlock
        from agenthicc.tools.capabilities import (
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
        gate_state = ctx.app_state
        agent_section = ""
        if ctx.agent_spec is not None:
            allowed_tool_names, excluded_capabilities, gate_state, agent_section = (
                _agent_restrictions(
                    ctx.agent_spec,
                    registry,
                    allowed_tool_names,
                    excluded_capabilities,
                    ctx.app_state,
                )
            )
        visible_tools = _visible_tools_for_agent(
            registry,
            allowed_tool_names,
            excluded_capabilities,
        )
        visible_names = {tool_name(tool) for tool in visible_tools}
        excluded_names = [name for name in registry.names if name not in visible_names]

        prompt_contract = ctx.prompt_contract
        if prompt_contract is not None:
            visible_tools = prompt_contract.ordered_tools(visible_tools)

        # System prompt
        effective_base = _BASE
        mcp_prompt = getattr(ctx.mcp_registry, "prompt_instructions", None)
        if callable(mcp_prompt):
            prompt_text = mcp_prompt()
            if isinstance(prompt_text, str) and prompt_text.strip():
                effective_base += (
                    "\n\n### MCP server instructions (untrusted metadata)\n" + prompt_text
                )
        if prompt_contract is None:
            system = (
                effective_base
                + (f"\n\n{agent_section}" if agent_section else "")
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
            self._runtime_dynamic_blocks = []
        else:
            # Contract-native workflow turns keep only immutable policy in the
            # system string. The generic runner's PhaseSpec override (or a
            # custom runner's explicit system_prompt) arrives through the
            # PromptContract's dynamic phase-instructions block; it must not be
            # concatenated here or phase changes would invalidate the stable
            # cache prefix. Tool summaries and phase restrictions are also
            # dynamic because the visible tool set may change between phases.
            system = effective_base
            if prompt_contract.stable_system_prefix:
                system += f"\n\n{prompt_contract.stable_system_prefix}"
            self._runtime_dynamic_blocks = []
            if agent_section:
                self._runtime_dynamic_blocks.append(PromptBlock("AGENT", agent_section))
            description = registry.describe(excluded_names=excluded_names)
            if description:
                self._runtime_dynamic_blocks.append(PromptBlock("AVAILABLE TOOLS", description))
            if self._skill_suffix:
                self._runtime_dynamic_blocks.append(
                    PromptBlock("MATCHED SKILLS", self._skill_suffix)
                )
            if allowed_tool_names is not None:
                self._runtime_dynamic_blocks.append(
                    PromptBlock(
                        "PHASE TOOL POLICY",
                        "Only the tools listed above are available in this phase. Do not call "
                        "shell, run_bash, run_command, or any other omitted tool. When calling "
                        "write_file, provide both required arguments: path and complete content.",
                    )
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
                self._workspace_access,
            )
        ]
        self._exploratory_tools = {
            tool_name(tool) for tool in visible_tools if is_exploratory_tool(tool)
        }
        if gate_state is not None:
            from agenthicc.tools.capability_gate import ToolCapabilityGate

            hooks.append(ToolCapabilityGate(gate_state))
            if ctx.approval_svc is not None:
                from agenthicc.tools.approval import ApprovalGate

                hooks.append(ApprovalGate(gate_state, ctx.approval_svc, ctx.workspace_access))

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
                    exchange = getattr(ctx.memory, "active_tool_exchange", None)
                    if exchange is not None and callable(
                        getattr(ctx.memory, "commit_tool_exchange", None)
                    ):
                        # ``tool_calls`` is the approved subset.  The active
                        # transaction still owns the complete assistant batch;
                        # commit against it so lauren-ai can synthesize stable
                        # results for calls filtered out by approval.  Passing
                        # only the subset through ``_complete_tool_results``
                        # would leave a missing-result batch immediately before
                        # the queued user message.
                        ctx.memory.commit_tool_exchange(exchange, results)
                    else:
                        from lauren_ai._agents._runner import _complete_tool_results

                        complete_results = _complete_tool_results(tool_calls, results)
                        ctx.memory.add_tool_results(complete_results)
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
            from agenthicc.subagents.tool import make_spawn_subagents_tool
            from agenthicc.subagents.policy import SubagentExecutionPolicy
            from agenthicc.runners.retry import RetryConfig

            _ec = _execution(ctx.exec_cfg)
            _subagent_retry = RetryConfig(
                max_retries=_ec.transport_max_retries,
                irrecoverable_error_max_retries=max(0, _ec.irrecoverable_error_max_retries),
                base_delay_s=_ec.transport_retry_base_delay_s,
                max_total_duration_s=_ec.transport_retry_max_total_s,
            )
            _subagent_policy = SubagentExecutionPolicy.from_app_state(
                gate_state,
                visible_tool_names=frozenset(tool_name(item) for item in visible_tools),
                system_prompt_suffix=ctx.system_prompt_suffix,
                workspace_scope_identity=(
                    str(ctx.workspace_access.scope.primary_root)
                    if ctx.workspace_access is not None
                    else ""
                ),
            )
            definition_registry = self._build_definition_subagent_registry(
                registry,
                visible_tools,
                gate_state,
            )
            spawn_tool = make_spawn_subagents_tool(
                parent_runner=ctx.runner,
                parent_model=self._model_id,
                all_tools=list(visible_tools),
                app_state=gate_state,
                processor=ctx.processor,
                conv_store=ctx.conv_store,
                usage_ledger=ctx.usage_ledger,
                conversation_id=ctx.conversation_id,
                parent_run_id=self._intent_id,
                conversation_journal=getattr(ctx.session_memory, "journal", None),
                tool_registry=registry,
                registry=definition_registry,
                retry_config=_subagent_retry,
                provider_options={
                    "temperature": _ec.temperature,
                    "top_p": _ec.top_p,
                    "max_completion_tokens": _ec.max_completion_tokens,
                    "request_options": _ec.request_options,
                },
                approval_svc=ctx.approval_svc,
                workspace_access=ctx.workspace_access,
                policy=_subagent_policy,
            )
            registry.register(spawn_tool, source="builtin")
            spawn_name = tool_name(spawn_tool)
            if (allowed_tool_names is None or spawn_name in allowed_tool_names) and not (
                get_tool_capabilities(spawn_tool) & excluded_capabilities
            ):
                self._spawn_tool = cast("Callable[..., Awaitable[dict[str, object]]]", spawn_tool)
                visible_tools.append(spawn_tool)
                populate_agent_tools(agent_instance, visible_tools)

                communication_tools = vars(spawn_tool).get(
                    "__agenthicc_subagent_communication_tools__", ()
                )
                for communication_tool in communication_tools:
                    registry.register(communication_tool, source="builtin")
                    communication_name = tool_name(communication_tool)
                    if (
                        allowed_tool_names is None or communication_name in allowed_tool_names
                    ) and not (get_tool_capabilities(communication_tool) & excluded_capabilities):
                        visible_tools.append(communication_tool)
                populate_agent_tools(agent_instance, visible_tools)

                # The non-contract path embeds the available-tool catalogue
                # directly in the system prompt.  That prompt was assembled
                # before this session-bound tool was registered, so the model
                # could receive a valid ``spawn_subagents`` provider schema
                # while never seeing it in the human-readable tool list.
                # Rebuild the dynamic catalogue after registration.  Contract
                # turns update their append-only AVAILABLE TOOLS block below.
                if prompt_contract is None:
                    from lauren_ai._agents import AGENT_META

                    meta = getattr(type(agent_instance), AGENT_META)
                    description = registry.describe(excluded_names=excluded_names)
                    meta.system = (
                        effective_base
                        + (f"\n\n{agent_section}" if agent_section else "")
                        + (f"\n\n{ctx.system_prompt_suffix}" if ctx.system_prompt_suffix else "")
                        + (self._skill_suffix or "")
                        + (f"\n\n{description}" if description else "")
                    )
                    if allowed_tool_names is not None:
                        meta.system += (
                            "\n\n### Phase-local tool policy\n"
                            "Only the tools listed above are available in this phase. Do not call shell, "
                            "run_bash, run_command, or any other omitted tool. When calling write_file, "
                            "provide both required arguments: path and complete content."
                        )

        # The registry description is intentionally captured after all
        # session-bound tools (including spawn_subagents) have been registered.
        if prompt_contract is not None:
            visible_tools = prompt_contract.ordered_tools(visible_tools)
            populate_agent_tools(agent_instance, visible_tools)
            self._runtime_dynamic_blocks = [
                block
                for block in self._runtime_dynamic_blocks
                if getattr(block, "kind", "") != "AVAILABLE TOOLS"
            ]
            description = registry.describe(excluded_names=excluded_names)
            if description:
                self._runtime_dynamic_blocks.insert(0, PromptBlock("AVAILABLE TOOLS", description))

        return agent_instance, active_runner

    def _build_definition_subagent_registry(
        self,
        tool_registry: ToolRegistry,
        visible_tools: Sequence[object],
        parent_state: AppState | None,
    ) -> SubagentTypeRegistry:
        """Merge Markdown agent definitions into the built-in spawn registry."""
        from agenthicc.agents.selection import resolve_agent_model
        from agenthicc.subagents.policy import SubagentExecutionPolicy
        from agenthicc.subagents.types import (
            DEFAULT_REGISTRY,
            SubagentTypeRegistry,
            SubagentTypeSpec,
        )
        from agenthicc.tools.capabilities import ToolCapability, get_tool_capabilities

        result = SubagentTypeRegistry()
        for name in DEFAULT_REGISTRY.names():
            spec = DEFAULT_REGISTRY.get(name)
            if spec is not None:
                result.register(spec)

        ctx = self._ctx
        if ctx.agents_registry is None:
            return result

        tools_by_name = {tool_name(tool): tool for tool in visible_tools}
        visible_names = frozenset(tools_by_name)
        self._definition_agent_errors.clear()
        for definition in ctx.agents_registry.subagents(include_hidden=True):
            allowed_names = visible_names
            if definition.tools is not None:
                expanded: set[str] = set()
                for pattern in definition.tools:
                    matches = tool_registry.glob_expand(pattern)
                    expanded.update(matches if matches else {pattern})
                allowed_names &= frozenset(expanded)
            if definition.capabilities is not None:
                allowed_capabilities = frozenset(
                    ToolCapability(name) for name in definition.capabilities
                )
                allowed_names = frozenset(
                    name
                    for name in allowed_names
                    if (
                        get_tool_capabilities(tools_by_name[name])
                        or frozenset({ToolCapability.UNDECLARED})
                    )
                    <= allowed_capabilities
                )

            gate_state = parent_state
            prompt = (
                f"## AGENT: {definition.name}\\n{definition.prompt}" if definition.prompt else ""
            )
            if definition.ceiling and gate_state is not None:
                from agenthicc.tui.conversation_store import AppState as PresentationState
                from agenthicc.tui.runtime.mode_manager import builtin_mode, restrict_mode

                ceiling = builtin_mode(definition.ceiling)
                restricted = restrict_mode(gate_state.active_mode(), ceiling)
                if restricted is not gate_state.active_mode():
                    isolated = PresentationState.create()
                    isolated.active_mode.set(restricted)
                    isolated.cli_flags = gate_state.cli_flags
                    gate_state = isolated
                    if ceiling.system_prompt_suffix:
                        prompt = "\\n\\n".join(
                            part for part in (prompt, ceiling.system_prompt_suffix) if part
                        )

            policy = SubagentExecutionPolicy.from_app_state(
                gate_state,
                visible_tool_names=allowed_names,
                workspace_scope_identity=(
                    str(ctx.workspace_access.scope.primary_root)
                    if ctx.workspace_access is not None
                    else ""
                ),
            )
            model = ""
            if definition.model:
                if ctx.agent_config is None:
                    self._definition_agent_errors[definition.name] = (
                        "the session model configuration is unavailable"
                    )
                    continue
                try:
                    model = resolve_agent_model(definition, ctx.agent_config)
                except ValueError as exc:
                    self._definition_agent_errors[definition.name] = str(exc)
                    continue
            result.register(
                SubagentTypeSpec(
                    name=definition.name,
                    allowed_tools=allowed_names,
                    max_turns=min(
                        ctx.max_agent_turns,
                        definition.max_steps
                        if definition.max_steps is not None
                        else ctx.max_agent_turns,
                    ),
                    system_prompt=prompt or definition.description,
                    model=model,
                    policy=policy,
                )
            )
        return result

    async def _run_agent_mentions(self, agent_text: str) -> str:
        """Run each explicit subagent mention and prepend its result to the turn."""
        if not self._agent_mentions:
            return agent_text
        ctx = self._ctx
        if ctx.agents_registry is None or self._spawn_tool is None:
            return (
                agent_text
                + "\\n\\n[⚠ @agent mentions require an interactive session agent registry.]"
            )

        tasks: list[dict[str, str]] = []
        warnings: list[str] = []
        task_text = ctx.text
        for mention in sorted(self._agent_mentions, key=lambda item: item.start, reverse=True):
            task_text = task_text[: mention.start] + task_text[mention.end :]
        task_text = task_text.strip()
        for mention in self._agent_mentions:
            spec = ctx.agents_registry.get(mention.path)
            failure = self._definition_agent_errors.get(mention.path)
            if failure:
                warnings.append(f"[⚠ {mention.raw} cannot run: {failure}]")
                continue
            if spec is None or not spec.subagent:
                warnings.append(f"[⚠ {mention.raw} is not an available subagent definition.]")
                continue
            tasks.append(
                {
                    "type": spec.name,
                    "task": task_text or spec.description,
                    "context": "Explicitly invoked by the user with @agent:NAME.",
                }
            )
        for warning in warnings:
            if ctx.conv_store is not None:
                ctx.conv_store.append_event("error", {"message": warning})
        if not tasks:
            return "\\n\\n".join([*warnings, agent_text]) if warnings else agent_text

        result = await self._spawn_tool(tasks=tasks)
        if not isinstance(result, dict):
            return agent_text + "\\n\\n[⚠ Agent mention returned an invalid subagent result.]"
        output = result.get("results", "")
        if not isinstance(output, str):
            output = str(output)
        if result.get("ok") is not True:
            error = result.get("error", "subagent failed")
            output = f"[⚠ {error}]\\n{output}" if output else f"[⚠ {error}]"
        if not output:
            return agent_text
        warning_text = "\\n".join(warnings)
        return "\\n\\n".join(
            part
            for part in (
                warning_text,
                f"## Results from explicitly mentioned agents\\n{output}",
                agent_text,
            )
            if part
        )

    # ── step 8: streaming loop ────────────────────────────────────────────────

    async def _auto_compact_if_needed(
        self,
        active_runner: AgentRunnerBase,
        agent_text: str,
        *,
        max_input_tokens: int,
        force: bool = False,
        agent_instance: object | None = None,
    ) -> bool:
        """Compact shared memory at Agenthicc's durable turn boundary.

        Agenthicc deliberately owns this decision for both direct and workflow
        turns. This keeps journal persistence and detached-session recovery
        inside the same boundary instead of relying on lauren-ai's internal
        compaction exception handling. Exact token counting is attempted only
        near the conservative threshold, is time-bounded, and falls back to a
        stricter local estimate when unavailable.

        ``force`` is used only after a provider explicitly rejects the request
        for context length. The caller compacts the current projection in
        place; it never restores a snapshot from before the logical turn, so
        committed provider steps and the user message remain available.
        """
        ctx = self._ctx
        memory = ctx.session_memory
        if not _execution(ctx.exec_cfg).auto_compact or memory is None:
            return False

        messages = getattr(memory, "_messages", None)
        if not isinstance(messages, list):
            return False

        estimate = getattr(memory, "token_estimate", 0)
        if not isinstance(estimate, int):
            estimate = 0
        system_prompt, tool_schemas, request_text = self._compaction_request_context(
            active_runner,
            agent_instance,
            agent_text,
        )
        projected_messages = list(messages)
        if not _has_trailing_user_message(projected_messages, request_text):
            projected_messages.append({"role": "user", "content": request_text})
        # Count the same logical request context as the provider call: system
        # instructions and tool schemas are included when the transport exposes
        # exact counting. The conservative estimate remains the fallback.
        projected = _conservative_message_estimate(
            projected_messages,
            system=system_prompt,
            tools=tool_schemas,
        )
        threshold = max(1, int(max_input_tokens * 0.8))
        if not force and projected < threshold:
            return False

        transport = getattr(active_runner, "_transport", None)
        exact_count = None
        if not force:
            exact_count = await self._exact_compaction_count(
                transport,
                projected_messages,
                system_prompt=system_prompt,
                tool_schemas=tool_schemas,
            )
        if not force and exact_count is not None and exact_count < threshold:
            return False

        if not messages and (
            force
            or projected >= max_input_tokens
            or (exact_count is not None and exact_count >= max_input_tokens)
        ):
            from agenthicc.memory.compactor import (
                CompactionOutcome,
                CompactionRecoveryRequired,
                _emit_outcome,
            )

            outcome = CompactionOutcome(
                "blocked_irreducible_context",
                estimate,
                estimate,
                durable=False,
                fallback_result="irreducible_latest_exchange",
                recovery_reason="current_input_exceeds_budget",
            )
            _emit_outcome(ctx.conv_store, outcome)
            raise CompactionRecoveryRequired(outcome)

        if transport is None or not callable(getattr(transport, "complete", None)):
            if force or projected >= max_input_tokens or not messages:
                from agenthicc.memory.compactor import (
                    CompactionOutcome,
                    CompactionRecoveryRequired,
                    _emit_outcome,
                )

                outcome = CompactionOutcome(
                    "blocked_irreducible_context",
                    estimate,
                    estimate,
                    durable=False,
                    fallback_result="no_safe_boundary",
                    recovery_reason=(
                        "current_input_exceeds_budget"
                        if not messages
                        else "latest_exchange_too_large"
                    ),
                )
                _emit_outcome(ctx.conv_store, outcome)
                raise CompactionRecoveryRequired(outcome)
            return False

        from agenthicc.memory.compactor import (
            CompactionOutcome,
            CompactionRecoveryRequired,
            _emit_outcome,
            _ConcurrentCompactionUpdate,
            _InvalidCompactionCandidate,
            _memory_summary,
            _prepare_local_compaction_fallback,
            _replace_state,
            compact_memory_with_result,
        )

        execution = _execution(ctx.exec_cfg)

        outcome = await compact_memory_with_result(
            memory,
            transport,
            model=self._model_id,
            conv_store=ctx.conv_store,
            max_input_tokens=max_input_tokens,
            usage_ledger=ctx.usage_ledger,
            session_id=ctx.conversation_id,
            run_id=self._intent_id,
            max_completion_tokens=execution.max_completion_tokens,
            request_options=execution.request_options,
            emit_result=False,
        )
        if outcome.applied:
            _emit_outcome(ctx.conv_store, outcome)
            return True

        if outcome.status == "skipped_concurrent_update":
            _emit_outcome(ctx.conv_store, outcome)
            if force:
                raise CompactionRecoveryRequired(outcome)
            return False

        if outcome.status == "blocked_durability_error":
            _emit_outcome(ctx.conv_store, outcome)
            raise CompactionRecoveryRequired(outcome)

        expected_messages = copy.deepcopy(list(memory._messages))
        expected_summary = _memory_summary(memory)
        prepared = _prepare_local_compaction_fallback(
            memory,
            max_input_tokens=max_input_tokens,
        )
        fallback = prepared.outcome
        if prepared.messages is not None:
            from dataclasses import replace

            candidate_request = list(prepared.messages)
            if not _has_trailing_user_message(candidate_request, request_text):
                candidate_request.append({"role": "user", "content": request_text})
            candidate_count = await self._exact_compaction_count(
                transport,
                candidate_request,
                system_prompt=system_prompt,
                tool_schemas=tool_schemas,
            )

            # Exact provider counting includes system/tool framing. If the
            # conservative candidate still exceeds the hard budget, make one
            # smaller deterministic candidate and re-count it. This is local
            # token accounting, not another summary/provider retry.
            if candidate_count is not None and candidate_count > max_input_tokens:
                tighter_target = max(
                    1,
                    int(fallback.after_tokens * max_input_tokens / max(1, candidate_count) * 0.60),
                )
                prepared = _prepare_local_compaction_fallback(
                    memory,
                    max_input_tokens=max_input_tokens,
                    target_tokens=tighter_target,
                )
                fallback = prepared.outcome
                candidate_count = None
                if prepared.messages is not None:
                    candidate_request = list(prepared.messages)
                    if not _has_trailing_user_message(candidate_request, request_text):
                        candidate_request.append({"role": "user", "content": request_text})
                    candidate_count = await self._exact_compaction_count(
                        transport,
                        candidate_request,
                        system_prompt=system_prompt,
                        tool_schemas=tool_schemas,
                    )

            if prepared.messages is not None and (
                candidate_count is None or candidate_count <= max_input_tokens
            ):
                try:
                    _replace_state(
                        memory,
                        prepared.messages,
                        prepared.summary,
                        expected_messages=expected_messages,
                        expected_summary=expected_summary,
                    )
                    fallback = replace(
                        prepared.outcome,
                        after_tokens=memory.token_estimate,
                        durable=True,
                    )
                except Exception as exc:  # noqa: BLE001 - keep old projection on commit failure
                    if isinstance(exc, _ConcurrentCompactionUpdate):
                        fallback = replace(
                            prepared.outcome,
                            status="skipped_concurrent_update",
                            after_tokens=memory.token_estimate,
                            durable=False,
                            recovery_reason="concurrent_update",
                        )
                        combined = replace(
                            fallback,
                            summary_result=outcome.summary_result,
                            summary_attempts=outcome.summary_attempts,
                            summary_error_type=outcome.summary_error_type,
                            error_type=outcome.error_type,
                        )
                        _emit_outcome(ctx.conv_store, combined)
                        if force:
                            raise CompactionRecoveryRequired(combined) from exc
                        return False
                    if isinstance(exc, _InvalidCompactionCandidate):
                        fallback = replace(
                            prepared.outcome,
                            status="blocked_irreducible_context",
                            after_tokens=prepared.outcome.before_tokens,
                            durable=False,
                            error_type=exc.issue_code,
                            fallback_result="no_safe_boundary",
                            recovery_reason="invalid_tool_history_candidate",
                        )
                    else:
                        fallback = CompactionOutcome(
                            "blocked_durability_error",
                            prepared.outcome.before_tokens,
                            prepared.outcome.before_tokens,
                            durable=False,
                            error_type=type(exc).__name__[:96],
                            fallback_result="durability_error",
                            recovery_reason="compaction_commit_failed",
                        )
            elif prepared.messages is not None:
                fallback = CompactionOutcome(
                    "blocked_irreducible_context",
                    prepared.outcome.before_tokens,
                    prepared.outcome.before_tokens,
                    durable=False,
                    fallback_result="irreducible_latest_exchange",
                    recovery_reason="exact_count_over_budget",
                )

        from dataclasses import replace

        combined = replace(
            fallback,
            summary_result=outcome.summary_result,
            summary_attempts=outcome.summary_attempts,
            summary_error_type=outcome.summary_error_type,
            error_type=fallback.error_type or outcome.error_type,
        )
        current_input_estimate = _conservative_message_estimate(
            [{"role": "user", "content": request_text}],
            system="",
        )
        if (
            combined.status == "blocked_irreducible_context"
            and current_input_estimate >= max_input_tokens
            and combined.recovery_reason == "no_protocol_safe_boundary"
        ):
            combined = replace(combined, recovery_reason="current_input_exceeds_budget")
        _emit_outcome(ctx.conv_store, combined)
        if combined.status == "blocked_durability_error":
            raise CompactionRecoveryRequired(combined)
        if combined.applied:
            return True
        # Below the hard usable budget, the independent provider guard remains
        # safe to use. After an actual context rejection, however, retaining an
        # unfit projection would simply replay the same failure forever.
        if (
            force
            or projected >= max_input_tokens
            or (exact_count is not None and exact_count >= max_input_tokens)
        ):
            raise CompactionRecoveryRequired(combined)
        return False

    def _compaction_request_context(
        self,
        active_runner: AgentRunnerBase,
        agent_instance: object | None,
        agent_text: str,
    ) -> tuple[str, list[object] | None, str]:
        """Resolve the prompt/schema context used for exact candidate counts."""
        ctx = self._ctx
        memory = ctx.session_memory
        meta: AgentMeta | None = None
        if agent_instance is not None:
            try:
                meta = active_runner._get_meta(agent_instance)
            except Exception:  # noqa: BLE001 - compatibility fallback is conservative
                meta = None

        system = getattr(meta, "system", None)
        if not isinstance(system, str):
            system = ""
        tools: list[object] | None = None
        if meta is not None:
            try:
                schemas = active_runner._get_tool_schemas(meta)
                if isinstance(schemas, list):
                    tools = schemas
            except Exception:  # noqa: BLE001 - omit unavailable schema estimate
                tools = None

        summary = getattr(memory, "summary", None)
        contract = ctx.prompt_contract
        if contract is not None:
            request_text = agent_text
            messages = getattr(memory, "_messages", [])
            if not _has_trailing_user_message(messages, agent_text):
                extra = list(getattr(self, "_runtime_dynamic_blocks", []))
                if isinstance(summary, str) and summary:
                    extra.append(PromptBlock("EARLIER CONVERSATION SUMMARY", summary))
                try:
                    request_text = contract.render_dynamic_message(agent_text, extra_blocks=extra)
                except Exception:  # noqa: BLE001 - retain supplied prompt on compatibility failure
                    request_text = agent_text
        else:
            request_text = agent_text
            if isinstance(summary, str) and summary:
                system = f"{system}\n\n[Earlier conversation summary]\n{summary}"
        return system, tools, request_text

    async def _exact_compaction_count(
        self,
        transport: object,
        messages: list[object],
        *,
        system_prompt: str,
        tool_schemas: list[object] | None,
    ) -> int | None:
        """Count a candidate with request framing when the adapter supports it."""
        count_tokens = getattr(transport, "count_tokens", None)
        if not callable(count_tokens):
            return None
        try:
            from lauren_ai._transport import ContentBlock, Message

            countable_messages = [
                Message(
                    role=cast(
                        "Literal['user', 'assistant']", str(_message_field(message, "role", "user"))
                    ),
                    content=cast(
                        "str | list[ContentBlock]", _message_field(message, "content", "")
                    ),
                )
                for message in messages
                if _message_field(message, "role", "") in {"user", "assistant"}
            ]
            kwargs: dict[str, object] = {"model": self._model_id}
            params: Mapping[str, inspect.Parameter]
            try:
                params = inspect.signature(count_tokens).parameters
            except (TypeError, ValueError):
                params = {}
            accepts_kwargs = any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in params.values()
            )
            if "system" in params or accepts_kwargs:
                kwargs["system"] = system_prompt or None
            if "tools" in params or accepts_kwargs:
                kwargs["tools"] = tool_schemas
            result = await asyncio.wait_for(count_tokens(countable_messages, **kwargs), timeout=8.0)
            return result if isinstance(result, int) and result >= 0 else None
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - exact counting remains advisory
            if self._ctx.conv_store is not None:
                try:
                    self._ctx.conv_store.append_event(
                        "compaction_status",
                        {
                            "status": "token_count_unavailable",
                            "error_type": type(exc).__name__[:96],
                        },
                    )
                except Exception:  # noqa: BLE001 - projection is advisory
                    logger.warning("could not project token-count fallback status")
            logger.warning(
                "compaction token count unavailable (%s); using conservative estimate",
                type(exc).__name__,
            )
            return None

    def _local_compaction_fallback(self, *, max_input_tokens: int) -> bool:
        """Compatibility helper for older tests and callers."""
        memory = self._ctx.session_memory
        if memory is None:
            return False
        from agenthicc.memory.compactor import local_compaction_fallback

        outcome = local_compaction_fallback(memory, max_input_tokens=max_input_tokens)
        return outcome.applied

    def _prepare_cache_stable_message(self, message: str) -> str:
        """Move the rolling summary into dynamic context for contract turns.

        lauren-ai 1.4 appends ``memory.summary`` to the decorated system
        prompt.  That is correct for ordinary turns but would make the stable
        system cache block change after compaction.  Contract-native turns
        temporarily hide that field from lauren-ai and append the same summary
        as an ordinary user-context block instead.  The original value is
        restored when the turn finishes, so journal/checkpoint semantics remain
        unchanged.
        """

        contract = self._ctx.prompt_contract
        memory = self._ctx.session_memory
        if contract is None or memory is None:
            return message
        summary = getattr(memory, "summary", None)
        self._cache_summary = summary if isinstance(summary, str) and summary else None
        if hasattr(memory, "_summary"):
            memory._summary = None
        extra = list(self._runtime_dynamic_blocks)
        if self._cache_summary:
            extra.append(PromptBlock("EARLIER CONVERSATION SUMMARY", self._cache_summary))
        return contract.render_dynamic_message(message, extra_blocks=extra)

    def _restore_cache_summary(self) -> None:
        """Restore the hidden rolling summary after a contract-native turn."""

        memory = self._ctx.session_memory
        if (
            self._ctx.prompt_contract is not None
            and memory is not None
            and hasattr(memory, "_summary")
        ):
            memory._summary = self._cache_summary

    async def _stream(
        self,
        agent_instance: object,
        agent_text: str,
        active_runner: AgentRunnerBase,
    ) -> None:
        from lauren_ai._config import AgentConfig as _AgentConfig

        ctx = self._ctx

        from agenthicc.memory.journaled import JournaledShortTermMemory

        if isinstance(ctx.session_memory, JournaledShortTermMemory):
            repaired_history = ctx.session_memory.ensure_provider_ready()
            if repaired_history and ctx.conv_store is not None:
                ctx.conv_store.append_event(
                    "system",
                    {
                        "text": (
                            "Tool execution history was repaired before continuing; "
                            "completed work was preserved."
                        )
                    },
                )

        # A cancellation/resume race can leave a result block behind a queued
        # user message. Repair the persisted projection before Lauren-ai's
        # preflight rejects the next provider request.
        repaired_history = _preserve_interrupted_memory(ctx.session_memory)
        if repaired_history and ctx.conv_store is not None:
            ctx.conv_store.append_event(
                "system",
                {
                    "text": (
                        "Tool execution history was repaired before continuing; "
                        "completed work was preserved."
                    )
                },
            )

        contract = ctx.prompt_contract
        # Keep the caller-supplied turn text separate from the rendered
        # contract message.  A context-overflow retry must rebuild the latter
        # after compaction without replacing the phase prompt with ``ctx.text``
        # (which may not include workflow-generated dynamic instructions).
        base_agent_text = agent_text

        usage_tracker = None
        if ctx.usage_ledger is not None:
            from agenthicc.runners.usage_ledger import UsageRunTracker

            usage_tracker = UsageRunTracker(
                ctx.usage_ledger,
                run_id=self._intent_id,
                provider=self._provider_label(),
                model=self._model_id,
                agent_id=self._agent_id,
                agent_name=ctx.active_agent or "default",
            )
            self._usage_tracker = usage_tracker

        turn_completed = False
        turn_cancelled = False
        turn_recoverable_compaction = False
        turn_error: BaseException | None = None

        # Agenthicc owns proactive compaction immediately before the shared
        # runner call so it can durably commit journal-backed state and turn a
        # failed persistence step into a recoverable session outcome. Lauren's
        # own automatic summarizer is disabled below to prevent a second
        # untyped compaction path from bypassing this boundary.
        #
        # PRD-133/136: the live-context budget is derived from the model's real
        # context window — resolved from the [memory.context_windows] map (per
        # model) → registry → default (ExecutionSettings.effective_context_window).
        # usable = window − completion reservation − head-room; summarisation fires
        # at ``summarize_at`` of that live window, and the same window feeds
        # lauren-ai's hard pre-send guard via AgentConfig.context_window
        # (PRD-133 D/E) so a request can never exceed the window.
        _auto_compact = _execution(ctx.exec_cfg).auto_compact
        # Completion ceiling for one round-trip.  lauren-ai defaults this to 4096,
        # which silently truncates a single large tool call (e.g. write_file with a
        # whole source file): the partial call is discarded, the turn yields nothing,
        # and the calling phase retries forever.  ExecutionSettings.max_output_tokens
        # is the configurable ceiling and is what effective_usable_budget() reserves.
        if ctx.exec_cfg is None:
            _max_output = _execution(None).max_output_tokens
        else:
            _max_output = ctx.exec_cfg.max_output_tokens or int(_AgentConfig().max_tokens_per_turn)
        if ctx.exec_cfg is not None:
            _window = ctx.exec_cfg.effective_context_window()
            _window_tokens = ctx.exec_cfg.effective_usable_budget()
        else:
            from agenthicc.config import _context_window_for

            _window = _context_window_for(self._model_id)
            _window_tokens = max(1, _window - _max_output - max(4_000, _window // 25))

        # Proactive compaction happens before lauren-ai appends this turn's
        # user message. If its durable replacement is blocked, preserve the
        # accepted direct-turn input as an incomplete logical turn so a
        # detached session can re-drive it after recovery. Workflow turns use
        # their workflow checkpoint as the recovery authority instead; writing
        # a parallel turn-recovery marker there would create competing cursors.
        _journal = getattr(ctx.session_memory, "journal", None)
        # The same durable compaction policy applies to foreground, detached,
        # direct, and workflow turns. For contract turns, keep the summary out
        # of the stable system prompt after this memory boundary is updated.
        try:
            await self._auto_compact_if_needed(
                active_runner,
                agent_text,
                max_input_tokens=_window_tokens,
                agent_instance=agent_instance,
            )
        except Exception as exc:
            from agenthicc.memory.compactor import CompactionRecoveryRequired

            if isinstance(exc, CompactionRecoveryRequired) and _journal is not None:
                from agenthicc.runners.workflow_execution_context import (
                    current_workflow_run_id,
                )

                if current_workflow_run_id() is None and ctx.session_memory is not None:
                    try:
                        _journal.turn_started(
                            self._intent_id,
                            agent_text,
                            len(ctx.session_memory._messages),
                            conversation_id=ctx.conversation_id,
                            base_cursor=_journal.cursor,
                        )
                        _journal.turn_recovery_started(self._intent_id)
                    except Exception as journal_exc:  # noqa: BLE001 - keep typed recovery state
                        logger.critical(
                            "could not persist the direct-turn recovery marker (%s)",
                            type(journal_exc).__name__,
                        )
            raise
        if contract is not None:
            agent_text = self._prepare_cache_stable_message(agent_text)
            if ctx.conv_store is not None:
                ctx.conv_store.append_event("prompt_cache", contract.diagnostics())

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
        if ctx.resume_ledger is not None:
            turn_ledger = ctx.resume_ledger
        elif _journal is not None:
            from agenthicc.runners.durable_ledger import DurableIdempotencyLedger

            turn_ledger = DurableIdempotencyLedger(_journal, self._intent_id)
        else:
            from lauren_ai import IdempotencyLedger

            turn_ledger = IdempotencyLedger()
        self._turn_ledger = turn_ledger

        # PRD-129 Phase 3: mark the turn's start + rollback point in the journal.
        # On a crash the absence of a matching turn_completed (written in the
        # finally below) flags this turn for resumption.
        if _journal is not None and ctx.session_memory is not None:
            from agenthicc.runners.workflow_execution_context import (
                current_workflow_run_id,
            )

            _journal.turn_started(
                self._intent_id,
                agent_text,
                len(ctx.session_memory._messages),
                conversation_id=ctx.conversation_id,
                base_cursor=getattr(_journal, "cursor", None),
                workflow_run_id=current_workflow_run_id(),
            )

        # PRD-182: retries must preserve the committed provider/tool prefix.
        # Step-aware lauren-ai runners advance an explicit checkpoint; older
        # runners retain their current memory projection and rely on the
        # idempotency ledger instead of rolling back to the start of the turn.
        _attempt_number = [0]
        _partial_stream_text: list[str] = []
        _stream_seeded_user = [False]
        _resume_existing_turn = [ctx.resume_turn_id is not None]
        _safe_snapshot_includes_user = [False]
        _partial_fragment_recorded = [False]
        _output_attempt_start = [
            len(ctx.output_collector) if ctx.output_collector is not None else 0
        ]
        _step_sink: object | None = None
        _latest_safe_snapshot: object | None = None

        async def _stream_once() -> None:
            local_turn: list[str] = []
            _attempt_number[0] += 1
            _partial_stream_text.clear()
            _stream_seeded_user[0] = False
            _partial_fragment_recorded[0] = False
            _output_attempt_start[0] = (
                len(ctx.output_collector) if ctx.output_collector is not None else 0
            )
            if usage_tracker is not None and _attempt_number[0] > 1:
                usage_tracker.finalize("failed")
            config_kwargs = _AgentConfig(
                max_turns=ctx.max_agent_turns,
                max_tokens_per_turn=_max_output,
                parallel_tool_calls=True,
                memory_window_tokens=_window_tokens,
                # Agenthicc owns all summarization so failures and journal
                # commits share one typed, resumable boundary.
                summarize_at=None,
                summary_model=self._model_id,
            )
            # Provider profiles carry sampling and vendor-specific request
            # options into every turn, including workflow phases.  Use
            # dataclass field detection so a downstream runner double or an
            # older lauren-ai installation remains import-compatible.
            from dataclasses import replace

            _ec = _execution(ctx.exec_cfg)
            provider_options: dict[str, object] = {
                "temperature": _ec.temperature,
                "top_p": _ec.top_p,
                "max_completion_tokens": _ec.max_completion_tokens,
                "request_options": _ec.request_options,
                "transport_max_retries": _ec.transport_max_retries,
                "transport_retry_base_delay_s": _ec.transport_retry_base_delay_s,
                "transport_retry_max_total_s": _ec.transport_retry_max_total_s,
            }
            if provider_options["request_options"] is not None:
                from agenthicc.config import RequestOptionSettings

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
            event_sinks: list[object] = []
            if usage_tracker is not None:
                event_sinks.append(usage_tracker.sink)
            if getattr(ctx.runner, "_signals", None) is None and ctx.conv_store is not None:
                if self._recovery_projector is not None:
                    event_sinks.append(_ToolRecoveryEventSink(self._recovery_projector))
            if _step_sink is not None:
                event_sinks.append(_step_sink)
            resume_existing_turn = _resume_existing_turn[0] or _safe_snapshot_includes_user[0]
            attempt_message = "" if resume_existing_turn else agent_text
            compatibility_seed_snapshot = (
                ctx.session_memory.snapshot()
                if (
                    resume_existing_turn
                    and ctx.session_memory is not None
                    and hasattr(ctx.session_memory, "snapshot")
                )
                else None
            )
            stream = await active_runner.run_stream(
                agent_instance,
                attempt_message,
                conversation_id=ctx.conversation_id or None,
                run_id=self._intent_id,
                memory=ctx.session_memory,
                idempotency_ledger=turn_ledger,
                config_override=config_kwargs,
                metadata={
                    # A crash/recovery or compatibility retry starts from a
                    # memory projection that already contains this logical
                    # turn's user message. Ordinary turns add it in Lauren.
                    "_agenthicc_resume_existing_turn": resume_existing_turn
                },
                event_sinks=event_sinks or None,
            )
            # ``run_stream`` seeds the user message before returning its async
            # generator.  On a retry from a committed step, the empty
            # compatibility seed must not reach the provider or become part of
            # the durable conversation, so restore the current projection
            # immediately. This removes only the synthetic empty user message
            # that older lauren-ai versions append unconditionally.
            _stream_seeded_user[0] = True
            if (
                compatibility_seed_snapshot is not None
                and ctx.session_memory is not None
                and hasattr(ctx.session_memory, "restore")
            ):
                ctx.session_memory.restore(compatibility_seed_snapshot)
            if not self._step_recovery_supported:
                # The pre-1.6 runner has no step receipt to advance a
                # checkpoint, but its stream still seeded this logical user
                # message. The next retry should reuse the current projection
                # rather than append the message again.
                _safe_snapshot_includes_user[0] = True
            async for chunk in stream:
                if chunk.delta:
                    local_turn.append(chunk.delta)
                    _partial_stream_text.append(chunk.delta)
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
                                    f"⚠ The response was truncated after reaching the "
                                    f"{_max_output}-token output limit, so the incomplete "
                                    "write_file call was discarded. Please split large files "
                                    "into chunks: use write_file for the first chunk, then "
                                    "append_file for each subsequent chunk, and read the file "
                                    "to verify it. You can also raise "
                                    "[execution].max_output_tokens for models that support "
                                    "larger completions."
                                )
                            },
                        )
                    turn_text = "".join(local_turn).strip()
                    local_turn = []
                    _partial_stream_text.clear()
                    if turn_text and ctx.conv_store:
                        ctx.conv_store.append_event("text", {"text": turn_text})
                    # Auto-index completed turn text for semantic search (PRD-101).
                    if turn_text and ctx.semantic_index is not None:
                        doc_id = f"{self._intent_id}_{ctx.completed_turns}"
                        ctx.semantic_index.index_in_background(doc_id, turn_text)

        # Snapshot after any proactive compaction, immediately before Lauren
        # adds this turn's user message.  It is the safe rollback point for the
        # reactive compact-then-retry path below.
        turn_snapshot = (
            ctx.session_memory.snapshot()
            if ctx.session_memory is not None and hasattr(ctx.session_memory, "snapshot")
            else None
        )
        _latest_safe_snapshot = turn_snapshot
        self._step_recovery_supported = bool(
            getattr(active_runner, "supports_step_recovery", False) and _journal is not None
        )

        def _advance_safe_step(step_id: str) -> None:
            nonlocal _latest_safe_snapshot
            if ctx.session_memory is not None and hasattr(ctx.session_memory, "snapshot"):
                _latest_safe_snapshot = ctx.session_memory.snapshot()
                _safe_snapshot_includes_user[0] = True
            self._last_committed_step = step_id

        def _begin_step(step_id: str) -> None:
            self._active_step_id = step_id

        if _journal is not None and self._step_recovery_supported:
            _step_sink = _TurnRecoveryEventSink(
                _journal,
                ctx.conv_store,
                _advance_safe_step,
                _begin_step,
            )

        def _reset_attempt_output() -> None:
            if ctx.output_collector is not None:
                del ctx.output_collector[_output_attempt_start[0] :]

        def _record_partial_fragment() -> None:
            """Persist interrupted output without turning it into a message."""
            if not _partial_stream_text or _partial_fragment_recorded[0]:
                return
            text = "".join(_partial_stream_text)[:16_384]
            record = getattr(ctx.session_memory, "record_partial_fragment", None)
            if callable(record):
                try:
                    record(self._intent_id, text, step_id=self._active_step_id)
                except Exception:  # noqa: BLE001 - preserve the provider failure
                    logger.warning(
                        "agenthicc: failed to persist interrupted provider output",
                        exc_info=True,
                    )
            if ctx.conv_store:
                try:
                    ctx.conv_store.append_event(
                        "assistant_partial",
                        {"text": text, "interrupted": True},
                    )
                except Exception:  # noqa: BLE001 - preserve the provider failure
                    logger.warning(
                        "agenthicc: failed to project interrupted provider output",
                        exc_info=True,
                    )
            _partial_fragment_recorded[0] = True

        self._safe_snapshot_provider = lambda: _latest_safe_snapshot
        self._reset_attempt_output = _reset_attempt_output

        try:
            # A second preflight is needed only for old lauren-ai runtimes that
            # cannot enforce the context-window guard themselves. The earlier
            # proactive pass already compacted when above threshold; this is
            # therefore an inexpensive no-op in the normal case.
            if not _supports_context_guard:
                await self._auto_compact_if_needed(
                    active_runner,
                    agent_text,
                    max_input_tokens=_window_tokens,
                    agent_instance=agent_instance,
                )
                if ctx.session_memory is not None and hasattr(ctx.session_memory, "snapshot"):
                    turn_snapshot = ctx.session_memory.snapshot()
                    _latest_safe_snapshot = turn_snapshot

            await self._stream_with_retry(_stream_once)
            turn_completed = True
        except (asyncio.CancelledError, KeyboardInterrupt):
            turn_cancelled = True
            # Preserve completed sub-turns and tool results so the next user
            # message can ask what was in progress. Only an unanswered final
            # tool call is healed; rolling back to ``turn_base_count`` would
            # erase the useful context from the shared session conversation.
            try:
                repaired = _preserve_interrupted_memory(ctx.session_memory)
            except Exception:  # noqa: BLE001
                # Cancellation must retain its original identity. The next
                # resume attempt will surface a typed invariant diagnostic if
                # the malformed history was not deterministically repairable.
                logger.warning(
                    "agenthicc: interrupted tool history could not be repaired", exc_info=True
                )
                repaired = False
            if repaired and ctx.conv_store:
                self._project_legacy_recovery()
            _record_partial_fragment()
            if ctx.conv_store:
                ctx.conv_store.close_turn()
            raise  # must propagate so task.cancel() terminates the workflow runner
        except Exception as exc:
            turn_error = exc
            # A generic 400 from older Lauren/OpenAI integrations is still
            # recoverable when its only cause is an overlong context. Compact
            # the current projection once and retry the same provider request.
            # Crucially, do not restore ``turn_snapshot`` here: it predates
            # this logical turn and could erase provider steps already
            # committed before a later step overflowed.
            overflow_error = _auto_compact and _is_context_overflow_error(exc)
            if overflow_error:
                _resume_existing_turn[0] = _stream_seeded_user[0]
                _record_partial_fragment()
                _reset_attempt_output()
                try:
                    compacted = await self._auto_compact_if_needed(
                        active_runner,
                        agent_text,
                        max_input_tokens=_window_tokens,
                        force=True,
                        agent_instance=agent_instance,
                    )
                except Exception as compaction_exc:
                    from agenthicc.memory.compactor import CompactionRecoveryRequired

                    if isinstance(compaction_exc, CompactionRecoveryRequired):
                        turn_error = compaction_exc
                        turn_recoverable_compaction = True
                    raise
                if compacted:
                    if ctx.session_memory is not None and hasattr(ctx.session_memory, "snapshot"):
                        _safe_snapshot_includes_user[0] = (
                            _stream_seeded_user[0] or _resume_existing_turn[0]
                        )
                        _latest_safe_snapshot = ctx.session_memory.snapshot()
                    if contract is not None:
                        agent_text = self._prepare_cache_stable_message(base_agent_text)
                    try:
                        # The failed overflow request already seeded the user
                        # message. Keep that message and all earlier committed
                        # steps in memory while the compatibility retry runs.
                        await self._stream_with_retry(_stream_once)
                    except Exception as retry_exc:  # noqa: BLE001
                        exc = retry_exc
                        turn_error = retry_exc
                    else:
                        turn_completed = True

            # A non-overflow failure may arrive after Lauren has appended an
            # assistant tool batch but before the result exchange is durable.
            # Repair it before exposing the error or allowing a phase loop to
            # retry. Context-overflow retries keep the current projection and
            # therefore do not need a synthetic pre-turn reset.
            repaired = False
            if not overflow_error:
                try:
                    repaired = _preserve_interrupted_memory(ctx.session_memory)
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "agenthicc: tool conversation could not be repaired after turn failure",
                        exc_info=True,
                    )
            if repaired and ctx.conv_store:
                self._project_legacy_recovery()

            if turn_completed:
                return
            _record_partial_fragment()
            if ctx.conv_store:
                # Emit one well-formatted error event with the exception class name.
                # Do NOT call fail_turn/close_turn here — the finally block handles
                # state cleanup idempotently, preventing the double-fail bug.
                error_event_id: str | None = f"agent-turn-error:{self._intent_id}"
                try:
                    # Tag an arbitrary (possibly third-party) exception object.
                    setattr(exc, "_agenthicc_error_event_id", error_event_id)  # noqa: B010
                except Exception:  # noqa: BLE001 - third-party exceptions may be slotted
                    error_event_id = None
                missing_session_diagnostic = _missing_opencode_session_diagnostic(exc)
                metadata = _provider_failure_metadata(
                    exc,
                    ctx,
                    provider=self._provider_label(),
                    model=self._model_id,
                    turn_id=self._intent_id,
                )
                if missing_session_diagnostic is not None:
                    ctx.conv_store.append_event(
                        "error",
                        {
                            "message": "Provider session identity is missing",
                            "detail": missing_session_diagnostic,
                            **metadata,
                        },
                        event_id=error_event_id,
                    )
                else:
                    status = metadata.get("http_status")
                    message = (
                        f"Provider request failed (HTTP {status})"
                        if isinstance(status, int)
                        else f"Agent turn failed ({metadata['exception_type']})"
                    )
                    ctx.conv_store.append_event(
                        "error",
                        {"message": message, **metadata},
                        event_id=error_event_id,
                    )
            # Every exception that survives the provider-step retry policy must
            # cross the agent-turn boundary.  Workflow owners need the original
            # exception to finalize one durable pause for the already-attached
            # typed context; swallowing a 429, timeout, tool error, ValueError,
            # or unknown extension exception makes the phase look like a normal
            # return and can send it through a fresh INIT path.  Permanence still
            # controls retry eligibility inside _stream_with_retry, but it never
            # controls workflow resumability.
            # _stream()'s finally block still runs → close_turn() is called.
            raise
        finally:
            if usage_tracker is not None:
                lifecycle = (
                    "completed" if turn_completed else "cancelled" if turn_cancelled else "failed"
                )
                usage_tracker.finalize(lifecycle)
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
                    elif turn_cancelled:
                        _journal.turn_aborted(self._intent_id, reason="cancelled")
                    elif ctx.resume_turn_id is not None or turn_recoverable_compaction:
                        # A failed re-drive must remain discoverable with its
                        # original turn ID and durable tool ledger. Closing it
                        # with turn_failed would make a later worker start a
                        # fresh turn against the same partial conversation.
                        _journal.turn_recovery_started(self._intent_id)
                    else:
                        fail = getattr(_journal, "turn_failed", None)
                        if callable(fail):
                            fail(
                                self._intent_id,
                                last_committed_step=self._last_committed_step,
                                cursor=getattr(_journal, "cursor", None),
                                error_kind=(
                                    type(turn_error).__name__
                                    if turn_error is not None
                                    else "agent_turn"
                                ),
                                retryable=(
                                    not _is_permanent_error(turn_error)
                                    if turn_error is not None
                                    else True
                                ),
                            )
                        else:
                            _journal.turn_aborted(self._intent_id, reason="failed")
                except OSError:
                    pass
            self._restore_cache_summary()
            self._safe_snapshot_provider = None
            self._reset_attempt_output = None

    # ── transport retry wrapper (PRD-126) ─────────────────────────────────────

    async def _stream_with_retry(self, stream_once: Callable[[], Awaitable[None]]) -> None:
        """Run one streaming invocation with safe transport retries.

        Delegates to the shared :func:`~agenthicc.runners.retry.run_with_transport_retry`.
        The helper restores the latest committed provider-step checkpoint when
        the installed lauren-ai runner exposes one; otherwise its idempotency
        ledger prevents already-completed tool side effects from running twice.
        Reads retry bounds from ``ctx.exec_cfg``.
        """
        from agenthicc.runners.retry import RetryConfig, run_with_transport_retry

        ctx = self._ctx
        exec_cfg = _execution(ctx.exec_cfg)
        # agenthicc owns this boundary because lauren-ai releases without
        # provider-step retries still need to recover transient stream errors.
        # A step-aware runner supplies the latest safe snapshot; older runners
        # preserve their current projection and use the durable idempotency
        # ledger instead of rolling back to the turn start.
        config = RetryConfig(
            max_retries=max(0, exec_cfg.transport_max_retries),
            irrecoverable_error_max_retries=max(0, exec_cfg.irrecoverable_error_max_retries),
            base_delay_s=max(0.0, exec_cfg.transport_retry_base_delay_s),
            max_total_duration_s=exec_cfg.transport_retry_max_total_s,
        )

        # On rollback, reset turn-local side effects and promote the ledger's
        # completed calls so a retried provider turn replays them safely.
        reset_fns: list[Callable[[], None]] = []
        _ledger = getattr(self, "_turn_ledger", None)
        if _ledger is not None:
            reset_fns.append(_ledger.promote)
        if ctx.approval_svc is not None:
            reset_fns.append(ctx.approval_svc.reset_turn_memory)
        workspace_access = ctx.workspace_access
        if workspace_access is None:
            from agenthicc.tools.workspace_access import current_workspace_access

            workspace_access = current_workspace_access()
        if workspace_access is not None:
            reset_fns.append(workspace_access.reset_turn_memory)
        if self._reset_attempt_output is not None:
            reset_fns.append(self._reset_attempt_output)

        if self._step_recovery_supported:
            checkpoint_provider = self._safe_snapshot_provider
        elif ctx.session_memory is not None and hasattr(ctx.session_memory, "snapshot"):
            # Older lauren-ai versions do not expose committed-step receipts.
            # Snapshot the *current* projection after a failure so retrying the
            # provider cannot erase earlier assistant/tool messages. The
            # idempotency ledger handles calls that completed before the error.
            checkpoint_provider = ctx.session_memory.snapshot
        else:
            checkpoint_provider = None

        await run_with_transport_retry(
            stream_once,
            config=config,
            memory=ctx.session_memory,
            deadline_monotonic=ctx.retry_deadline_monotonic,
            on_retry=self._emit_retry,
            on_irrecoverable_retry=self._emit_irrecoverable_retry,
            reset_fns=reset_fns,
            checkpoint_provider=checkpoint_provider,
        )

    async def _emit_retry(
        self, attempt: int, max_retries: int, delay: float, exc: BaseException
    ) -> None:
        """Observability + user notification for a scheduled transport retry."""
        ctx = self._ctx
        if ctx.conv_store is not None:
            ctx.conv_store.append_event(
                "network_retry",
                {
                    "attempt": attempt,
                    "max_retries": max_retries,
                    "delay_s": delay,
                    "reason": _network_retry_reason(exc),
                    "detail": _network_retry_detail(exc),
                    "status_code": _http_status_code(exc),
                },
            )
        import logging as _logging

        # The scroll appender is the user-facing notification surface. Keep
        # the full exception for debug logs rather than printing a nested SDK
        # repr into the transcript alongside the compact retry event.
        _logging.getLogger(__name__).debug(
            "Transient network error on attempt %d/%d, retrying in %.1fs: %s: %s",
            attempt,
            max_retries,
            delay,
            type(exc).__name__,
            exc,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        if ctx.processor is not None:
            from agenthicc.kernel import Event

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

    async def _emit_irrecoverable_retry(
        self, attempt: int, max_retries: int, delay: float, exc: BaseException
    ) -> None:
        """Report a bounded retry of a provider-rejected request (PRD-198)."""
        ctx = self._ctx
        recovery_id = f"provider-recovery:{self._intent_id}"
        payload = {
            "recovery_id": recovery_id,
            "attempt": attempt,
            "max_retries": max_retries,
            "delay_s": delay,
            "reason": "Provider rejected the request",
            "detail": _network_retry_detail(exc),
            "status_code": _http_status_code(exc),
            "category": "irrecoverable_provider",
        }
        if ctx.conv_store is not None:
            ctx.conv_store.append_event(
                "provider_recovery_retry",
                payload,
                event_id=f"{recovery_id}:retry:{attempt}",
            )
        import logging as _logging

        _logging.getLogger(__name__).debug(
            "Irrecoverable provider error on recovery retry %d/%d, retrying in %.1fs: %s: %s",
            attempt,
            max_retries,
            delay,
            type(exc).__name__,
            exc,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        if ctx.processor is not None:
            from agenthicc.kernel import Event

            await ctx.processor.emit(
                Event.create(
                    "ProviderRecoveryRetryScheduled",
                    {
                        "scope": "agent_turn",
                        "recovery_id": recovery_id,
                        "attempt": attempt,
                        "max_retries": max_retries,
                        "delay_s": delay,
                        "error_type": type(exc).__name__,
                        "category": "irrecoverable_provider",
                    },
                )
            )

    # ── step 9: kernel completion event ───────────────────────────────────────

    async def _emit_intent_complete(self, status: str = "complete") -> None:
        from agenthicc.kernel import Event

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
    mcp_registry: McpToolRegistry | McpSessionManager | None = None,
    active_agent: str | None = None,
    agent_spec: "AgentSpec | None" = None,
    agents_registry: "AgentsRegistry | None" = None,
    agent_config: "AgenthiccConfig | None" = None,
    completed_turns: int = 0,
    approval_svc: ApprovalBroker | None = None,
    output_collector: list[str] | None = None,
    system_prompt_suffix: str = "",
    prompt_contract: "PromptContract | None" = None,
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
    workspace_access: "WorkspaceAccessPolicy | None" = None,
    provider_diagnostic_context: dict[str, str | int] | None = None,
) -> None:
    """Thin shim — constructs AgentTurnContext and delegates to AgentTurnRunner.

    All existing call sites continue to work without modification.
    """
    if session_memory is None:
        from lauren_ai._memory import ShortTermMemory as _ShortTermMemory

        session_memory = _ShortTermMemory()

    if resume_turn_id is None and resume_ledger is None:
        from agenthicc.runners.workflow_execution_context import (
            consume_workflow_resume_plan,
        )

        workflow_resume_plan = consume_workflow_resume_plan()
        if workflow_resume_plan is not None:
            plan_turn_id = workflow_resume_plan.turn_id
            if isinstance(plan_turn_id, str) and plan_turn_id:
                resume_turn_id = plan_turn_id
                resume_ledger = cast("IdempotencyLedger", workflow_resume_plan.ledger)

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
        agent_spec=agent_spec,
        agents_registry=agents_registry,
        agent_config=agent_config,
        completed_turns=completed_turns,
        approval_svc=approval_svc,
        workspace_access=workspace_access,
        output_collector=output_collector,
        system_prompt_suffix=system_prompt_suffix,
        prompt_contract=prompt_contract,
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
        provider_diagnostic_context=provider_diagnostic_context,
    )
    await AgentTurnRunner(ctx).run()
