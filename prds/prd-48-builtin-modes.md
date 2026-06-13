---
title: "PRD-48: Built-in Modes — Auto, Plan, Ask, Review, Safe, Debug"
status: draft
version: 0.1.0
created: 2026-06-13
depends-on: prd-47-mode-system-architecture.md
---

# PRD-48: Built-in Modes

## Executive Summary

Six built-in modes ship with agenthicc.  They cover the most common workflows:
unconstrained operation, upfront planning, confirmation-gated action, read-only
review, hard sandboxing, and diagnostic tracing.

---

## Mode Catalogue

| Name | Label | Colour | Purpose |
|---|---|---|---|
| **Auto** | AUTO | green | Default — full permissions, standard behaviour |
| **Plan** | PLAN | yellow | Read-only; agent produces a plan but takes no action |
| **Ask** | ASK | cyan | Agent confirms every destructive action before running it |
| **Review** | REVIEW | blue | Read-only; agent reviews code or changes and provides feedback |
| **Safe** | SAFE | magenta | Hard read-only sandbox; no writes, no shell execution |
| **Debug** | DEBUG | red | Verbose — appends reasoning traces and tool timing to responses |

Shift+Tab cycles in the order listed above.

---

## 1. Auto Mode (default)

```python
Mode(
    name="Auto",
    label="AUTO",
    description="Full permissions — agent acts autonomously (default).",
    colour="green",
    system_patch="",   # no patch — use base system prompt as-is
    tool_filter=None,  # all tools allowed
    source_id="builtin",
)
```

No changes to behaviour.  Badge: `[AUTO]` in green.

---

## 2. Plan Mode

```python
_PLAN_PATCH = """\
[MODE: PLAN]
You are in PLAN mode. You MUST NOT write files, delete files, execute shell
commands, commit code, or call any tool that has side-effects. Your only
allowed actions are reading files, listing directories, and searching.

Instead of acting, produce a numbered step-by-step plan of exactly what you
WOULD do to complete the task. For each step include:
  - The action (e.g. "Edit src/auth.py lines 42-55")
  - The reason (e.g. "Fix the null-pointer when token is empty")
  - The tool you would use (e.g. `write_file`)

End your plan with: "Switch to Auto mode (Shift+Tab) to execute this plan."
"""

_PLAN_TOOLS_ALLOWED = frozenset({
    "read_file", "read_lines", "list_directory", "list_files",
    "search_files", "grep_files", "get_file_info", "file_exists",
    "git_status", "git_diff", "git_log", "git_show", "git_blame",
    "git_grep", "git_branch",
})

def _plan_filter(name: str, _ctx: dict) -> bool:
    return name in _PLAN_TOOLS_ALLOWED

Mode(
    name="Plan",
    label="PLAN",
    description="Read-only — agent plans but does not act.",
    colour="yellow",
    system_patch=_PLAN_PATCH,
    tool_filter=_plan_filter,
    source_id="builtin",
)
```

Badge: `[PLAN]` in yellow.

---

## 3. Ask Mode

Ask mode keeps all tools available but the agent's instructions require it to
explain what it is about to do and wait for approval before any destructive
operation.  This is enforced through the system prompt only (no tool filter);
the agent is trusted to follow the instructions.

```python
_ASK_PATCH = """\
[MODE: ASK]
You are in ASK mode. Before calling any tool that writes, deletes, moves,
executes, or commits (write_file, append_file, delete_file, move_file,
copy_file, patch_file, run_bash, shell, run_command, run_python,
run_python_expr, run_tests, git_add, git_commit, git_checkout, git_stash),
you MUST first describe the action you are about to take and ask the user
for explicit confirmation with a [Y/n] prompt in your reply.

If the user confirms, proceed. If they decline, stop and ask how to proceed.
For read-only operations (reading files, searching, git log/diff/status),
you may proceed without asking.
"""

Mode(
    name="Ask",
    label="ASK",
    description="Confirmation-gated — agent asks before destructive actions.",
    colour="cyan",
    system_patch=_ASK_PATCH,
    tool_filter=None,   # all tools available; agent self-gates via instructions
    source_id="builtin",
)
```

