"""Cache-stable prompt and tool composition for workflow turns.

The lauren-ai 1.4 public agent API accepts one system string and one flat tool
list.  This module provides the logical structured contract that agenthicc
needs at that boundary: the stable system prefix is sent as the system string,
while changing phase context is appended to the user turn.  Tools are ordered
with stable tools first and phase-local tools second so providers that support
prefix caching can reuse the longest safe prefix.

The contract deliberately contains no provider-specific cache-control objects.
Provider transports remain responsible for interpreting the deterministic
ordering, while older providers can safely consume the compatibility rendering.
"""

from __future__ import annotations

import dataclasses
import hashlib
import inspect
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agenthicc.tools.base import ToolLike

__all__ = [
    "CACHE_CONTRACT_VERSION",
    "DEFAULT_WORKFLOW_CACHE_POLICY",
    "CacheEpoch",
    "PromptBlock",
    "PromptContract",
    "build_prompt_contract",
    "build_workflow_prompt_contract",
    "tool_name",
    "tool_fingerprint",
]

CACHE_CONTRACT_VERSION = "agenthicc.prompt-cache.v1"

# This policy is stable by design.  Actual questions and answers belong in the
# dynamic request context and therefore never change the system cache prefix.
DEFAULT_WORKFLOW_CACHE_POLICY = (
    "[WORKFLOW EXECUTION CONTRACT]\n"
    "Keep the original user goal and the complete prior conversation in mind. "
    "Phase state, artifacts, validation reports, questions, answers, and transition "
    "details are dynamic context; do not treat them as permanent workflow policy.\n\n"
    "[REQUIREMENTS CLARIFICATION POLICY]\n"
    "Ask the user a focused clarifying question whenever required information is "
    "missing, ambiguous, or would materially change the result. Use the existing "
    "`ask_user` tool, wait for its answer, and do not guess over a material ambiguity. "
    "The question policy is stable; each actual question and answer remains dynamic.\n\n"
    "[CACHE SAFETY POLICY]\n"
    "Keep stable instructions and stable tool schemas deterministic. Do not insert "
    "messages near the beginning of conversation history, rewrite old messages, or "
    "put a rolling summary into the stable system prompt. Prompt caching never "
    "replaces capability filtering, approval, or tool authorization."
)


