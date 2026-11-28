---
title: "MCP-05: A repository cannot start or feed an MCP server on its own"
status: Proposed
version: 1.0.0
date: 2026-11-28
priority: P1
related_prds:
  - MCP-00
  - MCP-02
  - MCP-03
  - MCP-06
  - prd-146
  - prd-172
  - WFC-00
---

# MCP-05 — A repository cannot start or feed an MCP server on its own

agenthicc already refuses to run a project's tools, agents, modes, commands,
workflows and status-line command until the operator trusts the file
(`agenthicc trust add`). MCP servers are the one extension kind that runs code or
carries credentials and has no such gate.

## Evidence

| # | What is true today | Where |
|---|---|---|
| 1 | A project's `.agenthicc/agenthicc.toml` is merged into the effective configuration, and `[[tools.mcp_servers]]` entries from it are registered without any trust decision. The only project-config value the loader strips is `[ui.statusline] command`. | `config.py:3031-3052`; `runners/tui_session.py:1416-1445` |
| 2 | The trust store checks a file's path and SHA-256 per `kind`: `tool`, `agent`, `mode`, `command`, `workflow`, `cli`, `statusline`. There is no `mcp` kind. | `plugins/trust.py` (`ExtensionTrust.check`); callers in `plugins/discovery.py:140`, `agents/registry.py:273`, `modes/plugin_loader.py:130`, `commands/plugin_loader.py:83`, `workflows/loader.py:84`, `cli/registry.py:342` |
| 3 | A stdio entry's `command` is executed when the session starts: `enabled` and `auto_connect` both default to `true`. Opening a cloned repository is enough. | `tools/mcp_server_config.py:48-50` |
| 4 | A remote entry may attach any environment variable to any URL: `env_headers = { Authorization = "GITHUB_TOKEN" }` plus a `url`, or `token = "${GITHUB_TOKEN}"`. A stdio entry may pass named variables through `env_vars`. | `tools/mcp_server_config.py:41-47`, `:176-190` |
| 5 | The network allow-list reaches the MCP manager only when `security.network_allow_list` is non-empty: `NetworkGuard(...) if cfg.security.network_allow_list else None`. With the default (empty), remote servers are unchecked. | `runners/tui_session.py:1433-1437` |
| 6 | Even with an allow-list, the guard runs once on the configured URL when the client is built. The Streamable HTTP client then follows redirects (`follow_redirects=True`, up to 20). | `tools/mcp.py:295-306`; `lauren-mcp` `_client/_streamable.py:99-111` |
| 7 | A stdio child has no filesystem or network confinement. agenthicc already has a fail-closed Bubblewrap launcher for worktree commands. | `worktrees/environment.py:860-891` |
| 8 | A catalog change is detected (`catalog_hash`, `revision`) and republished, but a changed tool description is accepted silently. | `tools/mcp_manager.py:944-964` |
| 9 | `agenthicc mcp add --project` writes the server into the repository, which is how a team shares one, and also how a hostile contribution would. | `cli/commands/mcp.py:16-60` |

Rows 1, 3 and 4 together mean a repository can run a program on the operator's
machine, or send a named secret to a host it chooses, at session start. This PRD
treats that as a vulnerability, not a missing feature.

## Goals

1. Opening a repository never starts an MCP server, or sends a credential, that the
   operator has not approved.
2. The operator sees exactly what will run or connect, and which secrets it will
   receive, before approving it.
3. An approved definition stays approved only while it is unchanged.
4. A stdio server can be confined, fail-closed, where the platform supports it.
5. A server that changes its tools after approval is noticed.

## Non-goals

- Judging whether a server is safe. Approval is the operator's decision.
- Sandboxing remote servers (they are not on the machine).
- Restricting user-global configuration: the user wrote it.

## Design

### D1 — Which configuration is trusted

| Source | Trusted |
|---|---|
| `~/.agenthicc/agenthicc.toml` (user-global) | yes |
| `--config FILE` and `--set` on the command line | yes |
| `.agenthicc/agenthicc.toml` in the project | **no, until approved** |
| An MCP server a trusted skill or workflow declares (MCP-07) | follows that artifact's trust |

### D2 — Approve a definition, not a file

A project config changes often (models, UI). File-level trust would be revoked by
every unrelated edit. Trust therefore records, per server:
`(config file, server name, SHA-256 of the canonical definition)`, where the
canonical definition is the redacted shape from `McpServerConfig.redacted()` plus the
**names** of referenced environment variables (never their values). Editing
`command`, `url`, `env_vars`, `env_headers`, `cwd`, `transport` or `sandbox`
revokes trust; editing `[ui]` does not. Records live in `trust.json` beside the
existing ones, under a new `mcp` kind.

### D3 — The approval prompt

`agenthicc mcp trust NAME` (CLI) and a one-time overlay in the TUI show:

