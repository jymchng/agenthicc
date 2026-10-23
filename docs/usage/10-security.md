# Security

agenthicc's safety model is **capability-based**, not severity-based. There is
no Low/Medium/High/Critical rating. A tool declares which capabilities it
needs, and the active mode plus the workspace boundary decide whether the call
proceeds, prompts, or is blocked.

## Capabilities

`ToolCapability` (`src/agenthicc/tools/capabilities.py`) has **nine** members:

```text
CONTROL  EXECUTE  GIT_READ  GIT_WRITE  NETWORK  READ  SEARCH  UNDECLARED  WRITE
```

| Capability | Covers |
|---|---|
| `READ` | Reading file content and metadata |
| `SEARCH` | Finding things without returning full content |
| `GIT_READ` | Read-only git inspection |
| `GIT_WRITE` | Git mutations (add, commit, checkout, stash) |
| `WRITE` | Creating, editing, moving, deleting files |
| `EXECUTE` | Running commands and subprocesses |
| `NETWORK` | Reaching a network destination |
| `CONTROL` | Driving session/terminal lifecycle |
| `UNDECLARED` | The marker for a tool that declares nothing |

!!! warning "`UNDECLARED` is the important one"
    A tool with no capability decorator does **not** default to safe. It lands
    in `UNDECLARED`, which prompts in Safe and is blocked in Plan. Treat a
    missing decorator as a defect to fix, not a harmless default.

## How modes gate capabilities

The restricted set is `{WRITE, GIT_WRITE, EXECUTE, NETWORK, UNDECLARED}`:

| Mode | Restricted set |
|---|---|
| **Safe** | Prompts before each call |
| **Plan** | Hard-blocks — approval cannot override |
| **Yolo** | Allowed without prompting |

Blocked capabilities are read **live on every tool call**, so a mode change
takes effect from the next call even inside a single turn. See
[Modes](05-modes.md) for the cycle and aliases.

## `--dangerously-skip-permissions`

```bash
agenthicc --dangerously-skip-permissions
```

Auto-approves ordinary capability prompts for the session. Two limits worth
stating precisely:

- It does **not** turn Safe into Yolo, and it does not bypass an
  outside-workspace approval.
- Plan mode still hard-blocks side effects.

It is intentionally not settable in `agenthicc.toml`; it exists as a
per-invocation escape hatch.

## Boundaries

Security is layered, and an allow decision at one layer does not bypass a
stricter one:

- **Path boundary** — `WorkspaceView` is the final check, behind the
  mode-aware `WorkspaceScope`/`WorkspaceAccessPolicy`. All modes reject `..`
  traversal, absolute paths outside the workspace, and symlinks that resolve
  outside it, and the canonical target is revalidated immediately before I/O.
- **Network boundary** — `NetworkGuard` permits exact hostnames and their
  subdomains; an empty `network_allow_list` denies outbound destinations.
- **Capability boundary** — the mode's restricted set above.
- **Agent scope** — `AgentCapabilityScope` denies explicit patterns first,
  then applies an allow set, call budget, and spawn-depth ceiling. Child scopes
  are intersections and can never expand a parent.

Configure real project paths in `[security].allowed_paths`. The illustrative
`/workspace` default may not match your checkout.

## Extension trust

A repository's Python extensions — `.agenthicc/tools/`, `commands/`,
`workflows/`, `modes/`, `agents/`, `cli/` and `cli.toml` — are imported only
after you trust their exact content. A trust record binds a file's path to the
SHA-256 of its bytes and lives in `~/.agenthicc/trust.json`, never in the
repository; editing the file revokes it. Untrusted files are logged and
skipped, so opening an unfamiliar repository runs none of its code.

```bash
agenthicc trust list      # what this repository would run, and its status
agenthicc trust add       # trust the reviewed project extensions
agenthicc trust revoke .agenthicc/tools
```

!!! danger "Trust is not a sandbox"
    Trusting a file records that you reviewed it; once imported it runs with
    your permissions. Review before you trust, and re-review after every
    change that revokes trust.

Skill `` !`command` `` placeholders are never executed when a skill loads: the
agent is asked to run each command through its command tool, so approvals, the
active mode and the sandbox apply. Agenthicc never installs plugin
dependencies; a plugin whose declared requirements are missing is skipped.

## Next

- [Tools](09-tools.md)
- [Modes](05-modes.md)
- [Security model](../guides/security.md) — full layer-by-layer reference
