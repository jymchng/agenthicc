# Tools

agenthicc provides class-based tools with capability metadata, approvals,
path and network guards, shared HTTP safety helpers, and an optional
CloakBrowser adapter.

## Tool categories

| Category | Tools |
|---|---|
| **Filesystem** | read, write, edit, list, search, glob, tree, diff, file metadata |
| **Git** | status, diff, log, show, blame, branch, add, commit, push, stash |
| **Command** | shell/command execution with guards and timeouts |
| **MCP** | tools contributed by connected MCP servers |
| **Dynamic** | runtime-created scripted tools |

## Safety contracts

- **Capability** — each tool declares capabilities; the active mode's policy
  gates them.
- **Path** — path traversal and network guards on file/URL tools.
- **Approval** — risk levels route through the approval system.
- **Timeout/retry** — HTTP safety helpers with timeouts and retries.
- **Bounded output** — tool results are bounded so the transcript never
  floods.

## MCP tools

Connect MCP servers via `agenthicc mcp` (or the `/mcp` TUI command); their
tools join the same registry and inherit the same approval/path/capability
gates. See [MCP guide](../guides/mcp.md).

## Custom tools

Tools are class-based: implement the tool contract with input/output schema,
capability metadata, and risk level, then register it. Plugin loaders can add
tools at runtime.

## Related

- [Security →](10-security.md)
- [MCP guide](../guides/mcp.md)
