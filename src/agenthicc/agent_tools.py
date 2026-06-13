"""Built-in @tool() functions available to every agenthicc agent session.

Tools are organised by domain and implemented in their respective sub-packages:

  tools/fs/agent_tools.py      — 14 filesystem tools
  tools/git/agent_tools.py     — 11 git tools
  tools/exec/agent_tools.py    — 6 shell/exec tools
  tools/outlook/agent_tools.py — 9 Outlook/calendar tools (Win32 or Graph API)

This module re-exports all individual tools and the combined AGENT_TOOLS list.
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

__all__ = [
    # fs
    "append_file", "copy_file", "delete_file", "file_exists", "get_file_info",
    "grep_files", "list_directory", "make_directory", "move_file", "patch_file",
    "read_file", "read_lines", "search_files", "write_file",
    # git
    "git_add", "git_blame", "git_branch", "git_checkout", "git_commit",
    "git_diff", "git_grep", "git_log", "git_show", "git_stash", "git_status",
    # exec
    "run_bash", "run_command", "run_python", "run_python_expr", "run_tests", "shell",
    # outlook
    "calendar_events", "create_event", "list_emails", "list_folders",
    "move_email", "read_email", "reply_email", "search_emails", "send_email",
    # aggregates
    "FS_AGENT_TOOLS", "GIT_AGENT_TOOLS", "EXEC_AGENT_TOOLS", "OUTLOOK_AGENT_TOOLS",
    "AGENT_TOOLS",
]

#: All agent tools — filesystem + git + exec + outlook.
AGENT_TOOLS = [
    *FS_AGENT_TOOLS,
    *GIT_AGENT_TOOLS,
    *EXEC_AGENT_TOOLS,
    *OUTLOOK_AGENT_TOOLS,
]
