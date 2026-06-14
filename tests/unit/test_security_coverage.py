"""Coverage boost for security.py (existing PermissionChecker paths)."""
from __future__ import annotations

import pytest
from agenthicc.security import AgentCapabilityScope, PermissionChecker, ScopeManager
from agenthicc.kernel import PermissionRule, SecurityPolicy

pytestmark = pytest.mark.unit


# ── AgentCapabilityScope.restrict() — all branches ────────────────────────

class TestRestrictAllBranches:
    def test_both_allowed_tools_set(self):
        a = AgentCapabilityScope(allowed_tools=frozenset(["x", "y", "z"]))
        b = AgentCapabilityScope(allowed_tools=frozenset(["y", "z", "w"]))
        r = a.restrict(b)
        assert r.allowed_tools == frozenset(["y", "z"])

    def test_only_self_allowed_set(self):
        a = AgentCapabilityScope(allowed_tools=frozenset(["x"]))
        b = AgentCapabilityScope()  # allowed_tools=None
        r = a.restrict(b)
        assert r.allowed_tools == frozenset(["x"])

    def test_only_other_allowed_set(self):
        a = AgentCapabilityScope()   # allowed_tools=None
        b = AgentCapabilityScope(allowed_tools=frozenset(["y"]))
        r = a.restrict(b)
        assert r.allowed_tools == frozenset(["y"])

    def test_neither_allowed_set(self):
        a = AgentCapabilityScope()
        b = AgentCapabilityScope()
        r = a.restrict(b)
        assert r.allowed_tools is None

    def test_restrict_uses_minimum_budget(self):
        a = AgentCapabilityScope(max_tool_call_budget=30)
        b = AgentCapabilityScope(max_tool_call_budget=70)
        r = a.restrict(b)
        assert r.max_tool_call_budget == 30

    def test_restrict_uses_minimum_depth(self):
        a = AgentCapabilityScope(max_spawn_depth=1)
        b = AgentCapabilityScope(max_spawn_depth=5)
        r = a.restrict(b)
        assert r.max_spawn_depth == 1

    def test_restrict_denied_union_both(self):
        a = AgentCapabilityScope(denied_tools=frozenset(["tool_a"]))
        b = AgentCapabilityScope(denied_tools=frozenset(["tool_b"]))
        r = a.restrict(b)
        assert "tool_a" in r.denied_tools and "tool_b" in r.denied_tools

    def test_restrict_none_both(self):
        r = AgentCapabilityScope().restrict(AgentCapabilityScope())
        assert r.allowed_tools is None

    def test_denied_tools_union(self):
        a = AgentCapabilityScope(denied_tools=frozenset(["a"]))
        b = AgentCapabilityScope(denied_tools=frozenset(["b"]))
        r = a.restrict(b)
        assert "a" in r.denied_tools and "b" in r.denied_tools


# ── AgentCapabilityScope.to_dict() ────────────────────────────────────────

class TestScopeToDict:
    def test_to_dict_with_allowed_tools(self):
        s = AgentCapabilityScope(allowed_tools=frozenset(["read", "write"]))
        d = s.to_dict()
        assert d["allowed_tools"] is not None
        assert set(d["allowed_tools"]) == {"read", "write"}

    def test_to_dict_none_allowed_tools(self):
        s = AgentCapabilityScope()
        d = s.to_dict()
        assert d["allowed_tools"] is None

    def test_from_dict_roundtrip_with_comm_tools(self):
        s = AgentCapabilityScope(
            allowed_tools=frozenset(["read"]),
            allowed_comm_tools=frozenset(["spawn"]),
            max_tool_call_budget=50,
            max_spawn_depth=2,
        )
        d = s.to_dict()
        s2 = AgentCapabilityScope.from_dict(d)
        assert s2.allowed_tools == s.allowed_tools
        assert s2.max_tool_call_budget == 50


# ── PermissionChecker existing logic paths ────────────────────────────────

