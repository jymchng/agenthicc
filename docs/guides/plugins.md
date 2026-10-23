# Extensions

Extensions are discovered from project-local `.agenthicc/` directories and
user-global `~/.agenthicc/` directories. Project definitions generally shadow
user definitions with the same name. Python extensions are executable code, so
a project extension is imported only after you have reviewed and trusted its
exact content — see [Trust and dependencies](#trust-and-dependencies).

## Trust and dependencies

Every loader — tools, slash commands, workflows, modes, agents and CLI plugins
— asks one trust service before it imports a file:

- files under your own `~/.agenthicc/` are trusted by location;
- any other file, including everything under a repository's `.agenthicc/`,
  needs a trust record that binds its resolved path to the SHA-256 of its
  current bytes. Editing the file revokes its trust.

An untrusted file is logged — with the command that trusts it — and skipped; it
is never imported, so opening an untrusted repository runs none of its code.
Records live in `~/.agenthicc/trust.json`, never in the repository, which could
otherwise forge its own trust. Review the files, then manage trust with:

```bash
agenthicc trust list            # every project extension and its status
agenthicc trust add             # trust every project extension's current content
agenthicc trust add .agenthicc/tools/deploy.py
agenthicc trust revoke .agenthicc/tools
```

`trust list` counts every Python file under the extension directories,
including private helpers a package imports, and `.agenthicc/cli.toml`, whose
commands run shell templates. In a repository without extensions it prints:

```text
No project extensions under .agenthicc/.

Trust store: /home/you/.agenthicc/trust.json (0 record(s))
```

A tool or command plugin declares the packages it needs with a module-level
literal `DEPENDENCIES = ["package>=1.0"]` or a `<stem>.requirements.txt` file
beside it. Agenthicc reads both without executing the plugin, never installs
them, and skips a plugin whose requirements are missing — so a trusted plugin is
imported exactly once, after its dependencies are known to be present.

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
executable Python: they load only once trusted, and trusting a file does not
sandbox it. See the [security guide](security.md).

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
tools, prompt instructions, and explicit trigger behaviour. The loader supports
project and user scopes; the project copy wins. Discovery is deterministic and
returns diagnostics for malformed metadata, missing files, scope overrides,
and alias conflicts.

Use the CLI to install one without manually creating the directory:

```text
agenthicc skills add https://example.com/skills/review-code/SKILL.md
agenthicc skills add https://github.com/acme/skills/tree/main/review-code --global
agenthicc skills add https://github.com/heygen-com/hyperframes.git --skill hyperframes
agenthicc skills add heygen-com/hyperframes --all
```

Project scope is the default; `--global` targets `~/.agenthicc/skills/`, while
`--project` makes the project target explicit. Local directories and files are
also accepted for offline use. A GitHub repository source is shallow-cloned,
searched in conventional skill containers such as `skills/` and
`.agents/skills/`, and deduplicated by canonical skill name. Repository skills
retain companion files such as `references/` and scripts. `--skill` accepts a
comma-separated selection; without it, repository sources install all valid
skills, matching the non-interactive behavior of `npx skills add`. The
installer validates the existing skill metadata contract, uses canonical
kebab-case directory names, stages each skill before moving it into place, and
refuses to overwrite an existing skill. A downloaded skill is still executable
prompt content: review its `SKILL.md` before using it.

Use a lower-case kebab-case directory name (up to 64 characters), for example
`.agenthicc/skills/review-code/SKILL.md`. The frontmatter `name` is the display
name; the directory name is the canonical skill trigger name. The supported
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
legacy directory name is normalized to its canonical skill name and remains
available as an alias. Invoke either the canonical skill or an alias, for
example `$review-code` or `$review`.

Per-agent configuration can further restrict skills. An omitted allowlist means
all skills allowed by the skill itself; deny rules always win:

```toml
[agents.planner]
allowed_skills = ["review-code"]
denied_skills = ["deploy"]
```

The same policy may be written as `[agents.planner.skills]` with `allow` and
`deny` lists. `/skills`, `/skills reload`, `$skill-name` invocation, and
automatic topic matching all apply both frontmatter and per-agent restrictions.
`/skills reload` rescans the project and user skill directories in the current
TUI session, refreshes skill-owned dollar triggers and aliases, and preserves
all built-in and project command registrations. It reports added/removed
skills and any non-informational discovery diagnostics; a failed scan leaves
the current session unchanged. The former `/skill-name` spelling is not
accepted.

## Commands

Command plugins expose `COMMAND` and/or `COMMANDS` and are loaded from command directories. A
command needs a slash-prefixed name, description, optional argument hint, and
handler or menu factory. See the [user-defined commands guide](commands.md)
for the actual TUI discovery path, picker behavior, dispatch context,
precedence, and reload behavior. `/workflow` and
`/compact` demonstrate the session-stateful case that requires a built-in
interceptor.

The default `/create-tools <instructions>` and `/create-commands <instructions>`
skills guide the agent to author these extensions using the existing lauren-ai
tool convention and unified command registry. They do not establish trust or
replace review of executable project code, and they do not replace capability,
approval, testing, or documentation work.

## MCP servers

Configure MCP servers with `[[tools.mcp_servers]]`. A session owns one
`McpSessionManager` shared by normal turns, workflows, subagents, and headless
runs. It starts independent servers concurrently, keeps optional failures
visible without blocking healthy servers, caches a deterministic tool catalog,
and refreshes one server when its tool list changes. Streamable HTTP is the
preferred remote transport; WebSocket and the old `streamable`/`http` spellings
remain readable as compatibility aliases.

New stdio entries should use an argv array and never a shell:

```toml
[[tools.mcp_servers]]
name = "local-tools"
transport = "stdio"
command = ["python", "-m", "my_mcp_server"]
cwd = "."
startup_timeout_s = 10
tool_timeout_s = 60
required = false
enabled_tools = ["read_file", "list_directory"]
disabled_tools = ["write_file"]

[tools.mcp_servers.env_headers]
Authorization = "MCP_TOKEN"
```

Legacy `url = "python -m my_mcp_server"` stdio entries continue to load through
the compatibility parser and are never passed to `shell=True`. Treat remote
MCP servers as external code execution and protect tokens with environment
expansion and trust policy. Structured MCP results are preserved as native JSON
values; JSON-encoded text results are decoded when possible, while ordinary
text results remain strings.

For a safe, repeatable registration flow, add a server through the CLI:

```text
agenthicc mcp add local-tools "python -m my_mcp_server"
agenthicc mcp add remote "https://mcp.example.test/server" \
  --transport streamable --token-env MCP_TOKEN --global
```

Project configuration is the default; `--global` targets the user config and
`--project` makes project scope explicit. The command appends the existing
`[[tools.mcp_servers]]` format, rejects duplicate names and malformed TOML,
preserves existing file permissions, and writes `${MCP_TOKEN}` rather than a
secret value. It validates the entry but does not connect to the server until
the normal MCP bridge starts. A local Lauren MCP example directory (or its
`server.py`) is converted to the `uv run --project ... lmcp run ... --stdio`
launcher.

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

For MCP lifecycle diagnostics use `agenthicc mcp list`, `get`, `doctor`,
`connect`, `refresh`, and `remove`. Inside a running TUI, `/mcp` shows server
state, catalog revision, tool count, and redacted failures; `/mcp connect NAME`,
`/mcp disconnect NAME`, and `/mcp refresh NAME` operate on the current
session-scoped manager. `mcp doctor` never invokes a tool merely to test
connectivity.

The unified, digest-bound trust contract is implemented (PRD-250 S-02); the
long-term extension SDK and generated catalog remain PRD-138 P1.6/P2.4 work.

## Try it

Check what a repository would run before you open a session in it:

```bash
uv run agenthicc trust list
```

Each project extension is listed with `trusted`, `not trusted` or `changed
since it was trusted`. The only `[plugins]` setting is `strict_cli_shadow`;
validate a configuration file with `uv run agenthicc config validate`.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| A project extension is never loaded | It is not trusted, discovery is deferred until after the first TUI frame, or `strict_cli_shadow` rejected it | Run `agenthicc trust list`; after trusting, run `/commands reload` or `/tools reload` once the shell phase is `ready`; check the CLI shadow diagnostic |
| An extension stopped loading after an edit or `git pull` | Its content no longer matches the trust record | Review the change, then `agenthicc trust add PATH`; a changed digest is the signal, so do not trust blindly |
| A plugin import fails silently | Failed imports are recorded rather than raised | Read the discovery result and its recorded failure; check the module name and any missing optional dependency |
| A plugin is skipped for missing dependencies | Its `DEPENDENCIES` or `<stem>.requirements.txt` names a package that is not installed | Install the package yourself; agenthicc never installs plugin dependencies |
| A plugin is rejected with `DEPENDENCIES must be a literal list` | The declaration is computed | Write a literal list of requirement strings; it is read without running the plugin |
| A CLI shadowing conflict appears | `strict_cli_shadow` is enforcing precedence | Rename the project command or relax the setting knowingly |
| An extension works locally but not in CI | Trust records are per user and never committed, so CI skips the untrusted file | Run `agenthicc trust add PATH` as the CI user for the reviewed files, or keep the CI job independent of the extension |

### Discovery debugging

Read the discovery result rather than the log tail. The result distinguishes a
module that failed to import, a dependency that is absent, and a trust decision
that was declined — three different fixes that all look like "the plugin did
not load" in a scrolling log.

### Trust is not a sandbox

Project-local tools, agents, modes, workflows and commands are Python code, so
loading them is code execution. A trust record captures a decision about code
you already read; it does not constrain what that code can reach once
imported. Review before trusting. Skills are prompt content: a skill's
`` !`command` `` context placeholders are never executed by the loader — the
agent is asked to run them through its command tool, where approvals, the
active mode and the sandbox apply.