```text
Project server "db" wants to run:
  command   npx -y @acme/db-mcp --readonly
  cwd       <workspace>
  receives  env: DATABASE_URL      (named variable; value not shown)
  sandbox   none
Trust once / always for this definition / no
```

A remote server shows its host, the header names, and the environment variables
that fill them. The prompt uses the existing approval overlay and, in a background
session, the operator inbox. **Headless and background runs never prompt for
trust:** an unapproved server is skipped with a notice that names the fix.

### D4 — State and notices

Add `McpServerState.UNTRUSTED`. `/mcp status` and the MCP-01 notice read
`MCP server "db" is waiting for your approval (project config) — run: agenthicc mcp
trust db`. A definition edited after approval reads `changed since it was trusted`
(the wording `ExtensionTrust` already uses).

### D5 — Network policy

- The allow-list check is applied on every connection and, once the client exposes
  its HTTP client or redirect policy (U9), on each redirect hop. Until then the
  adapter disables redirect following for project-defined servers where it can and
  documents the limit.
- A project-defined remote server that is trusted still sends header secrets only to
  the host recorded in the approval.
- With an empty allow-list, trust is the gate; with a non-empty one, both apply.

### D6 — Optional confinement for stdio

Per-server `sandbox = "none" | "bwrap"` (default `none`; many servers need the
network and the home directory). `bwrap` reuses the launcher in
`worktrees/environment.py` (extracted into a shared helper, not copied): read-only
system directories, the workspace bound read-write, no network unless
`sandbox_network = true`, and an empty environment apart from the declared one. If
`bwrap` is requested and missing, or the platform lacks it, the server **fails closed**
with `sandbox_unavailable`; it never starts unconfined.

### D7 — Catalog pinning

On approval the manager records the server's `catalog_hash` and tool names. When a
later publish changes the hash for an approved definition:

1. the notice lists tools added, removed and changed (name and the size of the
   description change, never the text);
2. in Safe mode a changed tool asks for approval again before its next call, as if
   new; in Yolo it is noted only;
3. `mcp trust NAME --refresh` accepts the new catalog.

## Failure handling

| Case | Result |
|---|---|
| Untrusted project server | `UNTRUSTED`, not started, nothing contacted |
| Approved, then edited | `UNTRUSTED` (changed), not started |
| `bwrap` requested, unavailable | `FAILED: sandbox_unavailable`, never unconfined |
| Redirect to an unlisted host | refused; the connection fails with the host named |
| Catalog hash changed | tools keep working in Yolo; Safe asks again for changed tools |

## Security

This PRD is the security work. Beyond the design: approval prompts show environment
variable **names** and never values; the canonical definition excludes secrets; the
trust store is user-owned (`~/.agenthicc/trust.json`) and a project file cannot write
to it; trust is per definition so an attacker cannot ride an old approval by changing
the command.

## Rollout and migration

1. Existing project-defined servers become `UNTRUSTED` on upgrade. That is a
   behaviour change: the changelog states it first, and `agenthicc mcp trust --all`
   lists and approves every current definition after review.
2. Ship D1–D4 together; D5–D7 follow.
3. Update `docs/guides/mcp.md` (a "Trust" section and the project-scope warning in
   `mcp add --project`), `docs/guides/security.md` if present, `llms-full.txt`,
   `CHANGELOG.md`.

## Acceptance criteria

All use the MCP-03 peer.

1. A project config naming a stdio server whose command writes a marker file does
   not create the file at startup; `/mcp status` shows `untrusted` and the command to
   run.
2. After `mcp trust NAME` it starts. Changing its `command` makes it `untrusted`
   again. Editing an unrelated `[ui]` key does not.
3. A project remote server with `env_headers = { Authorization = "TOKEN" }` makes no
   connection until approved; the approval text names `TOKEN` and not its value.
4. A user-global server and a `--config` server start without a prompt.
5. A peer that redirects to a second host is refused when that host is not allowed,
   and a header supplied through `env_headers` never reaches the second host.
6. With `sandbox = "bwrap"` a peer tool cannot read a file outside the bound paths;
   with `bwrap` absent the server is `sandbox_unavailable`.
7. In the `changing_catalog` scenario, a changed description produces the diff
   notice, and in Safe mode the next call to that tool asks first.

## Verification

Marker-file test (row 1); header-capture on the peer (row 5); a mutation that skips
the trust check must fail row 1; `bwrap` tests skip with a named reason where the
binary is absent (CI installs it, per `CLAUDE.md`).

## Risks

- Trust prompts add friction for teams that commit MCP servers. Per-definition trust
  keeps it to one prompt per definition, and `trust --all` handles migration.
- U9 (redirect policy) is a `lauren-mcp` change; until it lands D5 documents a gap.
