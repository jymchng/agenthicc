"""Authoring catalog and effective-session snapshot for create_workflow.

The catalog is deliberately derived from live callable metadata and the active
session rather than from a second hand-maintained tool list. It is bounded,
deterministic, and safe to pass to an authoring agent. Runtime objects such as
tool callables, browser clients, MCP bridges, locks, and secrets never enter
the snapshot.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from agenthicc.runners.prompt_contract import _tool_schema, tool_fingerprint, tool_name
from agenthicc.tools.base import ToolLike
from agenthicc.tools.capabilities import (
    ToolCapability,
    classify_tool_capabilities,
    get_tool_capabilities,
    is_exploratory_tool,
)

if TYPE_CHECKING:
    from agenthicc.workflows.config import WorkflowConfig

__all__ = [
    "AUTHORING_CATALOG_VERSION",
    "AuthoringSnapshotCache",
    "AuthoringSnapshot",
    "ToolAccessDecision",
    "ToolCatalogEntry",
    "build_authoring_snapshot",
    "build_tool_catalog",
    "explain_tool_access",
]

AUTHORING_CATALOG_VERSION = "agenthicc.authoring-catalog.v1"
_MAX_DESCRIPTION = 2_000
_MAX_SCHEMA_BYTES = 32_000
_MAX_TOOLS = 512
_MAX_LIST_ITEMS = 128
_MAX_SNAPSHOT_BYTES = 256_000
_SECRET_KEY_RE = re.compile(
    r"(?i)(?:api[_ -]?key|authorization|cookie|password|secret|token|private[_ -]?key)"
)


def _bounded_text(value: object, limit: int = _MAX_DESCRIPTION) -> str:
    """Return a stable, bounded text value."""
    return str(value or "").replace("\x00", "").strip()[:limit]


def _redact(value: object, *, key: str = "") -> object:
    """Redact values under secret-bearing schema keys without changing shape."""
    if isinstance(value, Mapping):
        return {
            str(name): (
                "[redacted]" if _SECRET_KEY_RE.search(str(name)) else _redact(item, key=str(name))
            )
            for name, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_redact(item, key=key) for item in list(value)[:_MAX_LIST_ITEMS]]
    if isinstance(value, str):
        return (
            "[redacted]" if _SECRET_KEY_RE.search(key) else _bounded_text(value, _MAX_SCHEMA_BYTES)
        )
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _bounded_text(value)


def _schema(tool: object) -> dict[str, object]:
    """Extract provider schema and redact dangerous metadata values."""
    raw = _tool_schema(tool)
    result = _redact(raw)
    if not isinstance(result, dict):
        return {"name": tool_name(tool), "description": "", "parameters": {}}
    encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) <= _MAX_SCHEMA_BYTES:
        return result
    return {
        "name": tool_name(tool),
        "description": _bounded_text(result.get("description", "")),
        "parameters": {"type": "object", "properties": {}, "bounded": True},
    }


def _source_for(tool: object, source_by_name: Mapping[str, str]) -> str:
    """Infer a safe source label, allowing callers to provide exact provenance."""
    name = tool_name(tool)
    explicit = source_by_name.get(name)
    if explicit:
        return _bounded_text(explicit, 64)
    lowered = name.lower()
    module = str(getattr(tool, "__module__", "")).lower()
    if lowered.startswith(("cloakbrowser_", "playwright_")):
        return "browser"
    if lowered.startswith("mcp_") or "tools.mcp" in module:
        return "mcp"
    if lowered.startswith(("memory_", "semantic_")) or "memory" in module:
        return "memory"
    if module.startswith("agenthicc"):
        return "builtin"
    return "plugin"


@dataclass(frozen=True, slots=True)
class ToolCatalogEntry:
    """Bounded, model-facing metadata for one visible or filtered tool."""

    name: str
    description: str
    parameters: Mapping[str, object]
    capabilities: tuple[str, ...]
    exploratory: bool
    source: str
    available: bool = True
    availability_reason: str = ""
    optional_dependency: str = ""
    backend: str = ""
    fingerprint: str = ""

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("tool catalog entries require a name")
        if not self.fingerprint:
            object.__setattr__(
                self,
                "fingerprint",
                hashlib.sha256(
                    json.dumps(self.to_dict(include_fingerprint=False), sort_keys=True).encode(
                        "utf-8"
                    )
                ).hexdigest(),
            )

    def to_dict(self, *, include_fingerprint: bool = True) -> dict[str, object]:
        """Return a JSON-compatible redacted representation."""
        result: dict[str, object] = {
            "name": self.name,
            "description": self.description,
            "parameters": dict(self.parameters),
            "capabilities": list(self.capabilities),
            "exploratory": self.exploratory,
            "source": self.source,
            "available": self.available,
            "availability_reason": self.availability_reason,
            "optional_dependency": self.optional_dependency,
            "backend": self.backend,
        }
        if include_fingerprint:
            result["fingerprint"] = self.fingerprint
        return result


@dataclass(frozen=True, slots=True)
class ToolAccessDecision:
    """Explain why a tool is available or unavailable for a phase."""

    tool_name: str
    declared_capabilities: tuple[str, ...]
    phase_capabilities: tuple[str, ...]
    mode_blocked_capabilities: tuple[str, ...]
    available: bool
    reasons: tuple[str, ...]
    optional_dependency: str = ""
    backend: str = ""
    policy_constraints: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "tool_name": self.tool_name,
            "declared_capabilities": list(self.declared_capabilities),
            "phase_capabilities": list(self.phase_capabilities),
            "mode_blocked_capabilities": list(self.mode_blocked_capabilities),
            "available": self.available,
            "reasons": list(self.reasons),
            "optional_dependency": self.optional_dependency,
            "backend": self.backend,
            "policy_constraints": list(self.policy_constraints),
        }


@dataclass(frozen=True, slots=True)
class AuthoringSnapshot:
    """Immutable effective authoring contract for one create_workflow run."""

    catalog_version: str = AUTHORING_CATALOG_VERSION
    snapshot_id: str = ""
    active_mode: str = ""
    blocked_capabilities: tuple[str, ...] = ()
    phase_name: str = ""
    phase_role: str = "auto"
    phase_capability_source: str = "role_default"
    phase_capabilities: tuple[str, ...] = ()
    tools: tuple[ToolCatalogEntry, ...] = ()
    browser: Mapping[str, object] = field(default_factory=dict)
    mcp: tuple[Mapping[str, object], ...] = ()
    workspace: Mapping[str, object] = field(default_factory=dict)
    cache: Mapping[str, object] = field(default_factory=dict)
    checkpoint: Mapping[str, object] = field(default_factory=dict)
    unavailable: tuple[Mapping[str, object], ...] = ()

    def __post_init__(self) -> None:
        if not self.snapshot_id:
            object.__setattr__(self, "snapshot_id", self._compute_id())

    def _compute_id(self) -> str:
        payload = self.to_dict(include_id=False)
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()

    @property
    def stable_fingerprint(self) -> str:
        """Return the stable prompt identity for this snapshot."""
        return self.snapshot_id

    def to_dict(self, *, include_id: bool = True) -> dict[str, object]:
        """Return a bounded JSON-compatible snapshot without runtime objects."""
        result: dict[str, object] = {
            "catalog_version": self.catalog_version,
            "active_mode": self.active_mode,
            "blocked_capabilities": list(self.blocked_capabilities),
            "phase_name": self.phase_name,
            "phase_role": self.phase_role,
            "phase_capability_source": self.phase_capability_source,
            "phase_capabilities": list(self.phase_capabilities),
            "tools": [tool.to_dict() for tool in self.tools[:_MAX_TOOLS]],
            "tool_count": len(self.tools),
            "browser": _redact(dict(self.browser)),
            "mcp": [_redact(dict(item)) for item in self.mcp[:_MAX_LIST_ITEMS]],
            "workspace": _redact(dict(self.workspace)),
            "cache": _redact(dict(self.cache)),
            "checkpoint": _redact(dict(self.checkpoint)),
            "unavailable": [_redact(dict(item)) for item in self.unavailable[:_MAX_LIST_ITEMS]],
        }
        # A catalog can contain provider-owned schemas.  Each schema is bounded
        # independently, but the complete snapshot also needs a hard envelope
        # so a large number of legitimate tools cannot turn authoring into an
        # unbounded prompt or checkpoint operation.
        base = dict(result)
        base["tools_truncated"] = False
        encoded = json.dumps(
            {**base, **({"snapshot_id": self.snapshot_id} if include_id else {})},
            ensure_ascii=False,
            sort_keys=True,
        )
        if len(encoded.encode("utf-8")) > _MAX_SNAPSHOT_BYTES:
            base["tools"] = []
            base["tools_truncated"] = True
            for entry in self.tools[:_MAX_TOOLS]:
                existing_tools = base.get("tools")
                if not isinstance(existing_tools, list):
                    existing_tools = []
                candidate_tools = [*existing_tools, entry.to_dict()]
                candidate = {**base, "tools": candidate_tools}
                if include_id:
                    candidate["snapshot_id"] = self.snapshot_id
                candidate_encoded = json.dumps(candidate, ensure_ascii=False, sort_keys=True)
                if len(candidate_encoded.encode("utf-8")) > _MAX_SNAPSHOT_BYTES:
                    break
                base = candidate
            # The known session summaries are bounded, but keep a final
            # fail-safe for embedders constructing an AuthoringSnapshot by
            # hand with an unusually large mapping.  Preserve the contract
            # identity and phase fields while dropping optional detail.
            base_encoded = json.dumps(
                {**base, **({"snapshot_id": self.snapshot_id} if include_id else {})},
                ensure_ascii=False,
                sort_keys=True,
            )
            if len(base_encoded.encode("utf-8")) > _MAX_SNAPSHOT_BYTES:
                for key in ("unavailable", "mcp", "workspace", "cache", "checkpoint", "browser"):
                    base[key] = [] if key in {"unavailable", "mcp"} else {}
                    base["metadata_truncated"] = True
                    base_encoded = json.dumps(
                        {**base, **({"snapshot_id": self.snapshot_id} if include_id else {})},
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    if len(base_encoded.encode("utf-8")) <= _MAX_SNAPSHOT_BYTES:
                        break
            result = base
        if include_id:
            result["snapshot_id"] = self.snapshot_id
        return result

    def checkpoint_reference(self) -> dict[str, object]:
        """Return the only representation permitted in a workflow checkpoint."""
        return {
            "catalog_version": self.catalog_version,
            "snapshot_id": self.snapshot_id,
            "tool_fingerprints": [tool.fingerprint for tool in self.tools[:_MAX_TOOLS]],
            "tool_names": [tool.name for tool in self.tools[:_MAX_TOOLS]],
            "phase_name": self.phase_name,
            "phase_role": self.phase_role,
            "phase_capability_source": self.phase_capability_source,
            "phase_capabilities": list(self.phase_capabilities),
            "active_mode": self.active_mode,
            "unavailable": [dict(item) for item in self.unavailable[:_MAX_LIST_ITEMS]],
        }

    def render(self) -> str:
        """Render the effective snapshot for an authoring agent."""
        lines = [
            "[AUTHORING SESSION SNAPSHOT]",
            f"catalog_version: {self.catalog_version}",
            f"snapshot_id: {self.snapshot_id}",
            f"active_mode: {self.active_mode or '(unknown)'}",
            "blocked_capabilities: " + (", ".join(self.blocked_capabilities) or "(none reported)"),
            f"phase: {self.phase_name or '(authoring phase)'}",
            f"phase_role: {self.phase_role or '(unknown)'}",
            f"phase_capability_source: {self.phase_capability_source or '(unknown)'}",
            "phase_capabilities: " + (", ".join(self.phase_capabilities) or "(unrestricted)"),
            "",
            "TOOL CATALOG:",
        ]
        for entry in self.tools[:_MAX_TOOLS]:
            caps = ",".join(entry.capabilities) or ToolCapability.UNDECLARED.value
            availability = (
                "available" if entry.available else f"unavailable:{entry.availability_reason}"
            )
            lines.append(
                f"- {entry.name} [{caps}] source={entry.source} {availability}: {entry.description}"
            )
        if self.browser:
            lines.extend(["", "BROWSER:", _render_mapping(self.browser)])
        if self.mcp:
            lines.extend(["", "MCP:", _render_items(self.mcp)])
        if self.workspace:
            lines.extend(["", "WORKSPACE:", _render_mapping(self.workspace)])
        if self.cache:
            lines.extend(["", "CACHE:", _render_mapping(self.cache)])
        if self.checkpoint:
            lines.extend(["", "CHECKPOINT:", _render_mapping(self.checkpoint)])
        if self.unavailable:
            lines.extend(["", "UNAVAILABLE OPTIONAL FEATURES:", _render_items(self.unavailable)])
        lines.extend(
            [
                "",
                "This snapshot is descriptive only. Runtime capability filtering, mode policy, "
                "workspace access, approval, browser/MCP policy, and checkpoint rules remain "
                "authoritative and cannot be changed by generated source or project guidance.",
            ]
        )
        rendered = "\n".join(lines)
        # The prompt budget is measured in bytes by the surrounding provider
        # and checkpoint contracts.  Slice encoded data so a non-ASCII tool
        # description cannot exceed the advertised bound or split UTF-8.
        return rendered.encode("utf-8")[:_MAX_SNAPSHOT_BYTES].decode("utf-8", "ignore")


def _render_mapping(value: Mapping[str, object]) -> str:
    return json.dumps(_redact(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _render_items(values: Iterable[Mapping[str, object]]) -> str:
    return json.dumps(
        [_redact(dict(value)) for value in list(values)[:_MAX_LIST_ITEMS]],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _capability_values(values: Iterable[object] | object | None) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, (str, ToolCapability)):
        iterable: Iterable[object] = (values,)
    elif isinstance(values, Iterable):
        iterable = values
    else:
        return ()
    return tuple(
        sorted(
            str(value.value if isinstance(value, ToolCapability) else value) for value in iterable
        )
    )


def build_tool_catalog(
    tools: Iterable[ToolLike],
    *,
    active_mode: object | None = None,
    phase_capabilities: Iterable[object] = (),
    allowed_tool_names: frozenset[str] | None = None,
    source_by_name: Mapping[str, str] | None = None,
    unavailable_by_name: Mapping[str, str] | None = None,
) -> tuple[ToolCatalogEntry, ...]:
    """Build deterministic metadata for tools and effective decisions."""
    blocked_raw = getattr(active_mode, "blocked_capabilities", ())
    blocked = set(_capability_values(blocked_raw)) if blocked_raw else set()
    phase = set(_capability_values(phase_capabilities))
    source_map = source_by_name or {}
    unavailable = unavailable_by_name or {}
    by_name: dict[str, ToolCatalogEntry] = {}
    for tool in tools:
        name = tool_name(tool)
        if name in by_name:
            continue
        caps = classify_tool_capabilities(get_tool_capabilities(tool))
        cap_values = tuple(sorted(caps))
        schema = _schema(tool)
        reason = unavailable.get(name, "")
        available = not reason
        if allowed_tool_names is not None and name not in allowed_tool_names:
            available = False
            reason = reason or "phase_allowlist"
        if caps.intersection(blocked):
            available = False
            reason = reason or "active_mode_blocks_capability"
        if (
            phase
            and not set(cap_values) <= phase
            and ToolCapability.CONTROL.value not in cap_values
        ):
            available = False
            reason = reason or "phase_capability_mismatch"
        parameters = _redact(schema.get("parameters", {}))
        by_name[name] = ToolCatalogEntry(
            name=name,
            description=_bounded_text(schema.get("description", "")),
            parameters=parameters if isinstance(parameters, Mapping) else {},
            capabilities=cap_values,
            exploratory=is_exploratory_tool(tool),
            source=_source_for(tool, source_map),
            available=available,
            availability_reason=reason,
            optional_dependency=(
                "cloakbrowser"
                if name.startswith("cloakbrowser_")
                else "playwright"
                if name.startswith("playwright_")
                else ""
            ),
            backend=(
                "cloakbrowser"
                if name.startswith("cloakbrowser_")
                else "playwright"
                if name.startswith("playwright_")
                else ""
            ),
            fingerprint=tool_fingerprint(tool),
        )
    return tuple(by_name[name] for name in sorted(by_name)[:_MAX_TOOLS])


def explain_tool_access(
    entry: ToolCatalogEntry,
    *,
    active_mode: object | None = None,
    phase_capabilities: Iterable[object] = (),
    allowed_tool_names: frozenset[str] | None = None,
    policy_constraints: Iterable[str] = (),
) -> ToolAccessDecision:
    """Return the deterministic decision trace for one catalog entry."""
    blocked_raw = getattr(active_mode, "blocked_capabilities", ())
    blocked = set(_capability_values(blocked_raw)) if blocked_raw else set()
    phase = set(_capability_values(phase_capabilities))
    reasons: list[str] = []
    available = entry.available
    if entry.availability_reason:
        reasons.append(entry.availability_reason)
    if allowed_tool_names is not None and entry.name not in allowed_tool_names:
        available = False
        reasons.append("phase_allowlist")
    blocked_caps = tuple(sorted(set(entry.capabilities).intersection(blocked)))
    if blocked_caps:
        available = False
        reasons.append("active_mode_blocks_capability")
    if phase and not set(entry.capabilities) <= phase:
        if ToolCapability.CONTROL.value not in entry.capabilities:
            available = False
        reasons.append("phase_capability_mismatch")
    return ToolAccessDecision(
        tool_name=entry.name,
        declared_capabilities=entry.capabilities,
        phase_capabilities=tuple(sorted(phase)),
        mode_blocked_capabilities=tuple(sorted(blocked)),
        available=available,
        reasons=tuple(dict.fromkeys(reasons)),
        optional_dependency=entry.optional_dependency,
        backend=entry.backend,
        policy_constraints=tuple(dict.fromkeys(str(item) for item in policy_constraints)),
    )


def _safe_browser_summary(config: WorkflowConfig) -> dict[str, object]:
    manager = getattr(config, "browser_manager", None)
    if manager is None:
        return {
            "selected": False,
            "selected_backend": "none",
            "status": "not_configured",
            "dependency_status": "not_selected",
        }
    settings = getattr(manager, "settings", None)
    backend = _bounded_text(getattr(manager, "backend_name", ""), 64)
    client = getattr(manager, "client", None)
    client_name = type(client).__name__.lower() if client is not None else ""
    dependency_status = "not_probed"
    health = getattr(client, "_health", None)
    health_status = getattr(health, "status", None)
    if isinstance(health_status, str) and health_status:
        # UnavailableBrowserClient exposes a safe, precomputed health value;
        # reading it does not invoke a probe.  Do not inspect its message,
        # which can contain environment-specific details.
        dependency_status = _bounded_text(health_status, 64)
    elif "unavailable" in client_name:
        dependency_status = "unavailable"
    return {
        "selected": True,
        "selected_backend": backend.lower() or "unknown",
        "status": dependency_status,
        "backend": backend,
        "enabled": bool(getattr(manager, "enabled", False)),
        "configured": settings is not None,
        "dependency_status": dependency_status,
        "allow_all_domains": bool(getattr(settings, "allow_all_domains", False)),
        "optional_dependency": "cloakbrowser" if "cloak" in backend.lower() else "playwright",
        "health_probe": "not_requested",
    }


def _safe_mcp_summary(config: WorkflowConfig) -> tuple[Mapping[str, object], ...]:
    manager = getattr(config, "mcp_registry", None)
    if manager is None:
        return ()
    status_fn = getattr(manager, "status", None)
    if not callable(status_fn):
        return ()
    try:
        raw = status_fn()
    except Exception:
        return ({"server": "(unavailable)", "state": "status_error"},)
    if not isinstance(raw, Mapping):
        raw = {}
    tools_by_server: dict[str, list[str]] = {}
    catalog_fn = getattr(manager, "catalog", None)
    if callable(catalog_fn):
        try:
            catalog = catalog_fn()
        except Exception:
            catalog = ()
        if isinstance(catalog, Iterable) and not isinstance(catalog, (str, bytes, Mapping)):
            for item in catalog:
                if not isinstance(item, Mapping):
                    continue
                server = _bounded_text(item.get("server", ""), 128)
                name = _bounded_text(item.get("canonical_name", item.get("name", "")), 128)
                if server and name:
                    tools_by_server.setdefault(server, []).append(name)
    if not tools_by_server:
        all_tools_fn = getattr(manager, "all_tools", None)
        if callable(all_tools_fn):
            try:
                all_tools = all_tools_fn()
            except Exception:
                all_tools = ()
            if isinstance(all_tools, Iterable) and not isinstance(all_tools, (str, bytes, Mapping)):
                for tool in all_tools:
                    bridge = getattr(tool, "_bridge", None)
                    server = _bounded_text(getattr(bridge, "server_name", ""), 128)
                    name = _bounded_text(
                        getattr(tool, "name", getattr(tool, "provider_name", "")), 128
                    )
                    if server and name:
                        tools_by_server.setdefault(server, []).append(name)
    result: list[Mapping[str, object]] = []
    for name, value in sorted(raw.items(), key=lambda item: str(item[0])):
        if isinstance(value, Mapping):
            server = _bounded_text(name, 128)
            tool_names = sorted(set(tools_by_server.get(server, ())))[:_MAX_LIST_ITEMS]
            raw_names = value.get("tool_names", ())
            if not tool_names and isinstance(raw_names, (list, tuple, set, frozenset)):
                tool_names = sorted(
                    {
                        _bounded_text(item, 128)
                        for item in raw_names
                        if isinstance(item, str) and item.strip()
                    }
                )[:_MAX_LIST_ITEMS]
            raw_count = value.get("tool_count", 0)
            tool_count = (
                raw_count
                if isinstance(raw_count, int) and not isinstance(raw_count, bool) and raw_count >= 0
                else len(tool_names)
            )
            result.append(
                {
                    "server": server,
                    "state": _bounded_text(value.get("status", value.get("state", "unknown")), 64),
                    "tool_count": tool_count,
                    "required": bool(value.get("required", False)),
                    "tool_names": tool_names,
                }
            )
    for server in sorted(set(tools_by_server).difference(str(name) for name in raw)):
        result.append(
            {
                "server": _bounded_text(server, 128),
                "state": "ready",
                "tool_count": len(tools_by_server[server]),
                "required": False,
                "tool_names": sorted(set(tools_by_server[server]))[:_MAX_LIST_ITEMS],
            }
        )
    return tuple(result[:_MAX_LIST_ITEMS])


def build_authoring_snapshot(
    config: WorkflowConfig,
    *,
    phase_name: str = "design",
    phase_role: str = "auto",
    phase_capabilities: Iterable[object] | None = None,
    allowed_tool_names: frozenset[str] | None = None,
    tools: Iterable[ToolLike] | None = None,
    source_by_name: Mapping[str, str] | None = None,
) -> AuthoringSnapshot:
    """Build one bounded effective snapshot without probing external services."""
    from agenthicc.agents.plugin import ROLE_DEFAULT_ALLOWED  # noqa: PLC0415

    mode = config.app_state.active_mode()
    role = _bounded_text(phase_role or "auto", 64)
    if phase_capabilities is not None:
        resolved_phase = tuple(_capability_values(phase_capabilities))
        phase_source = "explicit" if resolved_phase else "explicit_unrestricted"
    else:
        role_default = ROLE_DEFAULT_ALLOWED.get(role)
        resolved_phase = _capability_values(role_default)
        phase_source = "role_default_unrestricted" if role_default is None else "role_default"
    visible = list(tools) if tools is not None else list(config.all_plugin_tools())
    # AgentTurnRunner always merges the built-in registry after project tools.
    # Include the same live callables here so the authoring catalog describes
    # the effective turn rather than only the project-plugin subset.  Project
    # tools remain first, matching the registry's last-writer-wins precedence.
    from agenthicc.agent_tools import AGENT_TOOLS  # noqa: PLC0415

    visible.extend(cast(Iterable[ToolLike], AGENT_TOOLS))
    unavailable_by_name: dict[str, str] = {}
    # Browser action tools are deliberately not callable by create_workflow,
    # but their live schemas and backend provenance are useful to an authoring
    # agent. Include redacted metadata and mark each action as excluded rather
    # than implying it is available in this phase.
    browser_tools = list(getattr(config, "browser_tools", []))
    visible_names = {tool_name(tool) for tool in visible}
    for browser_tool in browser_tools:
        name = tool_name(browser_tool)
        if name not in visible_names:
            visible.append(browser_tool)
            unavailable_by_name[name] = "authoring_excluded"
    mcp = getattr(config, "mcp_registry", None)
    if mcp is not None:
        try:
            visible.extend(mcp.all_tools())
        except Exception:
            pass
    catalog = build_tool_catalog(
        visible,
        active_mode=mode,
        phase_capabilities=resolved_phase,
        allowed_tool_names=allowed_tool_names,
        source_by_name=source_by_name,
        unavailable_by_name=unavailable_by_name,
    )
    browser = _safe_browser_summary(config)
    unavailable: list[Mapping[str, object]] = [
        {
            "name": entry.name,
            "reason": entry.availability_reason,
            "optional_dependency": entry.optional_dependency,
        }
        for entry in catalog
        if not entry.available
        and entry.optional_dependency
        and entry.availability_reason != "authoring_excluded"
    ]
    dependency_status = browser.get("dependency_status")
    if dependency_status in {
        "dependency_missing",
        "binary_missing",
        "browser_unavailable",
        "unavailable",
    }:
        unavailable.append(
            {
                "name": str(browser.get("selected_backend", "browser")),
                "reason": str(dependency_status),
                "optional_dependency": str(browser.get("optional_dependency", "browser")),
            }
        )
    scope = getattr(config, "workspace_scope", None)
    workspace_root = getattr(scope, "primary_root", None)
    workspace = {
        "policy": _bounded_text(
            getattr(getattr(config, "workspace_access", None), "mode", "unknown"), 64
        ),
        "root": _bounded_text(
            str(workspace_root) if workspace_root is not None else "(session default)", 512
        ),
        "outside_workspace": "controlled by the session workspace policy",
    }
    cache = {
        "contract_version": "agenthicc.prompt-cache.v1",
        "stable_region": "system_prompt_and_stable_tools",
        "dynamic_region": "phase_state_artifacts_questions_transitions",
    }
    checkpoint = {
        "schema_version": "agenthicc.workflow-checkpoint.v1",
        "memory": "session-owned and reattached on restore",
        "runtime_objects": "excluded",
    }
    return AuthoringSnapshot(
        active_mode=_bounded_text(getattr(mode, "name", mode), 64),
        blocked_capabilities=_capability_values(getattr(mode, "blocked_capabilities", ())),
        phase_name=phase_name,
        phase_role=role,
        phase_capability_source=phase_source,
        phase_capabilities=resolved_phase,
        tools=catalog,
        browser=browser,
        mcp=_safe_mcp_summary(config),
        workspace=workspace,
        cache=cache,
        checkpoint=checkpoint,
        unavailable=tuple(unavailable),
    )


def _snapshot_cache_key(
    config: WorkflowConfig,
    *,
    phase_name: str,
    phase_role: str,
    phase_capabilities: Iterable[object] | None,
    allowed_tool_names: frozenset[str] | None,
    tools: Iterable[ToolLike] | None,
) -> str:
    """Build a redacted key for one effective authoring snapshot.

    This function reads only local metadata.  In particular, it does not call
    browser health checks or MCP discovery; catalog revisions and safe status
    fields are enough to invalidate a session cache when those integrations
    change.
    """
    mode = config.app_state.active_mode()
    browser = getattr(config, "browser_manager", None)
    settings = getattr(browser, "settings", None)
    mcp = getattr(config, "mcp_registry", None)
    mcp_status: list[dict[str, object]] = []
    status_fn = getattr(mcp, "status", None)
    if callable(status_fn):
        try:
            raw_status = status_fn()
        except Exception:
            raw_status = {}
        if isinstance(raw_status, Mapping):
            for name, value in sorted(raw_status.items(), key=lambda item: str(item[0])):
                if not isinstance(value, Mapping):
                    continue
                mcp_status.append(
                    {
                        "server": _bounded_text(name, 128),
                        "state": _bounded_text(
                            value.get("status", value.get("state", "unknown")), 64
                        ),
                        "tool_count": value.get("tool_count", 0),
                    }
                )
    tool_list = list(tools) if tools is not None else None
    if tool_list is None:
        # The runner normally supplies its already-filtered phase tools.  The
        # fallback keeps direct cache users correct as plugin/MCP/browser
        # registries change, without probing any external service.
        tool_list = list(config.all_plugin_tools())
        try:
            from agenthicc.agent_tools import AGENT_TOOLS  # noqa: PLC0415

            tool_list.extend(cast(Iterable[ToolLike], AGENT_TOOLS))
        except (ImportError, AttributeError):
            pass
        browser_tools = getattr(config, "browser_tools", ())
        tool_list.extend(browser_tools)
        all_tools_fn = getattr(mcp, "all_tools", None)
        if callable(all_tools_fn):
            try:
                tool_list.extend(all_tools_fn())
            except Exception:
                pass
    browser_tools = getattr(config, "browser_tools", ())
    effective_tools = [*tool_list, *browser_tools]
    payload: dict[str, object] = {
        "catalog_version": AUTHORING_CATALOG_VERSION,
        "phase_name": _bounded_text(phase_name, 64),
        "phase_role": _bounded_text(phase_role, 64),
        "phase_capabilities": list(_capability_values(phase_capabilities)),
        "allowed_tool_names": sorted(allowed_tool_names)
        if allowed_tool_names is not None
        else None,
        "tools": (sorted((tool_name(tool), tool_fingerprint(tool)) for tool in effective_tools)),
        "mode": {
            "name": _bounded_text(getattr(mode, "name", mode), 64),
            "blocked": list(_capability_values(getattr(mode, "blocked_capabilities", ()))),
        },
        "browser": {
            "backend": _bounded_text(getattr(browser, "backend_name", ""), 64),
            "enabled": bool(getattr(browser, "enabled", False)),
            "client": type(getattr(browser, "client", None)).__name__
            if browser is not None
            else "",
            "allow_all_domains": bool(getattr(settings, "allow_all_domains", False)),
        },
        "mcp_revision": getattr(mcp, "catalog_revision", 0),
        "mcp_status": mcp_status,
        "workspace_root": _bounded_text(
            str(getattr(getattr(config, "workspace_scope", None), "primary_root", "")), 512
        ),
        "workspace_mode": _bounded_text(
            getattr(getattr(config, "workspace_access", None), "mode", "unknown"), 64
        ),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


@dataclass(slots=True)
class AuthoringSnapshotCache:
    """Session-local cache for immutable effective authoring snapshots.

    Entries are keyed by live tool fingerprints, mode policy, phase
    capabilities, workspace summary, browser selection, and MCP catalog/status
    metadata.  No provider or external health probe is performed.  The small
    bounded cache is owned by a workflow runner and can be discarded on
    session/tool reload.
    """

    max_entries: int = 16
    _entries: dict[str, AuthoringSnapshot] = field(default_factory=dict, init=False, repr=False)

    def get_or_build(
        self,
        config: WorkflowConfig,
        *,
        phase_name: str = "design",
        phase_role: str = "auto",
        phase_capabilities: Iterable[object] | None = None,
        allowed_tool_names: frozenset[str] | None = None,
        tools: Iterable[ToolLike] | None = None,
    ) -> AuthoringSnapshot:
        """Return a cached snapshot or build one from current live metadata."""
        tool_list = list(tools) if tools is not None else None
        resolved_phase_capabilities = (
            tuple(phase_capabilities) if phase_capabilities is not None else None
        )
        key = _snapshot_cache_key(
            config,
            phase_name=phase_name,
            phase_role=phase_role,
            phase_capabilities=resolved_phase_capabilities,
            allowed_tool_names=allowed_tool_names,
            tools=tool_list,
        )
        cached = self._entries.get(key)
        if cached is not None:
            return cached
        snapshot = build_authoring_snapshot(
            config,
            phase_name=phase_name,
            phase_role=phase_role,
            phase_capabilities=resolved_phase_capabilities,
            allowed_tool_names=allowed_tool_names,
            tools=tool_list,
        )
        self._entries[key] = snapshot
        while len(self._entries) > max(1, self.max_entries):
            self._entries.pop(next(iter(self._entries)))
        return snapshot

    def clear(self) -> None:
        """Discard cached snapshots after a registry/session reload."""
        self._entries.clear()
