# Connecting MCP servers

AgentHICC can discover tools from Model Context Protocol (MCP) servers and
make them available to normal chat, Plan mode, workflows, subagents, and
headless runs. MCP support is optional; install it only when the project uses
an MCP server.

## Install MCP support

For an installed AgentHICC package:

```bash
uv pip install 'agenthicc[mcp]'
```

For an AgentHICC checkout:

```bash
uv sync --extra mcp
```

The extra provides the MCP client and the HTTP, SSE, and WebSocket transport
adapters. The base installation remains usable without it.

## Add a local stdio server

Register a server command in the current project's configuration:

```bash
agenthicc mcp add local-tools "python -m my_mcp_server" --project
```

Stdio commands are tokenized into an argument vector and are never executed
through a shell. A local Lauren MCP application can be registered by pointing
at its `server.py` file or a directory containing that file:

```bash
agenthicc mcp add local-tools /path/to/server.py --project
```

AgentHICC converts that path into an `lmcp run ... --stdio` launcher. The
directory must contain `server.py`.

## Add a remote server

Streamable HTTP is the preferred remote transport:

```bash
export MCP_TOKEN='replace-me'
agenthicc mcp add remote-tools 'https://mcp.example.test/mcp' \
  --transport streamable \
  --token-env MCP_TOKEN \
  --project
```

`--global` writes to the user configuration instead of the project
configuration. `--token-env` stores only the environment-variable name; the
token itself is never written to TOML or printed by diagnostics.

The equivalent TOML is:

```toml
[[tools.mcp_servers]]
name = "remote-tools"
transport = "streamable_http"
url = "https://mcp.example.test/mcp"
auto_connect = true
startup_timeout_s = 10
tool_timeout_s = 60

[tools.mcp_servers.env_headers]
Authorization = "MCP_TOKEN"
```

Each server has an independent `env_headers` mapping. Different MCP servers
can therefore use different environment variables for the same header:

```toml
[[tools.mcp_servers]]
name = "github-tools"
transport = "streamable_http"
url = "https://github.example.test/mcp"

[tools.mcp_servers.env_headers]
Authorization = "GITHUB_MCP_TOKEN"

[[tools.mcp_servers]]
name = "linear-tools"
transport = "streamable_http"
url = "https://linear.example.test/mcp"

[tools.mcp_servers.env_headers]
Authorization = "LINEAR_MCP_TOKEN"
```

Provide both variables before launching AgentHICC:

```bash
export GITHUB_MCP_TOKEN='github-secret'
export LINEAR_MCP_TOKEN='linear-secret'
agenthicc
```

The mappings are resolved per server, so there is no collision between the
two `Authorization` headers. Different header names are supported as well,
for example `X-API-Key = "SEARCH_API_KEY"`. If an environment variable is
missing, its header is omitted and the server may reject the connection. The
alternative `token = "${MCP_TOKEN}"` setting creates an
`Authorization: Bearer ...` header automatically.

Other supported transport spellings include `ws`, `websocket`, `sse`,
`streamable`, and `http`; `streamable_http` is the canonical HTTP spelling.

## Connect and diagnose

`auto_connect = true` is the default, so a normal launch connects configured
servers and adds their advertised tools to the session:

```bash
agenthicc
```

Inspect configuration and run bounded diagnostics from the shell:

```bash
agenthicc mcp list
agenthicc mcp doctor remote-tools
agenthicc mcp connect remote-tools
```

`mcp connect` is a short-lived diagnostic connection. It does not keep a
server alive after the command exits.

Inside a running TUI, use the session-owned MCP manager:

```text
/mcp
/mcp connect remote-tools
/mcp refresh remote-tools
/mcp disconnect remote-tools
/mcp reload
```

`/mcp` shows server state, catalog revision, tool count, and redacted
failures. `/mcp refresh NAME` refreshes only that server's tool catalog.
`/mcp reload` disconnects and reconnects every enabled configured server in the
current session, including servers whose startup setting is
`auto_connect = false`. It republishes each server's current tool catalog and
isolates optional-server failures. It does not reread arbitrary configuration
files or expose credentials; restart the session after adding a new server so
the new server is registered first.

Agents can call the read-only `mcp_connection_guide` tool for a copy-ready
transport-specific setup, the correct project/global configuration path, and
the session commands. The tool does not write files or resolve secrets.

## Restrict or troubleshoot tools

An MCP server can be enabled without connecting automatically, restricted to a
tool allowlist, or filtered with a denylist:

```toml
[[tools.mcp_servers]]
name = "local-tools"
transport = "stdio"
command = ["python", "-m", "my_mcp_server"]
auto_connect = false
required = false
enabled_tools = ["search", "read_document"]
disabled_tools = ["delete_document"]
```

