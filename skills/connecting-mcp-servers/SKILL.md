---
name: connecting-mcp-servers
version: 1.0.0
tags: [mcp, integrations, tools, stdio, oauth]
description: >-
  Connect MCP servers to agenthicc: the mcp CLI subcommands and their flags, the
  [[tools.mcp_servers]] TOML schema, transport and auth options, session
  lifecycle, and how to diagnose a server that will not connect.
---

# Skill: Connecting MCP Servers

MCP (Model Context Protocol) servers extend the session with external tools.
agenthicc connects to them, imports their advertised tool catalog, and exposes
those tools to chat, Plan mode, workflows, and subagents.

## When to use this skill

Use this skill when you need to:

- Add, inspect, or remove an MCP server
- Write the TOML by hand instead of using the CLI
- Choose a transport and configure authentication
- Understand whether a failure will block other tools
- Diagnose a server that connects but exposes no tools

Do **not** use this skill for writing ordinary tools — those are Python
`@tool()` plugins. For skill and workflow discovery, see the sibling skills.

---

## The `mcp` CLI

`agenthicc mcp` has ten subcommands
(`src/agenthicc/cli/commands/mcp.py`):

| Subcommand | Purpose |
|---|---|
| `add` | Add a server to configuration (`cli/commands/mcp.py:20`) |
| `list` | List configured servers (`cli/commands/mcp.py:81`) |
| `get` | Show one configured server (`cli/commands/mcp.py:111`) |
| `remove` | Remove one server (`cli/commands/mcp.py:127`) |
| `connect` | Connect and inspect the catalog (`cli/commands/mcp.py:178`) |
| `disconnect` | Disconnect in the current process (`cli/commands/mcp.py:192`) |
| `refresh` | Refresh one server's tool catalog (`cli/commands/mcp.py:200`) |
| `doctor` | Validate and diagnose connectivity (`cli/commands/mcp.py:215`) |
| `auth` | Check configured authentication (`cli/commands/mcp.py:231`) |
| `logout` | Remove stored authentication (`cli/commands/mcp.py:252`) |

### Adding a server

```bash
agenthicc mcp add context7 'python -m my_mcp_server' --project
agenthicc mcp add docs 'https://example.com/mcp' --transport streamable_http
```

`mcp_add` (`cli/commands/mcp.py:21-33`) accepts:

| Flag | Default | Effect |
|---|---|---|
| `--global` | off | Write to the user config |
| `--project` | off | Write to the project config |
| `--transport` | `stdio` | One of `stdio`, `ws`, `websocket`, `streamable`, `http` |
| `--token-env` | `""` | Name of the environment variable holding a bearer token |
| `--no-auto-connect` | off | Store the server without connecting at startup |
| `--reconnect-attempts` | `3` | Reconnect attempts |
| `--reconnect-delay-seconds` | `1.0` | Delay between attempts |

For a Lauren MCP directory or `server.py`, pass that path to `mcp add`: it is
normalized into an `lmcp run server.py --stdio` launcher
(`src/agenthicc/cli/mcp_config.py:179`). A directory that does not contain a
`server.py` raises `McpConfigError` rather than silently doing nothing. A
`stdio` server is launched **without a shell** — the command is split with
`shlex` and executed directly (`src/agenthicc/tools/mcp.py:168-172`), so shell
metacharacters are not interpreted. That matters when writing a stdio `url`:
`foo | bar` is not a pipeline, it is one argument.

### Validation the CLI enforces

`_validate` (`src/agenthicc/cli/mcp_config.py:107`) rejects:

- a name that does not start with a letter or number, or contains anything other
  than letters, numbers, `.`, `_`, `-` (`cli/mcp_config.py:25`)
- an empty URL or stdio command
- a transport outside the accepted set (`cli/mcp_config.py:27`)
- a `--token-env` that is not an uppercase environment variable name
  (`cli/mcp_config.py:26`)
- negative reconnect attempts, or a non-finite/negative reconnect delay

Each violation raises `McpConfigError`, which the CLI catches and prints as
`error: ...` rather than a traceback (`cli/commands/mcp.py:44-46`).

---

## The TOML schema

The `mcp add` subcommand writes one `[[tools.mcp_servers]]` stanza. Writing it by
hand is equivalent:

