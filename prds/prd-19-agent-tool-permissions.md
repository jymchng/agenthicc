---
title: "PRD-19: Per-Agent Tool Permissions and Capability Scoping"
status: draft
version: 0.1.0
created: 2025-01-01
---

# PRD-19: Per-Agent Tool Permissions and Capability Scoping

## Executive Summary

All agents currently share the same global `SecurityPolicy`. When an orchestrator spawns sub-agents, it should be able to restrict what tools each sub-agent can call — a "docs" agent should only see file tools, not `run_bash` or `git_commit`. This PRD specifies `AgentCapabilityScope`, an immutable constraint set attached to `AgentInstance` that is checked by `PermissionChecker` before every tool invocation. Scopes are always **restrictive** — a child agent can never gain permissions its parent doesn't have. A `spawn_depth` limit prevents infinite spawning. A new `scope_restrict` communication tool lets the orchestrator downscope a running agent at any time.

---

## Goals

| ID | Goal |
|----|------|
| G1 | `AgentCapabilityScope` attached to `AgentInstance`; `None` = inherit parent |
| G2 | `PermissionChecker.check(tool, agent_id=...)` consults agent scope before global policy |
| G3 | Child scope can only restrict, never expand beyond parent scope |
| G4 | `spawn_depth` tracked per `AgentInstance`; max enforced at spawn time |
| G5 | `scope_restrict` comm tool lets orchestrators downscope running agents |
| G6 | Named scope profiles in TOML `[security.agent_scopes.*]` |

## Non-Goals
- Per-tool argument-level restrictions (e.g. `run_bash` only for `ls` commands) — pattern matching at the tool level is sufficient
- Cross-session scope persistence

---

## Architecture

```
agent_spawn(agent_type, capability_scope={...})
    │
    ▼
AgentSpawnRequest event payload includes capability_scope dict
    │
    ▼
reducer creates AgentInstance with scope=AgentCapabilityScope(...)
    │
    ▼
ScopeManager.register(agent_id, scope, parent_id)
    │
    ▼
On each tool call:
PermissionChecker.check(tool_name, agent_id=agent_id)
    ├── look up agent scope from ScopeManager
    ├── if tool in denied_tools → deny
    ├── if allowed_tools is set and tool NOT in allowed_tools → deny
    └── else → consult global SecurityPolicy
```

---

## Data Structures and Interfaces