def _jsonable(value: object) -> object:
    """Return a bounded, deterministic JSON representation of *value*."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple, set, frozenset)):
        values = [_jsonable(item) for item in value]
        if isinstance(value, (set, frozenset)):
            return sorted(values, key=lambda item: json.dumps(item, sort_keys=True))
        return values
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _jsonable(getattr(value, field.name)) for field in dataclasses.fields(value)
        }
    return str(value)


def _digest(value: object) -> str:
    raw = json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def tool_name(tool: object) -> str:
    """Return the provider-facing name of a callable or Tool object."""

    value = getattr(tool, "__name__", None)
    if isinstance(value, str) and value:
        return value
    value = getattr(tool, "name", None)
    if isinstance(value, str) and value:
        return value
    value = getattr(tool, "provider_name", None)
    if isinstance(value, str) and value:
        return value
    return type(tool).__qualname__


def _tool_metadata(tool: object) -> object | None:
    try:
        from lauren_ai._tools import TOOL_META  # noqa: PLC0415

        return getattr(tool, TOOL_META, None)
    except ImportError:
        return None


def _tool_schema(tool: object) -> dict[str, object]:
    """Extract only schema metadata; never include callable repr/address data."""

    metadata = _tool_metadata(tool)
    parameters = getattr(metadata, "parameters", None)
    description = getattr(metadata, "description", None)
    if parameters is None:
        parameters = getattr(tool, "parameters", None)
    if description is None:
        description = getattr(tool, "description", None)
    if parameters is None:
        try:
            signature = str(inspect.signature(tool))
        except (TypeError, ValueError):
            signature = ""
        parameters = {"signature": signature}
    return {
        "name": tool_name(tool),
        "description": str(description or (getattr(tool, "__doc__", "") or "").strip()),
        "parameters": _jsonable(parameters),
    }


def tool_fingerprint(tool: object) -> str:
    """Return a stable schema fingerprint for one tool."""

    return _digest(_tool_schema(tool))


@dataclass(frozen=True)
class PromptBlock:
    """Named dynamic prompt content rendered after the stable system prefix."""

    kind: str
    content: str

    def __post_init__(self) -> None:
        if not self.kind.strip():
            raise ValueError("prompt block kind must not be empty")
        if not isinstance(self.content, str):
            raise TypeError("prompt block content must be a string")


@dataclass(frozen=True)
class CacheEpoch:
    """Redacted identity of one stable prompt/tool contract."""

    value: str
    reason: str = "initial"


@dataclass(frozen=True)
class PromptContract:
    """Immutable logical prompt contract for one workflow execution epoch."""

    stable_system_prefix: str
    dynamic_system_context: tuple[PromptBlock, ...]
    stable_tools: tuple["ToolLike", ...]
    phase_tools: tuple["ToolLike", ...]
    cache_epoch: CacheEpoch
    stable_fingerprint: str
    dynamic_fingerprint: str
    provider_capability: str = "unknown"
    connection_fingerprint: str = ""

    @property
    def contract_version(self) -> str:
        return CACHE_CONTRACT_VERSION

    @property
    def cache_status(self) -> str:
        """Return local eligibility, never claiming a provider cache hit."""

        if self.provider_capability == "unsupported":
            return "unsupported"
        if self.provider_capability == "unknown":
            return "unknown"
        return "eligible"

    @property
    def stable_tool_names(self) -> frozenset[str]:
        return frozenset(tool_name(tool) for tool in self.stable_tools)

    def ordered_tools(self, visible_tools: Sequence["ToolLike"]) -> list["ToolLike"]:
        """Order visible tools by stable region, then phase-local region.

        The caller's visible list remains authoritative for capability and
        allowlist filtering.  Unknown tools are treated as phase-local and are
        sorted deterministically instead of being silently discarded.
        """

        by_name: dict[str, ToolLike] = {}
        for tool in visible_tools:
            name = tool_name(tool)
            existing = by_name.get(name)
            if existing is not None and tool_fingerprint(existing) != tool_fingerprint(tool):
                raise ValueError(f"conflicting schemas for tool {name!r}")
            by_name[name] = tool

        stable_names = self.stable_tool_names
        stable = sorted(
            (tool for name, tool in by_name.items() if name in stable_names),
            key=lambda item: (tool_name(item), tool_fingerprint(item)),
        )
        phase = sorted(
            (tool for name, tool in by_name.items() if name not in stable_names),
            key=lambda item: (tool_name(item), tool_fingerprint(item)),
        )
        return [*stable, *phase]

    def render_dynamic_message(
        self,
        message: str,
        *,
        extra_blocks: Iterable[PromptBlock] = (),
    ) -> str:
        """Render dynamic blocks as an append-only user context message."""

        blocks = [*self.dynamic_system_context, *extra_blocks]
        sections = [f"[{block.kind}]\n{block.content}" for block in blocks if block.content.strip()]
        if message.strip():
            sections.append(f"[CURRENT TURN]\n{message}")
        return "\n\n".join(sections)

    def diagnostics(self) -> dict[str, object]:
        """Return redacted diagnostics suitable for journals and checkpoints."""

        return {
            "contract_version": self.contract_version,
            "cache_epoch": self.cache_epoch.value,
            "stable_fingerprint": self.stable_fingerprint,
            "connection_fingerprint": self.connection_fingerprint,
            "dynamic_fingerprint": self.dynamic_fingerprint,
            "provider_capability": self.provider_capability,
            "cache_status": self.cache_status,
            "stable_tool_count": len(self.stable_tools),
            "phase_tool_count": len(self.phase_tools),
            "stable_tool_names": sorted(self.stable_tool_names),
        }


def _canonical_tools(tools: Iterable["ToolLike"]) -> tuple["ToolLike", ...]:
    by_name: dict[str, ToolLike] = {}
    for tool in tools:
        name = tool_name(tool)
        existing = by_name.get(name)
        if existing is not None and tool_fingerprint(existing) != tool_fingerprint(tool):
            raise ValueError(f"conflicting schemas for tool {name!r}")
        by_name[name] = tool
    return tuple(
        sorted(by_name.values(), key=lambda item: (tool_name(item), tool_fingerprint(item)))
    )


def _provider_capability(provider: str) -> str:
    normalized = provider.strip().lower()
    if normalized == "anthropic":
        return "explicit"
    if normalized in {"openai", "modal", "openai-compatible"}:
        return "automatic"
    if normalized in {"ollama", "litellm"}:
        return "unsupported"
    return "unknown"


def build_prompt_contract(
    *,
    stable_system_prefix: str = "",
    dynamic_system_context: Iterable[PromptBlock] = (),
    stable_tools: Iterable["ToolLike"] = (),
    phase_tools: Iterable["ToolLike"] = (),
    provider: str = "",
    model: str = "",
    profile: str = "",
    connection_identity: Mapping[str, object] | None = None,
) -> PromptContract:
    """Build and validate one deterministic workflow prompt contract."""

    stable = _canonical_tools(stable_tools)
    phase = _canonical_tools(phase_tools)
    stable_names = {tool_name(tool) for tool in stable}
    overlap = stable_names.intersection(tool_name(tool) for tool in phase)
    if overlap:
        names = ", ".join(sorted(overlap))
        raise ValueError(f"tools cannot be both stable and phase-local: {names}")

    blocks = tuple(dynamic_system_context)
    stable_text = "\n\n".join(
        part.strip()
        for part in (DEFAULT_WORKFLOW_CACHE_POLICY, stable_system_prefix)
        if part.strip()
    )
    stable_payload = {
        "version": CACHE_CONTRACT_VERSION,
        "stable_system": stable_text,
        "stable_tools": [_tool_schema(tool) for tool in stable],
    }
    stable_fingerprint = _digest(stable_payload)
    connection_fingerprint = _digest(
        {
            "provider": provider,
            "model": model,
            "profile": profile,
            "connection": dict(connection_identity or {}),
        }
    )
    epoch = CacheEpoch(
        _digest({"stable": stable_fingerprint, "connection": connection_fingerprint})
    )
    dynamic_fingerprint = _digest(
        [{"kind": block.kind, "content": block.content} for block in blocks]
    )
    return PromptContract(
        stable_system_prefix=stable_text,
        dynamic_system_context=blocks,
        stable_tools=stable,
        phase_tools=phase,
        cache_epoch=epoch,
        stable_fingerprint=stable_fingerprint,
        dynamic_fingerprint=dynamic_fingerprint,
        provider_capability=_provider_capability(provider),
        connection_fingerprint=connection_fingerprint,
    )


def build_workflow_prompt_contract(
    *,
    workflow_name: str,
    phase_prompt: str,
    stable_system_prefix: str = "",
    stable_tools: Iterable["ToolLike"] = (),
    phase_tools: Iterable["ToolLike"] = (),
    execution: object | None = None,
    extra_blocks: Iterable[PromptBlock] = (),
) -> PromptContract:
    """Build the standard contract used by built-in and generated workflows."""

    provider = str(getattr(execution, "provider", "unknown") or "unknown")
    model_value = getattr(execution, "effective_model", None)
    model = str(model_value() if callable(model_value) else getattr(execution, "model", ""))
    profile = str(getattr(execution, "profile", "") or "")
    base_url = str(getattr(execution, "base_url", "") or "")
    blocks = [PromptBlock("PHASE INSTRUCTIONS", phase_prompt), *extra_blocks]
    return build_prompt_contract(
        stable_system_prefix="\n\n".join(
            part for part in (f"[WORKFLOW: {workflow_name}]", stable_system_prefix) if part
        ),
        dynamic_system_context=blocks,
        stable_tools=stable_tools,
        phase_tools=phase_tools,
        provider=provider,
        model=model,
        profile=profile,
        # The URL contributes only to the digest; it is never returned by
        # diagnostics or persisted as a raw connection value.
        connection_identity={"base_url_digest": _digest(base_url)},
    )
