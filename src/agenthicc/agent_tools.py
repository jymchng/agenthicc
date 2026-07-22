"""Built-in @tool() functions available to every agenthicc agent session.

Tools are organised by domain and implemented in their respective sub-packages:

  tools/fs/agent_tools.py      — 24 filesystem tools
  tools/git/agent_tools.py     — 11 git tools
  tools/exec/agent_tools.py    — 6 shell/exec tools
  tools/outlook/agent_tools.py — 9 Outlook/calendar tools (Win32 or Graph API)

This module re-exports all individual tools, the combined AGENT_TOOLS list,
and the BUILTIN_GROUPS list used by ToolRegistry for structured system-prompt
sections and subagent glob expansion (PRD-125).
"""

from __future__ import annotations

# ── filesystem ────────────────────────────────────────────────────────────────
from agenthicc.tools.fs.agent_tools import (
    FS_AGENT_TOOLS,
    append_file,
    copy_file,
    delete_file,
    file_exists,
    get_file_info,
    grep_files,
    list_directory,
    make_directory,
    move_file,
    patch_file,
    read_file,
    read_lines,
    search_files,
    write_file,
)

# ── git ───────────────────────────────────────────────────────────────────────
from agenthicc.tools.git.agent_tools import (
    GIT_AGENT_TOOLS,
    git_add,
    git_blame,
    git_branch,
    git_checkout,
    git_commit,
    git_diff,
    git_grep,
    git_log,
    git_show,
    git_stash,
    git_status,
)

# ── shell / execution ─────────────────────────────────────────────────────────
from agenthicc.tools.exec.agent_tools import (
    EXEC_AGENT_TOOLS,
    run_bash,
    run_command,
    run_python,
    run_python_expr,
    run_tests,
    shell,
)

# ── outlook / calendar ────────────────────────────────────────────────────────
from agenthicc.tools.outlook.agent_tools import (
    OUTLOOK_AGENT_TOOLS,
    calendar_events,
    create_event,
    list_emails,
    list_folders,
    move_email,
    read_email,
    reply_email,
    search_emails,
    send_email,
)

from agenthicc.plugins.registry import ToolGroup

__all__ = [
    # fs
    "append_file",
    "copy_file",
    "delete_file",
    "file_exists",
    "get_file_info",
    "grep_files",
    "list_directory",
    "make_directory",
    "move_file",
    "patch_file",
    "read_file",
    "read_lines",
    "search_files",
    "write_file",
    # git
    "git_add",
    "git_blame",
    "git_branch",
    "git_checkout",
    "git_commit",
    "git_diff",
    "git_grep",
    "git_log",
    "git_show",
    "git_stash",
    "git_status",
    # exec
    "run_bash",
    "run_command",
    "run_python",
    "run_python_expr",
    "run_tests",
    "shell",
    # outlook
    "calendar_events",
    "create_event",
    "list_emails",
    "list_folders",
    "move_email",
    "read_email",
    "reply_email",
    "search_emails",
    "send_email",
    # aggregates
    "FS_AGENT_TOOLS",
    "GIT_AGENT_TOOLS",
    "EXEC_AGENT_TOOLS",
    "OUTLOOK_AGENT_TOOLS",
    "AGENT_TOOLS",
    # namespace groups (PRD-125)
    "FS_GROUP",
    "GIT_GROUP",
    "EXEC_GROUP",
    "OUTLOOK_GROUP",
    "BUILTIN_GROUPS",
]

#: All agent tools — filesystem + git + exec + outlook.
AGENT_TOOLS = [
    *FS_AGENT_TOOLS,
    *GIT_AGENT_TOOLS,
    *EXEC_AGENT_TOOLS,
    *OUTLOOK_AGENT_TOOLS,
]

# ── Tool groups for structured system-prompt sections (PRD-125) ───────────────

FS_GROUP = ToolGroup(
    name="fs",
    label="File System",
    description="Read, write, search, and patch files within the workspace.",
    tools=list(FS_AGENT_TOOLS),
    priority=4,
)

GIT_GROUP = ToolGroup(
    name="git",
    label="Git",
    description="Query history, stage changes, and commit to the repository.",
    tools=list(GIT_AGENT_TOOLS),
    priority=3,
)

EXEC_GROUP = ToolGroup(
    name="exec",
    label="Shell / Exec",
    description="Run shell commands, Python snippets, and the test suite.",
    tools=list(EXEC_AGENT_TOOLS),
    priority=2,
)

OUTLOOK_GROUP = ToolGroup(
    name="outlook",
    label="Outlook / Calendar",
    description="Read and send email, manage calendar events via Graph API.",
    tools=list(OUTLOOK_AGENT_TOOLS),
    priority=1,
)

#: Ordered list of built-in ToolGroups used by ToolRegistry (PRD-125).
BUILTIN_GROUPS: list[ToolGroup] = [FS_GROUP, GIT_GROUP, EXEC_GROUP, OUTLOOK_GROUP]
