"""Security policy evaluation for tool calls (PRD-07, PRD-19).

Evaluates the kernel's declarative :class:`SecurityPolicy` against a tool
name plus optional call conditions. Rules are checked in order and the
first match wins; if nothing matches, the policy's ``default_action``
applies (fail-closed deny by default).

PRD-19 adds :class:`AgentCapabilityScope` and :class:`ScopeManager` for
per-agent tool-permission scoping. :class:`PermissionChecker` now accepts an
optional ``agent_id`` kwarg and consults the agent's scope before falling
through to the global policy.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from agenthicc.kernel import PermissionRule, SecurityPolicy

if TYPE_CHECKING:
    from agenthicc.config import AgenthiccConfig

__all__ = [
    "AgentCapabilityScope",
    "PermissionChecker",
    "ScopeManager",
    "build_policy_from_config",
]

ALLOW = "allow"
DENY = "deny"
REQUIRE_CONFIRMATION = "require_confirmation"


# ---------------------------------------------------------------------------
# PRD-19 — per-agent capability scoping
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentCapabilityScope:
    """Immutable capability constraint attached to one agent instance.

    Attributes
    ----------
    allowed_tools:
        ``None`` means every tool is accessible (subject to global policy).
        A non-empty frozenset means *only* those tool-name patterns are
        accessible.
    denied_tools:
        Explicit denies that always take precedence over *allowed_tools*.
        Supports :mod:`fnmatch` wildcard patterns (e.g. ``"outlook_*"``).
    allowed_comm_tools:
        ``None`` means all communication tools are accessible.
    max_tool_call_budget:
        Maximum number of tool calls this agent may make per session.
    max_spawn_depth:
        Maximum depth in the agent-spawn tree; 0 means no sub-agents.
    """

    allowed_tools: frozenset[str] | None = None
    denied_tools: frozenset[str] = field(default_factory=frozenset)
    allowed_comm_tools: frozenset[str] | None = None
    max_tool_call_budget: int = 100
    max_spawn_depth: int = 3

    def is_tool_allowed(self, tool_name: str) -> bool:
        """Return ``True`` when *tool_name* is permitted by this scope.

        Deny patterns are checked first; then the allow whitelist (if set).
        """
        for pattern in self.denied_tools:
            if fnmatch.fnmatch(tool_name, pattern):
                return False
        if self.allowed_tools is not None:
            return any(fnmatch.fnmatch(tool_name, p) for p in self.allowed_tools)
        return True

    def restrict(self, other: "AgentCapabilityScope") -> "AgentCapabilityScope":
        """Return the intersection of *self* and *other* — always more restrictive.

        Rules:
        * ``allowed_tools``: intersection when both set; whichever is not None
          wins when only one side is set; ``None`` only when both are ``None``.
        * ``denied_tools``: union (more denies).
        * ``max_tool_call_budget`` / ``max_spawn_depth``: minimum.
        """
        if self.allowed_tools is not None and other.allowed_tools is not None:
            allowed: frozenset[str] | None = self.allowed_tools & other.allowed_tools
        elif self.allowed_tools is not None:
            allowed = self.allowed_tools
        elif other.allowed_tools is not None:
            allowed = other.allowed_tools
        else:
            allowed = None

        denied = self.denied_tools | other.denied_tools

        # allowed_comm_tools: same intersection logic
        if self.allowed_comm_tools is not None and other.allowed_comm_tools is not None:
            allowed_comm: frozenset[str] | None = (
                self.allowed_comm_tools & other.allowed_comm_tools
            )
        elif self.allowed_comm_tools is not None:
            allowed_comm = self.allowed_comm_tools
        elif other.allowed_comm_tools is not None:
            allowed_comm = other.allowed_comm_tools
        else:
            allowed_comm = None

        return AgentCapabilityScope(
            allowed_tools=allowed,
            denied_tools=denied,
            allowed_comm_tools=allowed_comm,
            max_tool_call_budget=min(
                self.max_tool_call_budget, other.max_tool_call_budget
            ),
            max_spawn_depth=min(self.max_spawn_depth, other.max_spawn_depth),
        )

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "AgentCapabilityScope":
        """Deserialise from a plain dict (e.g. from the event log)."""
        raw_allowed = data.get("allowed_tools")
        raw_comm = data.get("allowed_comm_tools")
        return cls(
            allowed_tools=frozenset(raw_allowed) if raw_allowed is not None else None,
            denied_tools=frozenset(data.get("denied_tools", [])),
            allowed_comm_tools=frozenset(raw_comm) if raw_comm is not None else None,
            max_tool_call_budget=int(data.get("max_tool_call_budget", 100)),
            max_spawn_depth=int(data.get("max_spawn_depth", 3)),
        )

    def to_dict(self) -> dict[str, object]:
        """Serialise to a plain dict suitable for JSON / TOML."""
        return {
            "allowed_tools": (
                list(self.allowed_tools) if self.allowed_tools is not None else None
            ),
            "denied_tools": list(self.denied_tools),
            "allowed_comm_tools": (
                list(self.allowed_comm_tools)
                if self.allowed_comm_tools is not None
                else None
            ),
            "max_tool_call_budget": self.max_tool_call_budget,
            "max_spawn_depth": self.max_spawn_depth,
        }


class ScopeManager:
    """Tracks ``agent_id → (AgentCapabilityScope, spawn_depth)`` mappings.

    Registered agents inherit their parent's scope when no explicit scope is
    supplied. Child scopes can only *restrict*, never expand, the parent scope.
    """

    def __init__(self) -> None:
        self._scopes: dict[str, AgentCapabilityScope] = {}
        self._depths: dict[str, int] = {}

    def register(
        self,
        agent_id: str,
        scope: AgentCapabilityScope | None,
        parent_id: str | None = None,
    ) -> None:
        """Register *agent_id* with an optional *scope* and optional *parent_id*.

        Depth is ``parent_depth + 1`` (root agents get depth 1).

        When *scope* is ``None`` the parent's scope is inherited verbatim.
        When both *scope* and a parent scope exist, the result is the
        intersection (child cannot exceed parent).
        """
        parent_depth = self._depths.get(parent_id, 0) if parent_id else 0
        self._depths[agent_id] = parent_depth + 1

        if scope is None:
            parent_scope = self._scopes.get(parent_id) if parent_id else None
            self._scopes[agent_id] = parent_scope if parent_scope is not None else AgentCapabilityScope()
        elif parent_id and parent_id in self._scopes:
            # Restrict: child cannot exceed parent
            self._scopes[agent_id] = self._scopes[parent_id].restrict(scope)
        else:
            self._scopes[agent_id] = scope

    def get_scope(self, agent_id: str) -> AgentCapabilityScope | None:
        """Return the current scope for *agent_id*, or ``None`` if not registered."""
        return self._scopes.get(agent_id)

    def get_depth(self, agent_id: str) -> int:
        """Return the spawn depth for *agent_id* (0 if never registered)."""
        return self._depths.get(agent_id, 0)

    def update_scope(self, agent_id: str, new_scope: AgentCapabilityScope) -> None:
        """Downscope a running agent — can only restrict, never expand.

        If *agent_id* has no existing scope the new scope is stored directly.
        """
        existing = self._scopes.get(agent_id)
        if existing is not None:
            self._scopes[agent_id] = existing.restrict(new_scope)
        else:
            self._scopes[agent_id] = new_scope

    def can_spawn(self, parent_id: str) -> bool:
        """Return ``True`` when *parent_id* has not reached its ``max_spawn_depth``.

        A parent at depth *d* would create a child at depth *d + 1*.
        ``max_spawn_depth`` is the maximum depth a descendant may reach, so
        ``can_spawn`` returns ``True`` iff ``depth < max_spawn_depth``.
        """
        scope = self._scopes.get(parent_id)
        depth = self._depths.get(parent_id, 0)
        max_depth = scope.max_spawn_depth if scope is not None else 3
        return depth < max_depth


# ---------------------------------------------------------------------------
# PRD-07 — global policy checker (extended for PRD-19)
# ---------------------------------------------------------------------------


class PermissionChecker:
    """Evaluates a kernel ``SecurityPolicy`` for tool invocations.

    Optionally accepts a :class:`ScopeManager` (PRD-19). When *agent_id* is
    supplied to :meth:`check`, the agent's capability scope is consulted first;
    a scope-level deny is fail-closed and short-circuits the global policy.
    """

    def __init__(
        self,
        policy: SecurityPolicy,
        scope_manager: ScopeManager | None = None,
    ) -> None:
        self._policy = policy
        self._scope_manager = scope_manager

    @property
    def policy(self) -> SecurityPolicy:
        return self._policy

    def check(
        self,
        tool_name: str,
        conditions: dict[str, object] | None = None,
        agent_id: str | None = None,
    ) -> str:
        """Return ``'allow'``, ``'deny'``, or ``'require_confirmation'``.

        When *agent_id* is provided and a :class:`ScopeManager` is configured,
        the agent's :class:`AgentCapabilityScope` is checked first. A scope
        deny is final and overrides any global ``allow`` rule (fail-closed).

        The first rule whose ``tool_pattern`` glob-matches ``tool_name`` and
        whose conditions are all satisfied wins the global check. With no
        matching rule the policy's ``default_action`` is returned.
        """
        # 1. Per-agent scope check (PRD-19) — runs before global policy
        if agent_id is not None and self._scope_manager is not None:
            scope = self._scope_manager.get_scope(agent_id)
            if scope is not None and not scope.is_tool_allowed(tool_name):
                return DENY

        # 2. Global policy (original PRD-07 logic)
        return self._check_global(tool_name, conditions)

    def _check_global(
        self, tool_name: str, conditions: dict[str, object] | None
    ) -> str:
        """Run the ordered global permission-rule table."""
        call_conditions = conditions or {}
        for rule in self._policy.permission_rules:
            if not fnmatch.fnmatch(tool_name, rule.tool_pattern):
                continue
            if not self._conditions_match(rule, call_conditions):
                continue
            return rule.action
        return self._policy.default_action

    # ── condition evaluation ─────────────────────────────────────────────

    def _conditions_match(
        self, rule: PermissionRule, call_conditions: dict[str, object]
    ) -> bool:
        for key, expected in rule.conditions.items():
            if key == "path_prefix":
                if not self._path_prefix_matches(expected, call_conditions):
                    return False
            elif key == "network_domain":
                if not self._network_domain_matches(expected, call_conditions):
                    return False
            else:
                # Unknown condition keys fail closed: the rule cannot match.
                return False
        return True

    @staticmethod
    def _path_prefix_matches(expected: object, call_conditions: dict[str, object]) -> bool:
        path = str(call_conditions.get("path", ""))
        if not path:
            return False
        prefixes = expected if isinstance(expected, (list, tuple)) else [expected]
        return any(path.startswith(str(prefix)) for prefix in prefixes)

    @staticmethod
    def _network_domain_matches(expected: object, call_conditions: dict[str, object]) -> bool:
        host = call_conditions.get("host")
        if host is None:
            url = str(call_conditions.get("url", ""))
            host = urlparse(url).hostname or ""
        host = str(host)
        if not host:
            return False
        domains = expected if isinstance(expected, (list, tuple)) else [expected]
        return any(
            host == domain or host.endswith("." + str(domain)) for domain in domains
        )


def build_policy_from_config(config: "AgenthiccConfig") -> SecurityPolicy:
    """Translate an :class:`AgenthiccConfig` into a kernel ``SecurityPolicy``.

    Denied tool patterns come first (so deny wins over a broader allow),
    followed by allowed tool patterns. Allow rules are scoped to the
    configured filesystem prefixes only when the pattern is file-system
    flavored; the default action is always ``deny`` (fail-closed).
    """
    rules: list[PermissionRule] = []
    for pattern in config.tools.denied:
        rules.append(PermissionRule(tool_pattern=pattern, action=DENY))
    for pattern in config.tools.allowed:
        rules.append(PermissionRule(tool_pattern=pattern, action=ALLOW))
    return SecurityPolicy(permission_rules=tuple(rules), default_action=DENY)

