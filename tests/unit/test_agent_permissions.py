"""Unit tests for per-agent capability scoping (PRD-19)."""

from __future__ import annotations
import pytest
from agenthicc.security import AgentCapabilityScope, PermissionChecker, ScopeManager
from agenthicc.kernel import PermissionRule, SecurityPolicy

pytestmark = pytest.mark.unit


class TestAgentCapabilityScope:
    def test_none_allows_all(self):
        s = AgentCapabilityScope()
        assert s.is_tool_allowed("run_bash")
        assert s.is_tool_allowed("anything")

    def test_allowed_whitelist_restricts(self):
        s = AgentCapabilityScope(allowed_tools=frozenset(["read_file", "write_file"]))
        assert s.is_tool_allowed("read_file")
        assert not s.is_tool_allowed("run_bash")

    def test_denied_overrides_allowed(self):
        s = AgentCapabilityScope(
            allowed_tools=frozenset(["run_bash"]), denied_tools=frozenset(["run_bash"])
        )
        assert not s.is_tool_allowed("run_bash")

    def test_wildcard_in_denied(self):
        s = AgentCapabilityScope(denied_tools=frozenset(["outlook_*"]))
        assert not s.is_tool_allowed("outlook_send_email")
        assert s.is_tool_allowed("read_file")

    def test_restrict_intersection(self):
        parent = AgentCapabilityScope(allowed_tools=frozenset(["a", "b", "c"]))
        child = AgentCapabilityScope(allowed_tools=frozenset(["a", "b"]))
        r = parent.restrict(child)
        assert r.allowed_tools == frozenset(["a", "b"])

    def test_restrict_child_cannot_expand(self):
        parent = AgentCapabilityScope(allowed_tools=frozenset(["a"]))
        child = AgentCapabilityScope(allowed_tools=None)  # wants all
        r = parent.restrict(child)
        assert r.allowed_tools == frozenset(["a"])

    def test_restrict_denied_union(self):
        p = AgentCapabilityScope(denied_tools=frozenset(["x"]))
        c = AgentCapabilityScope(denied_tools=frozenset(["y"]))
        r = p.restrict(c)
        assert "x" in r.denied_tools and "y" in r.denied_tools

    def test_restrict_budget_minimum(self):
        p = AgentCapabilityScope(max_tool_call_budget=50)
        c = AgentCapabilityScope(max_tool_call_budget=100)
        assert p.restrict(c).max_tool_call_budget == 50

    def test_restrict_depth_minimum(self):
        p = AgentCapabilityScope(max_spawn_depth=2)
        c = AgentCapabilityScope(max_spawn_depth=5)
        assert p.restrict(c).max_spawn_depth == 2

    def test_from_dict_to_dict_roundtrip(self):
        data = {
            "allowed_tools": ["a", "b"],
            "denied_tools": ["c"],
            "max_tool_call_budget": 30,
            "max_spawn_depth": 2,
        }
        s = AgentCapabilityScope.from_dict(data)
        assert s.max_tool_call_budget == 30
        assert "c" in s.denied_tools
        d = s.to_dict()
        assert set(d["allowed_tools"]) == {"a", "b"}

    def test_from_dict_none_allowed_tools(self):
        s = AgentCapabilityScope.from_dict({"allowed_tools": None})
        assert s.allowed_tools is None


class TestScopeManager:
    def test_root_depth_is_one(self):
        m = ScopeManager()
        m.register("root", None)
        assert m.get_depth("root") == 1

    def test_child_depth_increments(self):
        m = ScopeManager()
        m.register("root", None)
        m.register("child", None, parent_id="root")
        assert m.get_depth("child") == 2

    def test_can_spawn_below_max(self):
        m = ScopeManager()
        m.register("root", AgentCapabilityScope(max_spawn_depth=3))
        assert m.can_spawn("root")

    def test_cannot_spawn_at_max(self):
        m = ScopeManager()
        m.register("root", AgentCapabilityScope(max_spawn_depth=1))
        m.register("child", None, parent_id="root")
        assert not m.can_spawn("child")

    def test_update_scope_restricts(self):
        m = ScopeManager()
        m.register("a1", AgentCapabilityScope(allowed_tools=frozenset(["a", "b", "c"])))
        m.update_scope("a1", AgentCapabilityScope(allowed_tools=frozenset(["a", "b"])))
        s = m.get_scope("a1")
        assert s.allowed_tools == frozenset(["a", "b"])

    def test_inherit_parent_scope(self):
        m = ScopeManager()
        parent_scope = AgentCapabilityScope(allowed_tools=frozenset(["read_file"]))
        m.register("parent", parent_scope)
        m.register("child", None, parent_id="parent")
        cs = m.get_scope("child")
        assert cs.allowed_tools == frozenset(["read_file"])

    def test_unknown_agent_returns_none(self):
        m = ScopeManager()
        assert m.get_scope("unknown") is None


class TestPermissionCheckerWithScope:
    def _policy(self):
        return SecurityPolicy(
            permission_rules=(PermissionRule("run_bash", "allow"),), default_action="deny"
        )

    def test_scope_deny_overrides_global_allow(self):
        mgr = ScopeManager()
        mgr.register("a1", AgentCapabilityScope(denied_tools=frozenset(["run_bash"])))
        checker = PermissionChecker(policy=self._policy(), scope_manager=mgr)
        assert checker.check("run_bash", agent_id="a1") == "deny"

    def test_no_scope_uses_global_policy(self):
        mgr = ScopeManager()
        checker = PermissionChecker(policy=self._policy(), scope_manager=mgr)
        assert checker.check("run_bash", agent_id="unregistered") == "allow"

    def test_no_scope_manager_uses_policy(self):
        checker = PermissionChecker(policy=self._policy())
        assert checker.check("run_bash") == "allow"