Badge: `[ASK]` in cyan.

---

## 4. Review Mode

```python
_REVIEW_PATCH = """\
[MODE: REVIEW]
You are in REVIEW mode. Your role is to review code, files, and changes.
You MUST NOT write, modify, delete, move, or execute anything.

Provide a structured review with:
  1. Summary of what the code/diff does
  2. Issues found (bugs, security concerns, performance problems)
  3. Suggestions for improvement
  4. Overall assessment (Approve / Request Changes / Needs Discussion)

Use only read-only tools: read_file, list_directory, git_diff, git_log, etc.
"""

_REVIEW_ALLOWED = frozenset({
    "read_file", "read_lines", "list_directory", "list_files",
    "search_files", "grep_files", "get_file_info", "file_exists",
    "git_status", "git_diff", "git_log", "git_show", "git_blame",
    "git_grep", "git_branch",
})

Mode(
    name="Review",
    label="REVIEW",
    description="Read-only — agent reviews code and provides structured feedback.",
    colour="blue",
    system_patch=_REVIEW_PATCH,
    tool_filter=lambda name, _: name in _REVIEW_ALLOWED,
    source_id="builtin",
)
```

Badge: `[REVIEW]` in blue.

---

## 5. Safe Mode

Safe mode is a hard sandbox — only the most conservative read tools are
allowed.  Suitable when running agenthicc against untrusted codebases.

```python
_SAFE_PATCH = """\
[MODE: SAFE]
You are in SAFE mode. You may ONLY read files and directory listings.
You cannot execute shell commands, write files, run tests, or perform
any git operations that have side-effects (add/commit/checkout/stash).
If asked to do something that requires write or exec access, explain that
Safe mode prevents it and suggest the user switch modes with Shift+Tab.
"""

_SAFE_ALLOWED = frozenset({
    "read_file", "read_lines", "list_directory", "list_files",
    "search_files", "grep_files", "get_file_info", "file_exists",
    "git_status", "git_diff", "git_log", "git_show",
})

Mode(
    name="Safe",
    label="SAFE",
    description="Hard sandbox — read-only filesystem access only.",
    colour="magenta",
    system_patch=_SAFE_PATCH,
    tool_filter=lambda name, _: name in _SAFE_ALLOWED,
    source_id="builtin",
)
```

Badge: `[SAFE]` in magenta.

---

## 6. Debug Mode

Debug mode adds verbose output to every response: timing for each tool call,
token counts per turn, and the raw reasoning trace when the model supports it.
It uses a post-flight hook to append the diagnostics.

```python
import time

_DEBUG_PATCH = """\
[MODE: DEBUG]
You are in DEBUG mode. After completing your response, append a
--- DEBUG --- section containing:
  - Each tool you called, in order, with the time it took
  - Total tokens used this turn (if known)
  - Any warnings, retries, or fallbacks that occurred
Be explicit about every decision you made and why.
"""

def _debug_post_hook(content: str, renderer: Any) -> str:
    """Append live timing/token stats to the agent's response."""
    s = getattr(renderer, "_status", None)
    if s is None:
        return content
    elapsed = time.monotonic() - s.intent_started_at if s.intent_started_at else 0
    footer = (
        f"\n\n```\n"
        f"[DEBUG] elapsed={elapsed:.1f}s  "
        f"in={s.input_tokens:,} out={s.output_tokens:,}  "
        f"cost=${s.session_cost_usd:.4f}\n"
        f"```"
    )
    return content + footer