```python
# src/agenthicc/security.py  — add AgentCapabilityScope and ScopeManager

from __future__ import annotations
import fnmatch
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class AgentCapabilityScope:
    """Immutable capability constraint for one agent."""

    allowed_tools: frozenset[str] | None = None
    # None = all tools allowed (subject to global policy)
    # frozenset = ONLY these tools are accessible

    denied_tools: frozenset[str] = field(default_factory=frozenset)
    # Explicit denies; take precedence over allowed_tools

    allowed_comm_tools: frozenset[str] | None = None
    # None = all comm tools allowed

    max_tool_call_budget: int = 100
    max_spawn_depth: int = 3

    def is_tool_allowed(self, tool_name: str) -> bool:
        """Return True if tool_name is in scope (ignores global policy)."""
        for pattern in self.denied_tools:
            if fnmatch.fnmatch(tool_name, pattern):
                return False
        if self.allowed_tools is not None:
            return any(fnmatch.fnmatch(tool_name, p) for p in self.allowed_tools)
        return True  # allowed_tools=None means all tools allowed

    def restrict(self, other: AgentCapabilityScope) -> AgentCapabilityScope:
        """Return the intersection: child can never exceed self."""
        # allowed_tools: intersection (more restrictive)
        if self.allowed_tools is not None and other.allowed_tools is not None:
            allowed = self.allowed_tools & other.allowed_tools
        elif self.allowed_tools is not None:
            allowed = self.allowed_tools
        elif other.allowed_tools is not None:
            allowed = other.allowed_tools
        else:
            allowed = None

        # denied_tools: union
        denied = self.denied_tools | other.denied_tools

        # budget and depth: take the smaller
        return AgentCapabilityScope(
            allowed_tools=allowed,
            denied_tools=denied,
            max_tool_call_budget=min(self.max_tool_call_budget, other.max_tool_call_budget),
            max_spawn_depth=min(self.max_spawn_depth, other.max_spawn_depth),
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentCapabilityScope:
        return cls(
            allowed_tools=frozenset(data["allowed_tools"]) if data.get("allowed_tools") is not None else None,
            denied_tools=frozenset(data.get("denied_tools", [])),
            max_tool_call_budget=int(data.get("max_tool_call_budget", 100)),
            max_spawn_depth=int(data.get("max_spawn_depth", 3)),
        )


class ScopeManager:
    """Tracks agent_id → (scope, spawn_depth) mappings."""

    def __init__(self) -> None:
        self._scopes: dict[str, AgentCapabilityScope] = {}
        self._depths: dict[str, int] = {}

    def register(
        self,
        agent_id: str,
        scope: AgentCapabilityScope | None,
        parent_id: str | None,
    ) -> None:
        parent_depth = self._depths.get(parent_id, 0) if parent_id else 0
        self._depths[agent_id] = parent_depth + 1

        if scope is None:
            # Inherit parent scope
            parent_scope = self._scopes.get(parent_id) if parent_id else None
            self._scopes[agent_id] = parent_scope or AgentCapabilityScope()
        elif parent_id and parent_id in self._scopes:
            # Restrict: child cannot exceed parent
            self._scopes[agent_id] = self._scopes[parent_id].restrict(scope)
        else:
            self._scopes[agent_id] = scope

    def get_scope(self, agent_id: str) -> AgentCapabilityScope | None:
        return self._scopes.get(agent_id)

    def get_depth(self, agent_id: str) -> int:
        return self._depths.get(agent_id, 0)

    def update_scope(self, agent_id: str, new_scope: AgentCapabilityScope) -> None:
        """Downscope a running agent (can only restrict)."""
        existing = self._scopes.get(agent_id)
        if existing is not None:
            self._scopes[agent_id] = existing.restrict(new_scope)
        else:
            self._scopes[agent_id] = new_scope

    def can_spawn(self, parent_id: str) -> bool:
        """Return True if parent_id has not reached max_spawn_depth."""
        scope = self._scopes.get(parent_id)
        depth = self._depths.get(parent_id, 0)
        max_depth = scope.max_spawn_depth if scope else 3
        return depth < max_depth
```

```python
# Update PermissionChecker.check() to accept agent_id:

class PermissionChecker:
    def __init__(
        self,
        policy: SecurityPolicy,
        scope_manager: ScopeManager | None = None,
    ) -> None:
        self._policy = policy
        self._scope_manager = scope_manager

    def check(
        self,
        tool_name: str,
        conditions: dict | None = None,
        agent_id: str | None = None,
    ) -> str:
        # 1. Check agent scope first (fail-closed)
        if agent_id and self._scope_manager:
            scope = self._scope_manager.get_scope(agent_id)
            if scope is not None and not scope.is_tool_allowed(tool_name):
                return "deny"

        # 2. Fall through to global policy (existing logic)
        return self._check_global(tool_name, conditions)

    def _check_global(self, tool_name: str, conditions: dict | None) -> str:
        # ... existing rule matching logic ...
        for rule in self._policy.permission_rules:
            if fnmatch.fnmatch(tool_name, rule.tool_pattern):
                return rule.action
        return self._policy.default_action
```