Use `agenthicc mcp doctor NAME --json` for machine-readable diagnostics. An
optional server failure is surfaced without preventing healthy MCP servers
from loading; set `required = true` when startup must fail if that server
cannot connect. Check network policy for remote URLs and ensure the MCP extra
is installed in the same environment that runs `agenthicc`.

### Startup generations and failure isolation

Each startup, explicit connect, and reload operation has a monotonic lifecycle
generation. Server status and lifecycle events include the generation and a
stable redacted event ID. This prevents a delayed process or network response
from an older `/mcp reload` from overwriting a newer catalog.

Servers are independent startup fault domains. If an optional server such as
`asyncmove` cannot start, its failure is recorded once for that generation and
the session continues with healthy MCP and built-in tools. A required server
still fails closed, but the manager waits for the other eligible servers to
settle and preserves their status for diagnosis. Repeating `/mcp reload` or
redrawing the transcript does not repeat the same generation's failure notice;
an explicit new connect/reload generation may produce a new notice.

## Try it

The server registry is a plain TOML-backed list, so listing is read-only and
safe to run anywhere:

```bash
PYTHONPATH=src python -m agenthicc mcp list --json
```

```text
{"path": "/root/python_projects/agenthicc/.agenthicc/agenthicc.toml", "servers": []}
```

`path` names the exact file the command read, which is the single most useful
field when a server you added "does not exist": you are usually editing a
different project root or have not passed `--global`/`--project`.

Adding a server is validated before anything is written:

```bash
PYTHONPATH=src python -m agenthicc mcp add --help
```

```text
usage: agenthicc mcp add [-h] [--global] [--project] [--transport TRANSPORT]
                         [--token-env TOKEN_ENV]
                         [--reconnect-attempts RECONNECT_ATTEMPTS]
                         [--reconnect-delay-seconds RECONNECT_DELAY_SECONDS]
                         [--no-auto-connect]
                         SOURCE
```

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| A server you added is missing from `mcp list` | You wrote it to a different scope or project root | Check the `path` field in `mcp list --json`; pass `--global` or `--project` explicitly to `mcp add` |
| `--transport ws` is rejected at runtime though the CLI accepted it | The two layers use different vocabularies | The CLI accepts `stdio`, `ws`, `websocket`, `streamable`, `http`; the runtime accepts `stdio`, `streamable_http`, `sse`, `ws`. Use a runtime spelling such as `streamable_http` |
| A server connects but exposes no tools | The handshake succeeded and the catalogue is empty or still loading | Run `mcp doctor [NAME]`; the catalogue starts in the background and an operation that declares a required MCP dependency waits for it |
| `mcp add` fails with a validation error and nothing is written | CLI validation runs before persistence | Read the error: the source or transport is invalid. This is fail-closed by design |
| A token appears in a config file | A literal token was passed instead of an environment reference | Use `--token-env ENV_VAR`; credentials belong in the environment, never in TOML |
| Reconnects hammer a failing server | Default retry policy is too eager for your server | Set `--reconnect-attempts` and `--reconnect-delay-seconds` on `mcp add` |
| A server connects immediately when you did not want it to | Auto-connect is on by default | Pass `--no-auto-connect` and drive the lifecycle with `mcp connect`/`mcp disconnect` |
| Session start is delayed by every configured server | Optional connections are started in the background, but a declared required dependency is awaited | Make the dependency optional, or accept the wait; required resources keep fail-closed semantics |
| `agenthicc doctor` is not a command | Diagnostics are subcommand-scoped | Use `mcp doctor [NAME]`, not a top-level `doctor` |
| A previously working server fails after an upgrade | Transport alias or dependency drift | `mcp doctor --json` first, then `mcp refresh`, then `mcp logout`/`mcp auth` if the credential expired |

### The transport-alias trap

This is the single most common MCP misconfiguration, because the CLI and the
runtime deliberately accept different spelling sets. `agenthicc mcp add
--transport ws ...` can be accepted and stored, and then the runtime rejects
`ws` at connect time — or vice versa. When diagnosing, verify which layer
produced the error before editing the config.

### Diagnostic ladder

Work outward, one step at a time, and stop at the first failure:

```bash
PYTHONPATH=src python -m agenthicc mcp list --json      # is it configured, and where?
PYTHONPATH=src python -m agenthicc mcp doctor --json    # dependency/transport health
PYTHONPATH=src python -m agenthicc mcp refresh NAME     # re-read the catalogue
```

Only after those pass should you suspect the tool surface; a *connection*
problem cannot be fixed by editing a tool allow-list.

### Deny by default

An unavailable MCP server does not fall back to a local implementation. The
choice is fail-closed: the dependent operation reports the missing required
resource rather than silently proceeding without the tools it declared.
