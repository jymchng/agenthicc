# Extensions

Extensions are discovered from project-local `.agenthicc/` directories and
user-global `~/.agenthicc/` directories. Project definitions generally shadow
user definitions with the same name. Python extensions are executable code and
must be reviewed; trust and enforcement behavior varies by extension surface.

## Tools

Project/user tool files are scanned below `tools/`. A normal project plugin
must expose a `TOOLS` list of lauren-ai `@tool()` callables or no-argument
tool classes. The complete authoring journey, registry precedence, security
boundary, and testing contract are in the [user-defined tools guide](tools.md).

`Tool` and `ToolBase` are lower-level contracts for explicit
`AgenthiccToolExecutor` registration; implementing one does not by itself
make an object discoverable from a `TOOLS` export.

Use capability metadata, explicit `WorkspaceView`/`NetworkGuard` checks when
available, the shared HTTP client, and bounded outputs. Project tool files are
executable Python and the normal tool scanner currently does not show the
repository's trust prompt, so review them before starting a session. See the
[security guide](security.md).

## Agents

Agent plugins live below `agents/`. Subclass `AgentPlugin`, set a non-empty
`name`, use lauren-ai's `@agent` metadata, and optionally set
`allowed_capabilities` or `replaces`. Export `AGENTS` for explicit discovery or
let the registry scan subclasses.

Built-in roles include `planner`, `executor`, `reviewer`, `explorer`,
`verifier`, `human`, and `auto`.

## Modes

Mode plugins define runtime labels, system patches, filters, approvals, and
workflow mappings. Keep destructive capabilities blocked by default in new
modes and test mode cycling, direct selection, and workflow availability.

## Workflows

Workflow plugins subclass `WorkflowPlugin` and expose `PhaseSpec` values. See
the [workflow guide](workflows.md) for phase transitions, parameters, and
resume rules.

## Skills

Skills are directories with `SKILL.md` metadata and body text. They can add
tools, prompt instructions, and slash-command behaviour. The loader supports
project and user scopes; the project copy wins. Discovery is deterministic and
returns diagnostics for malformed metadata, missing files, scope overrides,
and alias conflicts.

Use a lower-case kebab-case directory name (up to 64 characters), for example
`.agenthicc/skills/review-code/SKILL.md`. The frontmatter `name` is the display
name; the directory name is the canonical slash command. The supported
frontmatter includes:

```yaml
---
name: Review Code
description: Review implementation changes
aliases: [review]
suggestedTopics: [review, code quality]
allowedAgents: [planner, reviewer]
deniedAgents: [executor]
---
```

Legacy snake_case keys such as `suggested_topics`, `allowed_agents`, and
`max_turn_depth` remain readable and produce compatibility diagnostics. A
legacy directory name is normalized to its canonical command name and remains
available as an alias. Invoke either the canonical command or an alias, for
example `/review-code` or `/review`.

Per-agent configuration can further restrict skills. An omitted allowlist means
all skills allowed by the skill itself; deny rules always win:

```toml
[agents.planner]
allowed_skills = ["review-code"]
denied_skills = ["deploy"]
```

The same policy may be written as `[agents.planner.skills]` with `allow` and
`deny` lists. `/skills`, `/skills reload`, slash invocation, and automatic topic
matching all apply both frontmatter and per-agent restrictions. `/skills reload`
rescans the project and user skill directories in the current TUI session,
refreshes skill-owned slash commands and aliases, and preserves all built-in
and project command registrations. It reports added/removed skills and any
non-informational discovery diagnostics; a failed scan leaves the current
session unchanged.

## Commands

Command plugins expose `COMMANDS` and are loaded from command directories. A
command needs a name, description, optional argument hint, and handler or an
explicit session interceptor. `/workflow` and `/compact` demonstrate the
session-stateful case.

The default `/create-tools <instructions>` and `/create-commands <instructions>`
skills guide the agent to author these extensions using the existing lauren-ai
tool convention and unified command registry. They do not establish trust or
replace review of executable project code, and they do not replace capability,
approval, testing, or documentation work.

## MCP servers

Configure MCP servers with `[[tools.mcp_servers]]`. The bridge supports stdio,
WebSocket, and streamable HTTP forms, reconnects according to config, discovers
schemas, and exposes namespaced tools. Treat remote MCP servers as external
code execution and protect tokens with environment expansion and trust policy.

## Discovery debugging

1. Run `uv run agenthicc config show`.
2. Verify the directory name and file suffix.
3. Check startup warnings for syntax/import/missing-dependency failures.
4. Run `/commands`, `/skills`, `/skills reload`, or `/mcp` inside the TUI;
   `/skills` shows only skills permitted for the active agent, and the reload
   subcommand applies newly added or edited skill files without restarting.
5. For structured diagnostics, call
   `discover_skills_with_diagnostics(project_dir=..., user_dir=...)`.
6. Test the loader directly in a focused unit/integration test.

The long-term extension SDK, generated catalog, and unified trust contract are
PRD-138 P1.6/P2.4 work.
