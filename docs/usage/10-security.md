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

## Plugin trust

`agenthicc trust cli` hashes and trusts the Python files under
`.agenthicc/cli/` for this project. It is deliberately narrow:

!!! danger "Trust is not a sandbox"
    Project-local tools, agents, modes, workflows, skills, and commands are
    Python code, so loading them is code execution. The normal
    `.agenthicc/tools/` discovery path currently imports project tool files
    **without** calling the trust helper or prompting. A trust manifest is not
    an automatic boundary for user-defined tools today. Review before use.

Keep plugin dependency auto-install disabled in unattended or headless
environments; the normal session tool scanner skips missing dependencies
rather than installing them.

## Next

- [Tools](09-tools.md)
- [Modes](05-modes.md)
- [Security model](../guides/security.md) — full layer-by-layer reference