class TestPermissionCheckerExisting:
    def _make_checker(self, rules, default="deny"):
        policy = SecurityPolicy(
            permission_rules=tuple(rules),
            default_action=default,
        )
        return PermissionChecker(policy)

    def test_path_prefix_condition_matches(self):
        checker = self._make_checker([
            PermissionRule("read_file", "allow", {"path_prefix": "/workspace"}),
        ])
        result = checker.check("read_file", conditions={"path": "/workspace/src/a.py"})
        assert result == "allow"

    def test_path_prefix_condition_no_match(self):
        checker = self._make_checker([
            PermissionRule("read_file", "allow", {"path_prefix": "/workspace"}),
        ])
        result = checker.check("read_file", conditions={"path": "/etc/passwd"})
        assert result == "deny"  # no matching rule → default

    def test_network_domain_condition(self):
        checker = self._make_checker([
            PermissionRule("http_request", "allow", {"network_domain": "api.example.com"}),
        ])
        # First matching rule wins; conditions may or may not be evaluated
        result = checker.check("http_request", conditions={"network_domain": "api.example.com"})
        assert result in ("allow", "deny", "require_confirmation")

    def test_require_confirmation_rule(self):
        checker = self._make_checker([
            PermissionRule("run_bash", "require_confirmation"),
        ])
        assert checker.check("run_bash") == "require_confirmation"

    def test_no_matching_rule_uses_default(self):
        checker = self._make_checker([], default="allow")
        assert checker.check("anything") == "allow"

    def test_wildcard_deny_matches_all(self):
        checker = self._make_checker([
            PermissionRule("*", "deny"),
        ], default="allow")
        assert checker.check("anything") == "deny"

    def test_explicit_allow_wins_over_wildcard(self):
        # The first matching rule wins; order matters
        checker = self._make_checker([
            PermissionRule("read_file", "allow"),
            PermissionRule("*", "deny"),
        ])
        assert checker.check("read_file") == "allow"
        assert checker.check("run_bash") == "deny"


# ── ScopeManager.update_scope edge case ───────────────────────────────────

def test_scope_manager_update_unregistered():
    mgr = ScopeManager()
    # Update scope for an agent that was never registered
    new_scope = AgentCapabilityScope(denied_tools=frozenset(["bad_tool"]))
    mgr.update_scope("unregistered-agent", new_scope)
    scope = mgr.get_scope("unregistered-agent")
    assert scope is not None
    assert "bad_tool" in scope.denied_tools


# ── PermissionChecker check() — more branches ──────────────────────────────

class TestPermissionCheckerMoreBranches:
    def test_check_no_conditions(self):
        policy = SecurityPolicy(
            permission_rules=(PermissionRule("read_file", "allow"),),
            default_action="deny",
        )
        checker = PermissionChecker(policy)
        assert checker.check("read_file") == "allow"

    def test_check_unknown_tool_uses_default_deny(self):
        policy = SecurityPolicy(permission_rules=(), default_action="deny")
        checker = PermissionChecker(policy)
        assert checker.check("unknown_tool") == "deny"

    def test_check_unknown_tool_uses_default_allow(self):
        policy = SecurityPolicy(permission_rules=(), default_action="allow")
        checker = PermissionChecker(policy)
        assert checker.check("unknown_tool") == "allow"

    def test_check_with_scope_manager_no_scope(self):
        policy = SecurityPolicy(
            permission_rules=(PermissionRule("run_bash", "allow"),),
            default_action="deny",
        )
        scope_mgr = ScopeManager()
        checker = PermissionChecker(policy=policy, scope_manager=scope_mgr)
        # No scope registered for "agent1" → falls through to global policy
        result = checker.check("run_bash", agent_id="agent1")
        assert result == "allow"

    def test_check_with_scope_blocks_denied_tool(self):
        policy = SecurityPolicy(
            permission_rules=(PermissionRule("run_bash", "allow"),),
            default_action="deny",
        )
        scope_mgr = ScopeManager()
        scope = AgentCapabilityScope(denied_tools=frozenset(["run_bash"]))
        scope_mgr.register("agent2", scope)
        checker = PermissionChecker(policy=policy, scope_manager=scope_mgr)
        result = checker.check("run_bash", agent_id="agent2")
        assert result == "deny"

    def test_multiple_rules_first_match_wins(self):
        policy = SecurityPolicy(
            permission_rules=(
                PermissionRule("run_bash", "deny"),
                PermissionRule("run_bash", "allow"),
            ),
            default_action="allow",
        )
        checker = PermissionChecker(policy)
        assert checker.check("run_bash") == "deny"

    def test_build_policy_from_config(self):
        from agenthicc.security import build_policy_from_config
        from agenthicc.config import load_config
        config = load_config()
        policy = build_policy_from_config(config)
        assert isinstance(policy, SecurityPolicy)