```python
# New comm tool — add to CommunicationTools:

async def scope_restrict(
    self,
    agent_id: str,
    allowed_tools: list[str] | None = None,
    denied_tools: list[str] | None = None,
    max_tool_call_budget: int | None = None,
) -> dict[str, Any]:
    """Downscope a running agent. Can only restrict, never expand.

    Args:
        agent_id: Target agent to restrict.
        allowed_tools: Restrict to only these tool name patterns.
        denied_tools: Explicitly deny these tool name patterns.
        max_tool_call_budget: New (lower) tool call budget.
    """
    from agenthicc.kernel import Event  # noqa: PLC0415
    new_scope = AgentCapabilityScope(
        allowed_tools=frozenset(allowed_tools) if allowed_tools is not None else None,
        denied_tools=frozenset(denied_tools or []),
        max_tool_call_budget=max_tool_call_budget or 100,
    )
    await self._emit("AgentScopeUpdated", {
        "agent_id": agent_id,
        "scope": {
            "allowed_tools": list(new_scope.allowed_tools) if new_scope.allowed_tools is not None else None,
            "denied_tools": list(new_scope.denied_tools),
            "max_tool_call_budget": new_scope.max_tool_call_budget,
        },
    })
    return {"ok": True, "agent_id": agent_id}
```

---

## Configuration Reference

```toml
[security.agent_scopes]
default_max_spawn_depth = 3
default_tool_budget = 100

[security.agent_scopes.orchestrator]
allowed_comm_tools = ["agent_spawn", "task_create", "workflow_modify", "memory_read", "memory_write", "application_log"]
max_spawn_depth = 5
max_tool_call_budget = 200

[security.agent_scopes.code_writer]
allowed_tools = ["read_file", "write_file", "patch_file", "git_add", "git_commit", "run_tests"]
denied_tools = ["run_bash", "outlook_*"]
max_spawn_depth = 1

[security.agent_scopes.reviewer]
allowed_tools = ["read_file", "git_diff", "git_log", "git_blame", "grep_files"]
max_tool_call_budget = 50
max_spawn_depth = 0   # reviewers cannot spawn sub-agents
```

---

## Tests

