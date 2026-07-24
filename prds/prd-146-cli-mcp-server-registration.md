---
title: "PRD-146: CLI MCP Server Registration"
status: Implemented
version: 1.0.0
created: 2026-07-24
related_prds:
  - PRD-138  # Repository improvement roadmap
  - PRD-139  # Product expansion
tags:
  - cli
  - mcp
  - configuration
  - security
---

# PRD-146 — CLI MCP Server Registration

## Summary

Provide a non-interactive command for registering an MCP server in the
configuration format already consumed by the MCP bridge:

```text
agenthicc mcp add NAME URL [--project | --global]
```

Project scope is the default and writes `.agenthicc/agenthicc.toml`. `--global`
writes the user configuration; `--project` makes project scope explicit. The
command must remain a configuration operation and must not start a server.

## Requirements

- Append a `[[tools.mcp_servers]]` stanza without replacing unrelated TOML.
- Validate server names, non-empty URLs/stdio commands, supported transports,
  reconnect settings, and duplicate names before writing.
- Accept `--transport`, `--no-auto-connect`, `--reconnect-attempts`, and
  `--reconnect-delay-seconds` for fields supported by `McpServerConfig`.
- Accept `--token-env ENV_VAR` and persist `${ENV_VAR}`; raw token values are
  not accepted, printed, or written by the command.
- Reject malformed existing TOML without modifying it.
- Preserve existing file permissions and use restrictive `0600` permissions for
  newly created configuration files.
- Use the existing `McpServerConfig`/`[[tools.mcp_servers]]` contract rather
  than creating a second registry or persistence format.

## Acceptance and verification

- `agenthicc mcp add --help` exposes the command and all supported options.
- Project and user-global additions load through `load_config()` as
  `McpServerConfig` instances.
- Duplicate, malformed, invalid-scope, invalid-transport, invalid-token-env,
  and negative-reconnect inputs fail without changing the target file.
- Unit coverage lives in `tests/unit/test_mcp_cli.py`.
- Run the repository lint, typing, type-audit, and full test commands recorded
  in `AGENTS.md` before release.

## Security and rollout

The command never handles raw bearer tokens; operators provide them through
the existing `${ENV_VAR}` expansion mechanism. Remote MCP endpoints remain
subject to the bridge's network and trust policies. The feature is additive:
existing hand-written `[[tools.mcp_servers]]` configuration continues to work.