```toml
[[tools.mcp_servers]]
name = "context7"
transport = "stdio"
url = "python -m my_mcp_server"
auto_connect = true
```

A remote server with authentication:

```toml
[[tools.mcp_servers]]
name = "docs"
transport = "streamable_http"
url = "https://example.com/mcp"
token = "${DOCS_MCP_TOKEN}"
env_headers = { "X-Tenant" = "DOCS_TENANT_ID" }
required = false
tool_timeout_s = 60.0
```

The stanza maps onto `McpServerConfig` (`src/agenthicc/tools/mcp.py:99`):

| Field | Default | Notes |
|---|---|---|
| `name` | required | Must match the CLI name rule |
| `url` | `""` | A stdio command string or a remote URL |
| `transport` | `"stdio"` | See the alias table below |
| `token` | `""` | Bearer token; supports `${ENV_VAR}` expansion |
| `command` | `()` | Tuple form; a plain string is split with `shlex` |
| `cwd` | `""` | Working directory for a stdio server |
| `env` / `env_vars` | `{}` / `()` | Literal environment / names to forward |
| `headers` / `env_headers` | `{}` / `{}` | Literal headers / value-from-env headers |
| `enabled` | `True` | Set `false` to keep the stanza but skip the server |
| `required` | `False` | Set `true` to fail closed on startup |
| `auto_connect` | `True` | Connect during startup |
| `reconnect_attempts` | `3` | |
| `reconnect_delay_seconds` | `1.0` | |
| `startup_timeout_s` | `10.0` | |
| `tool_timeout_s` | `60.0` | |
| `enabled_tools` / `disabled_tools` | `()` | Per-server tool filters |
| `default_approval_mode` | `"prompt"` | `tool_approval` overrides per tool |
| `oauth` | `None` | Mapping or boolean |
| `metadata` | `{}` | Free-form |

Two compatibility aliases are accepted (`tools/mcp.py:158-160`):
`reconnect_delay_s` for `reconnect_delay_seconds`, and `timeout_s` for
`tool_timeout_s`. With `strict=True`, an unknown field raises
`McpConfigurationError` (`tools/mcp.py:298`) instead of being silently ignored.

### Transport aliases

The CLI and the config layer accept slightly different spellings, and this is a
real source of confusion:

| You may write | Normalized to |
|---|---|
| `stdio` | `stdio` |
| `streamable` | `streamable_http` |
| `http` | `streamable_http` |
| `websocket` | `ws` |
| `streamable_http` | `streamable_http` |
| `ws` | `ws` |
| `sse` | `sse` |

The alias table is `_TRANSPORT_ALIASES` (`cli/commands/mcp.py:33-37`) and the runtime set of
supported transports is `_SUPPORTED_TRANSPORTS = {"stdio", "streamable_http",
"sse", "ws"}` (`tools/mcp.py:38`). The CLI's accepted set is
`{"stdio", "ws", "websocket", "streamable", "http"}` (`cli/mcp_config.py:27`), so
`sse` and `streamable_http` are valid in TOML but are not offered by
`--transport`.

---

## Session lifecycle

`McpSessionManager` (`src/agenthicc/tools/mcp_manager.py:238`) owns server
connections for the life of a session. Its observable state is
`McpServerState` (`tools/mcp_manager.py:90`) and its status snapshot type is
`McpServerStatus` (`tools/mcp_manager.py:108`), with a catalog snapshot at
`tools/mcp_manager.py:138`.

Configuration is read at session startup. After editing it you must restart the
session, or use `/mcp reload` when the server is already registered.

### In-session commands

`/mcp` accepts (`src/agenthicc/commands/builtins.py:975`, dispatch at `:168`):

```text
/mcp [status|reload|connect NAME|disconnect NAME|refresh NAME|doctor [NAME]]
```

`status` and `list` are treated as the same read-only action (`commands/builtins.py:174`).
An unrecognised action, or an action missing its required `NAME`, prints the
usage line above instead of guessing (`commands/builtins.py:184-197`). Actions run as a
background asyncio task and report `/mcp <action> scheduled` immediately
(`commands/builtins.py:218-219`), so a slow server does not block the composer.