Mode(
    name="Debug",
    label="DEBUG",
    description="Verbose — appends timing, token counts, and reasoning traces.",
    colour="red",
    system_patch=_DEBUG_PATCH,
    tool_filter=None,
    post_hook=_debug_post_hook,
    source_id="builtin",
)
```

Badge: `[DEBUG]` in red.

---

## `BUILTIN_MODES` and `build_default_registry()`

```python
# src/agenthicc/modes/builtins.py

BUILTIN_MODES: list[Mode] = [Auto, Plan, Ask, Review, Safe, Debug]

def build_default_registry() -> ModeRegistry:
    reg = ModeRegistry()
    reg.register_many(BUILTIN_MODES)
    return reg
```

---

## Future Built-in Modes (planned, not in v1)

| Name | Purpose |
|---|---|
| **Pair** | Pair-programming mode — agent narrates as it goes, explains every decision |
| **Teach** | Explains code in detail; avoids just doing things for the user |
| **Strict** | Follows a project-specific `.agenthicc/rules.md` file exactly |
| **Headless** | No interactive prompts; outputs structured JSON for CI pipelines |
| **Yolo** | No confirmations even in Ask mode; maximum autonomy |

---

## Tests

```python
# tests/unit/test_builtin_modes.py  (pytestmark = pytest.mark.unit)

def test_auto_mode_no_filter():
    from agenthicc.modes import build_default_registry, ModeManager
    reg = build_default_registry()
    mgr = ModeManager(reg)
    assert mgr.active_name == "Auto"
    _, tools = mgr.apply_to_agent("base", ["write_file", "read_file"])
    assert "write_file" in tools

def test_plan_mode_blocks_write():
    from agenthicc.modes import build_default_registry, ModeManager
    reg = build_default_registry()
    mgr = ModeManager(reg)
    mgr.set("Plan")
    _, tools = mgr.apply_to_agent("base", ["write_file", "read_file", "git_status"])
    assert "write_file" not in tools
    assert "read_file" in tools
    assert "git_status" in tools

def test_plan_mode_patches_system():
    from agenthicc.modes import build_default_registry, ModeManager
    reg = build_default_registry()
    mgr = ModeManager(reg)
    mgr.set("Plan")
    sys, _ = mgr.apply_to_agent("Base.", [])
    assert "PLAN" in sys
    assert "MUST NOT" in sys

def test_safe_mode_most_restrictive():
    from agenthicc.modes import build_default_registry, ModeManager
    reg = build_default_registry()
    mgr = ModeManager(reg, default_name="Safe")
    _, tools = mgr.apply_to_agent("base", [
        "write_file", "run_bash", "git_commit", "read_file"
    ])
    assert tools == ["read_file"]

def test_review_mode_blocks_exec():
    from agenthicc.modes import build_default_registry, ModeManager
    reg = build_default_registry()
    mgr = ModeManager(reg)
    mgr.set("Review")
    _, tools = mgr.apply_to_agent("base", ["run_bash", "read_file", "git_diff"])
    assert "run_bash" not in tools
    assert "git_diff" in tools

def test_debug_mode_post_hook():
    from agenthicc.modes import build_default_registry, ModeManager
    from unittest.mock import MagicMock
    reg = build_default_registry()
    mgr = ModeManager(reg)
    mgr.set("Debug")
    mode = mgr.active
    assert mode.post_hook is not None
    renderer = MagicMock()
    renderer._status.intent_started_at = 0
    renderer._status.input_tokens = 100
    renderer._status.output_tokens = 50
    renderer._status.session_cost_usd = 0.001
    result = mode.post_hook("Hello world.", renderer)
    assert "DEBUG" in result
    assert "Hello world." in result

def test_cycle_order():
    from agenthicc.modes import build_default_registry, ModeManager
    reg = build_default_registry()
    mgr = ModeManager(reg)
    visited = set()
    for _ in range(len(reg)):
        visited.add(mgr.active_name)
        mgr.cycle()
    assert "Auto" in visited
    assert "Plan" in visited
    assert "Debug" in visited
    assert len(visited) == len(reg)
```
