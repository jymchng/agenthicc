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


class TestSubagentPermissionMergeTable:
    """SUBAGENT-05 criterion 8: effective scope == inherited.restrict(declared).

    Each row is one type declaration applied by the subagent permission path
    (``SubagentExecutionPolicy.restrict_by``). The expectation is computed with
    ``AgentCapabilityScope.restrict`` itself — never a reimplementation — so the
    table fails if the subagent path ever diverges from the scope algebra.
    """

    INHERITED = AgentCapabilityScope(
        allowed_tools=frozenset({"read_file", "grep_files", "run_bash", "outlook_send"}),
        denied_tools=frozenset({"write_file"}),
        max_tool_call_budget=40,
    )

    ROWS: tuple[tuple[str, frozenset[str], frozenset[str], frozenset[str]], ...] = (
        ("empty declaration", frozenset(), frozenset(), frozenset()),
        ("deny one allowed tool", frozenset({"run_bash"}), frozenset(), frozenset()),
        ("deny by wildcard", frozenset({"outlook_*"}), frozenset(), frozenset()),
        ("deny a tool not inherited", frozenset({"web_fetch"}), frozenset(), frozenset()),
        ("repeat an inherited deny", frozenset({"write_file"}), frozenset(), frozenset()),
        ("deny a capability", frozenset(), frozenset({"execute"}), frozenset()),
        ("require approval", frozenset(), frozenset(), frozenset({"write"})),
        (
            "deny and require the same capability",
            frozenset(),
            frozenset({"network"}),
            frozenset({"network"}),
        ),
        (
            "everything at once",
            frozenset({"run_bash", "outlook_*"}),
            frozenset({"execute"}),
            frozenset({"read"}),
        ),
    )

    @staticmethod
    def _inherited_policy() -> object:
        from agenthicc.subagents.policy import SubagentExecutionPolicy

        return SubagentExecutionPolicy(
            mode_name="Safe",
            badge="",
            color="",
            description="",
            system_prompt_suffix="",
            blocked_capabilities=frozenset({"admin"}),
            approval_required=frozenset({"write"}),
            visible_tool_names=frozenset(
                TestSubagentPermissionMergeTable.INHERITED.allowed_tools or ()
            ),
            capability_scope=TestSubagentPermissionMergeTable.INHERITED,
        )

    @pytest.mark.parametrize(
        ("name", "deny_tools", "deny_capabilities", "require_approval"),
        ROWS,
        ids=[row[0] for row in ROWS],
    )
    def test_effective_scope_is_the_restriction_of_the_declaration(
        self,
        name: str,
        deny_tools: frozenset[str],
        deny_capabilities: frozenset[str],
        require_approval: frozenset[str],
    ) -> None:
        from agenthicc.subagents.policy import SubagentExecutionPolicy
        from agenthicc.subagents.types import SubagentPermissionConstraints

        inherited = self._inherited_policy()
        assert isinstance(inherited, SubagentExecutionPolicy)
        declared = SubagentPermissionConstraints(
            deny_tools=deny_tools,
            deny_capabilities=deny_capabilities,
            require_approval=require_approval,
        )

        effective = inherited.restrict_by(None, permissions=declared, type_name="typed")

        expected_scope = self.INHERITED.restrict(AgentCapabilityScope(denied_tools=deny_tools))
        assert effective.capability_scope == expected_scope, name
        # Narrow-only: nothing the inherited scope refused becomes allowed.
        for tool in ("read_file", "grep_files", "run_bash", "outlook_send", "write_file", "web"):
            if not self.INHERITED.is_tool_allowed(tool):
                assert not effective.capability_scope.is_tool_allowed(tool), (name, tool)
        expected_blocked = inherited.blocked_capabilities | deny_capabilities
        assert effective.blocked_capabilities == expected_blocked, name
        assert effective.approval_required == (
            (inherited.approval_required | require_approval) - expected_blocked
        ), name
        assert inherited.blocked_capabilities <= effective.blocked_capabilities