### Required versus optional servers

This is the single most important behavioural choice. An optional server can
fail to start without taking healthy servers or local tools down with it. Setting
`required = true` makes startup fail closed, and the failure surfaces as
`McpRequiredServerError` (`tools/mcp_manager.py:224`). Reserve `required = true` for a
server the workflow genuinely cannot proceed without — a workflow can also
declare a readiness dependency on the `mcp` phase instead, which is the more
granular mechanism.

### Tool identity and naming

agenthicc keeps MCP tools under the canonical ``mcp:<server>:<tool>`` identity,
represented by `AgenthiccMcpTool` (`tools/mcp.py:347`). Provider tool schemas are
stricter than that, so the bridge rewrites names into a provider-safe form
(`tools/mcp.py:41`) while preserving the canonical identity internally.
`McpToolRegistry` (`tools/mcp.py:788`) holds the bridged catalog, and `McpToolBridge`
(`tools/mcp.py:451`) performs the calls.

A stale catalog produces `McpStaleCatalogError` (`tools/mcp.py:338`), a subclass of
`McpToolCallError` (`tools/mcp.py:334`). Seeing that error means the server's tool set
changed after import; refresh it.

---

## Diagnosing a connection

Work from cheapest to most invasive:

```bash
agenthicc mcp list              # what is configured, with secrets redacted
agenthicc mcp get context7      # one stanza in full
agenthicc mcp doctor context7   # validate and diagnose connectivity
agenthicc mcp connect context7  # connect and dump the imported catalog
```

`mcp list` prints redacted output (`cli/commands/mcp.py:68-79`), and
`McpServerConfig._redacted_url` (`tools/mcp.py:307`) exists so a token embedded in a
URL is never echoed. `_looks_secret` (`tools/mcp.py:302`) identifies secret-looking
field names.

If the CLI reports an auth-shaped problem, `agenthicc mcp auth NAME` checks
configured authentication and `agenthicc mcp logout NAME` clears stored state.
Internally `_is_auth_error` (`tools/mcp_manager.py:1196`) classifies error text.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `error: server name must start with a letter or number...` | Invalid name | Use letters, numbers, `.`, `_`, `-`; start alphanumeric |
| `error: unsupported transport 'x'` | Typo or an unoffered spelling | Use `stdio`, `ws`, `websocket`, `streamable`, or `http` |
| `error: --token-env must be an uppercase environment variable name` | Lowercase or symbol in the env var name | Use e.g. `DOCS_MCP_TOKEN` |
| Server connects but exposes zero tools | Server advertised an empty catalog, or all tools are filtered | Check `enabled_tools`/`disabled_tools`, then `/mcp refresh NAME` |
| `McpStaleCatalogError` on a tool call | The server's catalog changed after import | Refresh the server |
| Edits to TOML have no effect | Configuration is read at session startup | Restart the session, or `/mcp reload` |
| One bad server blocks everything | `required = true` | Set `required = false` unless the workflow truly needs it |
| A command with pipes or `&&` does not work | stdio commands are launched without a shell | Wrap it in an explicit `sh -c` yourself if you really need shell semantics |
| `/mcp ...` prints a usage line | Unknown action or missing `NAME` | Follow the usage string exactly |

---

## Key points

- Ten subcommands: `add`, `list`, `get`, `remove`, `connect`, `disconnect`,
  `refresh`, `doctor`, `auth`, `logout`.
- Configuration lives in `[[tools.mcp_servers]]` stanzas mapping onto
  `McpServerConfig` (`tools/mcp.py:99`).
- Transports normalize through `_TRANSPORT_ALIASES` (`cli/commands/mcp.py:33`); `sse` and
  `streamable_http` are valid in TOML but are not `--transport` choices.
- stdio commands run without a shell.
- Secrets are redacted in `mcp list`; prefer `token = "${ENV_VAR}"` over literals.
- `required = true` fails startup closed; the default keeps failures contained.
- MCP tools are session-scoped and shared by chat, Plan mode, workflows, and
  subagents.
- Configuration is read at startup, so restart or `/mcp reload` after edits.
- `/mcp` actions are scheduled asynchronously and report a `scheduled` line.
- See `docs/guides/mcp.md` for the prose guide.
