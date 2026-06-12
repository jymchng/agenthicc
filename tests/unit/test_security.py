"""Unit tests for agenthicc.security.PermissionChecker (PRD-07)."""

from __future__ import annotations

import pytest

from agenthicc.kernel import PermissionRule, SecurityPolicy
from agenthicc.security import PermissionChecker

pytestmark = pytest.mark.unit


def make_checker(rules: list[PermissionRule], default_action: str = "deny") -> PermissionChecker:
    return PermissionChecker(
        SecurityPolicy(permission_rules=tuple(rules), default_action=default_action)
    )


class TestGlobMatching:
    def test_allow_rule_matches_glob(self):
        checker = make_checker([PermissionRule(tool_pattern="read_*", action="allow")])
        assert checker.check("read_file") == "allow"
        assert checker.check("read_directory") == "allow"

    def test_exact_pattern_matches_only_that_tool(self):
        checker = make_checker([PermissionRule(tool_pattern="shell_exec", action="allow")])
        assert checker.check("shell_exec") == "allow"
        assert checker.check("shell_exec_unsafe") == "deny"


class TestRuleOrdering:
    def test_deny_precedence_by_order(self):
        """First matching rule wins: an earlier deny beats a later catch-all allow."""
        checker = make_checker([
            PermissionRule(tool_pattern="write_*", action="deny"),
            PermissionRule(tool_pattern="*", action="allow"),
        ])
        assert checker.check("write_file") == "deny"
        assert checker.check("read_file") == "allow"

    def test_first_matching_allow_beats_later_deny(self):
        checker = make_checker([
            PermissionRule(tool_pattern="read_*", action="allow"),
            PermissionRule(tool_pattern="*", action="deny"),
        ])
        assert checker.check("read_file") == "allow"
        assert checker.check("write_file") == "deny"


class TestDefaultAction:
    def test_default_deny_when_no_match(self):
        checker = make_checker([PermissionRule(tool_pattern="read_*", action="allow")])
        assert checker.check("shell_exec") == "deny"

    def test_empty_policy_denies_everything(self):
        checker = make_checker([])
        assert checker.check("read_file") == "deny"
        assert checker.check("anything") == "deny"

    def test_default_action_is_policy_default(self):
        checker = make_checker([], default_action="require_confirmation")
        assert checker.check("read_file") == "require_confirmation"


class TestRequireConfirmation:
    def test_require_confirmation_action(self):
        checker = make_checker([
            PermissionRule(tool_pattern="fs_delete", action="require_confirmation"),
            PermissionRule(tool_pattern="fs_*", action="allow"),
        ])
        assert checker.check("fs_delete") == "require_confirmation"
        assert checker.check("fs_read") == "allow"


class TestPathPrefixCondition:
    def test_path_prefix_allows_inside(self):
        checker = make_checker([
            PermissionRule(
                tool_pattern="fs_*",
                action="allow",
                conditions={"path_prefix": ["/workspace"]},
            ),
        ])
        assert checker.check("fs_read", {"path": "/workspace/src/main.py"}) == "allow"

    def test_path_prefix_denies_outside(self):
        checker = make_checker([
            PermissionRule(
                tool_pattern="fs_*",
                action="allow",
                conditions={"path_prefix": ["/workspace"]},
            ),
        ])
        # rule does not fire -> fail-closed default deny
        assert checker.check("fs_read", {"path": "/etc/passwd"}) == "deny"

    def test_path_prefix_any_of_multiple(self):
        checker = make_checker([
            PermissionRule(
                tool_pattern="fs_*",
                action="allow",
                conditions={"path_prefix": ["/workspace", "/tmp/agenthicc"]},
            ),
        ])
        assert checker.check("fs_read", {"path": "/tmp/agenthicc/cache"}) == "allow"
        assert checker.check("fs_read", {"path": "/tmp/other"}) == "deny"

    def test_path_prefix_with_missing_path_condition_does_not_match(self):
        checker = make_checker([
            PermissionRule(
                tool_pattern="fs_*",
                action="allow",
                conditions={"path_prefix": ["/workspace"]},
            ),
        ])
        assert checker.check("fs_read") == "deny"
        assert checker.check("fs_read", {}) == "deny"


class TestNetworkDomainCondition:
    def test_allowed_host(self):
        checker = make_checker([
            PermissionRule(
                tool_pattern="http_*",
                action="allow",
                conditions={"network_domain": ["api.anthropic.com"]},
            ),
        ])
        assert checker.check("http_get", {"host": "api.anthropic.com"}) == "allow"

    def test_subdomain_of_allowed_domain(self):
        checker = make_checker([
            PermissionRule(
                tool_pattern="http_*",
                action="allow",
                conditions={"network_domain": ["example.com"]},
            ),
        ])
        assert checker.check("http_get", {"host": "api.example.com"}) == "allow"

    def test_blocked_host_falls_through_to_default_deny(self):
        checker = make_checker([
            PermissionRule(
                tool_pattern="http_*",
                action="allow",
                conditions={"network_domain": ["api.anthropic.com"]},
            ),
        ])
        assert checker.check("http_get", {"host": "evil.attacker.com"}) == "deny"

    def test_host_extracted_from_url_condition(self):
        checker = make_checker([
            PermissionRule(
                tool_pattern="http_*",
                action="allow",
                conditions={"network_domain": ["pypi.org"]},
            ),
        ])
        assert checker.check("http_get", {"url": "https://pypi.org/simple/"}) == "allow"
        assert checker.check("http_get", {"url": "https://evil.com/x"}) == "deny"


class TestUnknownConditions:
    def test_unknown_condition_key_fails_closed(self):
        checker = make_checker([
            PermissionRule(
                tool_pattern="*",
                action="allow",
                conditions={"mystery_condition": True},
            ),
        ])
        assert checker.check("read_file", {"path": "/workspace/a"}) == "deny"