```python
# tests/unit/test_agent_permissions.py
"""Unit tests for per-agent scoping (PRD-19)."""
from __future__ import annotations
import pytest
from agenthicc.security import AgentCapabilityScope, ScopeManager, PermissionChecker
from agenthicc.kernel import PermissionRule, SecurityPolicy

pytestmark = pytest.mark.unit


class TestAgentCapabilityScope:
    def test_allowed_tools_none_permits_all(self):
        scope = AgentCapabilityScope()
        assert scope.is_tool_allowed("run_bash") is True
        assert scope.is_tool_allowed("anything") is True

    def test_allowed_tools_set_restricts(self):
        scope = AgentCapabilityScope(allowed_tools=frozenset(["read_file", "write_file"]))
        assert scope.is_tool_allowed("read_file") is True
        assert scope.is_tool_allowed("run_bash") is False

    def test_denied_tools_overrides_allowed(self):
        scope = AgentCapabilityScope(
            allowed_tools=frozenset(["read_file", "run_bash"]),
            denied_tools=frozenset(["run_bash"]),
        )
        assert scope.is_tool_allowed("run_bash") is False
        assert scope.is_tool_allowed("read_file") is True

    def test_wildcard_pattern_in_denied(self):
        scope = AgentCapabilityScope(denied_tools=frozenset(["outlook_*"]))
        assert scope.is_tool_allowed("outlook_send_email") is False
        assert scope.is_tool_allowed("read_file") is True

    def test_restrict_takes_intersection(self):
        parent = AgentCapabilityScope(allowed_tools=frozenset(["read_file", "write_file", "run_bash"]))
        child_request = AgentCapabilityScope(allowed_tools=frozenset(["read_file", "write_file"]))
        result = parent.restrict(child_request)
        assert result.allowed_tools == frozenset(["read_file", "write_file"])
        assert "run_bash" not in result.allowed_tools

    def test_restrict_child_cannot_expand(self):
        parent = AgentCapabilityScope(allowed_tools=frozenset(["read_file"]))
        child_request = AgentCapabilityScope(allowed_tools=None)  # child asks for all
        result = parent.restrict(child_request)
        # Parent's restriction still applies
        assert result.allowed_tools == frozenset(["read_file"])

    def test_restrict_budget_takes_minimum(self):
        parent = AgentCapabilityScope(max_tool_call_budget=50)
        child_request = AgentCapabilityScope(max_tool_call_budget=100)
        result = parent.restrict(child_request)
        assert result.max_tool_call_budget == 50

    def test_from_dict_roundtrip(self):
        data = {"allowed_tools": ["read_file", "write_file"],
                "denied_tools": ["run_bash"], "max_tool_call_budget": 30, "max_spawn_depth": 2}
        scope = AgentCapabilityScope.from_dict(data)
        assert scope.max_tool_call_budget == 30
        assert "run_bash" in scope.denied_tools


class TestScopeManager:
    def test_register_sets_depth(self):
        mgr = ScopeManager()
        mgr.register("root", None, parent_id=None)
        assert mgr.get_depth("root") == 1

    def test_child_depth_increments(self):
        mgr = ScopeManager()
        mgr.register("root", None, parent_id=None)
        mgr.register("child", None, parent_id="root")
        assert mgr.get_depth("child") == 2

    def test_can_spawn_at_root(self):
        mgr = ScopeManager()
        scope = AgentCapabilityScope(max_spawn_depth=3)
        mgr.register("root", scope, parent_id=None)
        assert mgr.can_spawn("root") is True

    def test_cannot_spawn_at_max_depth(self):
        mgr = ScopeManager()
        scope = AgentCapabilityScope(max_spawn_depth=1)
        mgr.register("root", scope, parent_id=None)
        mgr.register("child", None, parent_id="root")
        # child is at depth 2 which equals max_spawn_depth (spawn would create depth 3 > 1)
        assert mgr.can_spawn("child") is False

    def test_update_scope_further_restricts(self):
        mgr = ScopeManager()
        mgr.register("a1", AgentCapabilityScope(allowed_tools=frozenset(["r", "w", "x"])), None)
        mgr.update_scope("a1", AgentCapabilityScope(allowed_tools=frozenset(["r", "w"])))
        scope = mgr.get_scope("a1")
        assert scope.allowed_tools == frozenset(["r", "w"])

    def test_inherit_parent_scope(self):
        mgr = ScopeManager()
        parent_scope = AgentCapabilityScope(allowed_tools=frozenset(["read_file"]))
        mgr.register("parent", parent_scope, None)
        mgr.register("child", None, parent_id="parent")   # inherit
        child_scope = mgr.get_scope("child")
        assert child_scope.allowed_tools == frozenset(["read_file"])


class TestPermissionCheckerWithScope:
    def _policy(self) -> SecurityPolicy:
        return SecurityPolicy(
            permission_rules=(PermissionRule("run_bash", "allow"),),
            default_action="deny",
        )

    def test_scope_deny_overrides_policy_allow(self):
        mgr = ScopeManager()
        mgr.register("a1", AgentCapabilityScope(denied_tools=frozenset(["run_bash"])), None)
        checker = PermissionChecker(policy=self._policy(), scope_manager=mgr)
        assert checker.check("run_bash", agent_id="a1") == "deny"

    def test_no_scope_uses_policy(self):
        mgr = ScopeManager()
        checker = PermissionChecker(policy=self._policy(), scope_manager=mgr)
        # "a2" has no registered scope → global policy applies
        assert checker.check("run_bash", agent_id="a2") == "allow"

    def test_unknown_agent_id_falls_to_policy(self):
        checker = PermissionChecker(policy=self._policy(), scope_manager=None)
        assert checker.check("run_bash") == "allow"
```

---

## Open Questions

1. **`AgentScopeUpdated` reducer**: the reducer for this new event updates `AgentInstance.scope` via `ScopeManager.update_scope()`. Since `ScopeManager` is not in `AppState`, it must be injected as a side-effect object. The `EffectExecutor` calls `scope_manager.update_scope()` on the `AgentScopeUpdated` effect.
2. **Scope serialisation for replay**: `AgentCapabilityScope` needs to be serialisable to the event log. `from_dict`/`to_dict` is sufficient; add `to_dict()` companion to `from_dict`.
3. **UI visibility**: `/status` should show each agent's current scope (allowed/denied counts) in the Rich table so operators can audit restrictions at a glance.
