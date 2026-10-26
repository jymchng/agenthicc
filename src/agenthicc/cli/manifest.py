"""Declarative manifest of built-in CLI commands (AGX-01).

The CLI resolves a command path against this table and imports only the
module that implements it, instead of importing every command module to
let decorators register themselves. Plugin commands in ``~/.agenthicc/cli/``
and ``.agenthicc/cli/`` still register through ``registry.command`` and
take precedence over a built-in command with the same path.

A handler is referenced as ``"module:function"``; its arguments are inferred
from its signature only after its module is imported.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

__all__ = ["BUILTIN_COMMANDS", "BUILTIN_GROUPS", "CommandRef"]


@dataclass(frozen=True)
class CommandRef:
    """One built-in command: its path, implementation and help text."""

    path: tuple[str, ...]
    target: str
    help: str


def _ref(module: str, function: str, *path: str, help: str) -> CommandRef:
    return CommandRef(path=path, target=f"agenthicc.cli.commands.{module}:{function}", help=help)


BUILTIN_COMMANDS: tuple[CommandRef, ...] = (
    # agenthicc.cli.commands.auth
    _ref("auth", "login", "login", help="Authenticate with agenthicc.ai"),
    _ref("auth", "logout", "logout", help="Log out and revoke stored tokens"),
    _ref("auth", "whoami", "whoami", help="Show the currently authenticated user"),
    # agenthicc.cli.commands.keybindings
    _ref("keybindings", "keybindings_show", "keybindings", help="Show your key bindings"),
    _ref(
        "keybindings",
        "keybindings_edit",
        "keybindings",
        "edit",
        help="Open your key bindings file in $EDITOR",
    ),
    # agenthicc.cli.commands.background
    _ref(
        "background",
        "agents",
        "agents",
        help="Open the agents hub: running work, what needs you, history and the library",
    ),
    _ref("background", "run", "run", help="Start an agent turn or workflow"),
    _ref("background", "agents_list", "agents", "list", help="List background agents"),
    _ref("background", "agents_status", "agents", "status", help="Show one background agent"),
    _ref("background", "agents_cancel", "agents", "cancel", help="Stop a background agent"),
    _ref("background", "agents_resume", "agents", "resume", help="Resume a background agent"),
    _ref("background", "agents_retry", "agents", "retry", help="Retry a failed background agent"),
    _ref(
        "background",
        "agents_priority",
        "agents",
        "priority",
        help="Change a background agent's scheduling priority",
    ),
    _ref(
        "background",
        "agents_approve",
        "agents",
        "approve",
        help="Approve a waiting background agent",
    ),
    _ref(
        "background", "agents_reject", "agents", "reject", help="Reject a waiting background agent"
    ),
    _ref(
        "background",
        "agents_input",
        "agents",
        "input",
        help="Provide input to a waiting background agent",
    ),
    _ref(
        "background",
        "agents_inbox_list",
        "agents",
        "inbox",
        "list",
        help="List pending background approvals and questions",
    ),
    _ref(
        "background",
        "agents_inbox_answer",
        "agents",
        "inbox",
        "answer",
        help="Answer a pending question or input request",
    ),
    _ref(
        "background",
        "agents_inbox_approve",
        "agents",
        "inbox",
        "approve",
        help="Approve the exact pending tool request",
    ),
    _ref(
        "background",
        "agents_inbox_reject",
        "agents",
        "inbox",
        "reject",
        help="Reject the exact pending tool request",
    ),
    _ref(
        "background",
        "agents_inbox_expire",
        "agents",
        "inbox",
        "expire",
        help="Expire an exact pending interaction",
    ),
    _ref(
        "background",
        "agents_notifications",
        "agents",
        "notifications",
        help="Inspect background notification delivery status",
    ),
    _ref("background", "agents_rename", "agents", "rename", help="Rename a background agent"),
    _ref(
        "background",
        "agents_labels",
        "agents",
        "labels",
        help="Set comma-separated labels on a background agent",
    ),
    _ref(
        "background",
        "agents_purge",
        "agents",
        "purge",
        help="Permanently remove expired background trash",
    ),
    _ref("background", "agents_archive", "agents", "archive", help="Archive a background agent"),
    _ref(
        "background",
        "agents_delete",
        "agents",
        "delete",
        help="Move a background agent to recoverable trash",
    ),
    _ref(
        "background",
        "agents_restore",
        "agents",
        "restore",
        help="Restore a deleted background agent",
    ),
    # agenthicc.cli.commands.config
    _ref(
        "config", "config_show", "config", "show", help="Print the merged effective configuration"
    ),
    _ref(
        "config",
        "config_validate",
        "config",
        "validate",
        help="Validate the effective provider configuration",
    ),
    _ref(
        "config", "config_profiles", "config", "profiles", help="List configured provider profiles"
    ),
    _ref(
        "config",
        "config_init",
        "config",
        "init",
        help="Create a template agenthicc.toml in .agenthicc/",
    ),
    # agenthicc.cli.commands.init
    _ref(
        "init",
        "init_project",
        "init",
        help="Create AGENTS.md and a commented .agenthicc configuration template",
    ),
    # agenthicc.cli.commands.mcp
    _ref("mcp", "mcp_add", "mcp", "add", help="Add an MCP server to configuration"),
    _ref("mcp", "mcp_list", "mcp", "list", help="List configured MCP servers"),
    _ref("mcp", "mcp_get", "mcp", "get", help="Show one configured MCP server"),
    _ref("mcp", "mcp_remove", "mcp", "remove", help="Remove one configured MCP server"),
    _ref(
        "mcp",
        "mcp_connect",
        "mcp",
        "connect",
        help="Connect to one MCP server and inspect its catalog",
    ),
    _ref(
        "mcp",
        "mcp_disconnect",
        "mcp",
        "disconnect",
        help="Disconnect an MCP server in the current process",
    ),
    _ref("mcp", "mcp_refresh", "mcp", "refresh", help="Refresh one MCP server's tool catalog"),
    _ref(
        "mcp", "mcp_doctor", "mcp", "doctor", help="Validate and diagnose MCP server connectivity"
    ),
    _ref("mcp", "mcp_auth", "mcp", "auth", help="Check configured MCP authentication"),
    _ref("mcp", "mcp_logout", "mcp", "logout", help="Remove stored MCP authentication"),
    # agenthicc.cli.commands.runs
    _ref("runs", "runs", "runs", help="List durable goal runs"),
    _ref("runs", "runs_list", "runs", "list", help="List durable goal runs"),
    _ref("runs", "runs_show", "runs", "show", help="Show one durable goal run"),
    _ref(
        "runs",
        "runs_cancel",
        "runs",
        "cancel",
        help="Cancel a goal run and its active main session",
    ),
    _ref("runs", "runs_resume", "runs", "resume", help="Resume a recoverable goal run"),
    _ref("runs", "attach", "attach", help="Attach the TUI to one exact background session"),
    # agenthicc.cli.commands.schedules
    _ref(
        "schedules",
        "schedules_root",
        "schedules",
        help="List repository schedules; use subcommands to manage them",
    ),
    _ref(
        "schedules", "schedules_list", "schedules", "list", help="List schedules for a repository"
    ),
    _ref(
        "schedules",
        "schedules_create",
        "schedules",
        "create",
        help="Create a disabled repository schedule",
    ),
    _ref(
        "schedules",
        "schedules_show",
        "schedules",
        "show",
        help="Inspect a schedule and its recent run history",
    ),
    _ref("schedules", "schedules_pause", "schedules", "pause", help="Pause future occurrences"),
    _ref(
        "schedules",
        "schedules_resume",
        "schedules",
        "resume",
        help="Resume after confirming its next execution time",
    ),
    _ref(
        "schedules",
        "schedules_run_now",
        "schedules",
        "run-now",
        help="Queue an auditable immediate occurrence",
    ),
    _ref(
        "schedules",
        "schedules_retry",
        "schedules",
        "retry",
        help="Retry a failed occurrence as a new auditable attempt",
    ),
    _ref(
        "schedules",
        "schedules_update",
        "schedules",
        "update",
        help="Revise a schedule after explicit confirmation",
    ),
    _ref(
        "schedules",
        "schedules_delete",
        "schedules",
        "delete",
        help="Stop future occurrences and tombstone the definition",
    ),
    _ref(
        "schedules",
        "schedules_service_status",
        "schedules",
        "service-status",
        help="Show whether schedule execution is armed",
    ),
    _ref(
        "schedules",
        "schedules_history",
        "schedules",
        "history",
        help="Show bounded occurrence history",
    ),
    # agenthicc.cli.commands.session_service
    _ref(
        "session_service",
        "session_create",
        "session",
        "create",
        help="Create a client-neutral session",
    ),
    _ref("session_service", "session_list", "session", "list", help="List client-neutral sessions"),
    _ref(
        "session_service",
        "session_show",
        "session",
        "show",
        help="Show a client-neutral session snapshot",
    ),
    _ref(
        "session_service",
        "session_export",
        "session",
        "export",
        help="Export a redacted client-neutral session",
    ),
    _ref(
        "session_service",
        "session_events",
        "session",
        "events",
        help="Replay durable session events as JSON-lines",
    ),
    _ref(
        "session_service",
        "session_send",
        "session",
        "send",
        help="Submit a message to a client-neutral session",
    ),
    _ref(
        "session_service",
        "session_control",
        "session",
        "control",
        help="Submit a JSON session control command",
    ),
    _ref(
        "session_service",
        "session_serve",
        "session",
        "serve",
        help="Serve local session snapshots and events over HTTP/SSE",
    ),
    # agenthicc.cli.commands.sessions
    _ref(
        "sessions",
        "sessions_list",
        "sessions",
        "list",
        help="Open saved sessions in the agents hub (History tab)",
    ),
    _ref("sessions", "sessions_show", "sessions", "show", help="Show detail for one session"),
    _ref(
        "sessions",
        "sessions_export",
        "sessions",
        "export",
        help="Export one session as a redacted JSON document",
    ),
    _ref(
        "sessions",
        "sessions_inspect",
        "sessions",
        "inspect",
        help="Inspect one session's durable state",
    ),
    # agenthicc.cli.commands.skills
    _ref("skills", "skills_add", "skills", "add", help="Download and install a skill"),
    # agenthicc.cli.commands.trust
    _ref(
        "trust",
        "trust_list",
        "trust",
        "list",
        help="Show project extensions and whether each is trusted",
    ),
    _ref("trust", "trust_add", "trust", "add", help="Trust the current content of extension files"),
    _ref(
        "trust",
        "trust_revoke",
        "trust",
        "revoke",
        help="Revoke trust for a file or every file under a directory",
    ),
    # agenthicc.cli.commands.workflows
    _ref(
        "workflows", "workflows_list", "workflows", "list", help="List available workflow plugins"
    ),
    _ref(
        "workflows",
        "workflows_run",
        "workflows",
        "run",
        help="Run a workflow headlessly for one intent",
    ),
    # agenthicc.cli.commands.worktrees
    _ref(
        "worktrees",
        "worktrees_list",
        "worktrees",
        "list",
        help="List durable parallel orchestrations",
    ),
    _ref(
        "worktrees", "worktrees_show", "worktrees", "show", help="Show one orchestration manifest"
    ),
    _ref(
        "worktrees",
        "worktrees_recover",
        "worktrees",
        "recover",
        help="Reconcile one orchestration with Git worktrees",
    ),
    _ref(
        "worktrees",
        "worktrees_integrate",
        "worktrees",
        "integrate",
        help="Integrate a completed worker task",
    ),
    _ref(
        "worktrees",
        "worktrees_dispatch",
        "worktrees",
        "dispatch",
        help="Dispatch one task, approving its setup for this run",
    ),
    _ref(
        "worktrees",
        "worktrees_actions",
        "worktrees",
        "actions",
        help="List named actions available in one worker worktree",
    ),
    _ref(
        "worktrees",
        "worktrees_action",
        "worktrees",
        "action",
        help="Run one approved named worker action",
    ),
    _ref(
        "worktrees",
        "worktrees_setup",
        "worktrees",
        "setup",
        help="Run trusted or explicitly approved worker setup",
    ),
    _ref(
        "worktrees",
        "worktrees_trust",
        "worktrees",
        "trust",
        help="Trust the current environment manifest digest",
    ),
    _ref(
        "worktrees",
        "agents_actions",
        "agents",
        "actions",
        help="List actions declared for one worker session",
    ),
    _ref(
        "worktrees",
        "agents_action",
        "agents",
        "action",
        help="Run one explicitly approved worker action",
    ),
)

BUILTIN_GROUPS: Mapping[tuple[str, ...], str] = MappingProxyType(
    {
        ("agents",): "Open the agents hub and manage background agents",
        ("agents", "inbox"): "List and resolve background approvals and questions",
        ("config",): "Manage configuration",
        ("mcp",): "Manage configured MCP servers",
        ("runs",): "Inspect and control durable goal runs",
        ("schedules",): "Manage repository-scoped scheduled workflows",
        ("session",): "Inspect, control, and attach to client-neutral sessions",
        ("sessions",): "Manage saved sessions",
        ("skills",): "Install and manage skill definitions",
        ("trust",): "Review and trust this repository's Python extensions",
        ("workflows",): "Discover and run workflow plugins",
        ("worktrees",): "Inspect and recover parallel worker orchestration",
    }
)
