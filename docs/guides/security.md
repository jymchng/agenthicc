# Security model

agenthicc runs tools that can read and change a project. Security is layered;
an allow decision at one layer does not bypass a stricter layer.

## Path boundaries

`WorkspaceView` remains the low-level final path boundary. Session tools use the
mode-aware `WorkspaceScope`/`WorkspaceAccessPolicy` boundary before it and
revalidate the canonical target immediately before I/O. The shared resolver
records the requested path, canonical target, configured root, operation, and
scope status; it is inherited by mentions, filesystem/git tools, command
working directories, workflow phases, and headless runs.

In **Safe**, an exact target outside `[security].allowed_paths` is shown in the
existing approval overlay before content, directory entries, or command output
is accessed. The choices are target-once, target-this-turn, target-this-session,
or deny. These grants never imply ordinary write/execute/network capability
approval. **Plan** denies outside-scope access without prompting, while
**Yolo** selects an explicit unrestricted workspace policy. Yolo still obeys OS,
container, resource, network, and missing-path errors; it does not change the
process working directory.

All modes resolve real paths and reject or revalidate:

- `..` traversal outside the workspace;
- absolute paths outside the workspace;
- symlinks that resolve outside the workspace.

Configure the real project paths in `[security].allowed_paths`. The illustrative
`/workspace` default may not match a local checkout.

## Network boundaries

`NetworkGuard` permits exact hostnames and their subdomains. An empty
`network_allow_list` denies outbound destinations. Network tools must use the
shared HTTP client so connect and read timeout policy is consistent.

### Browser-specific boundaries

Browser backends are optional and selected by `[tools].browser_backend`;
`cloakbrowser` remains the backwards-compatible default and `playwright` is
the Microsoft Playwright alternative. Both are enabled with a liberal
allow-all policy by default for this local VPS/sandbox profile: localhost,
private addresses, arbitrary HTTP(S) hosts, and all destination ports are
reachable. Set `allow_all_domains = false` and configure `allowed_domains` to
restore hostname and private-address restrictions. CloakBrowser's CDP
transport still accepts only the configured loopback endpoint. Every
navigation, redirect, and Playwright subresource request is checked for valid
HTTP(S) syntax and DNS resolution. Browser tools cannot execute raw JavaScript,
select a proxy, read cookies/storage, or fill password/token/card-like fields.
Snapshots and screenshots are bounded, and screenshots cross the same
`WorkspaceView` path boundary as other artifacts. Capability metadata remains
the approval boundary: observation uses `NETWORK + READ`, interaction uses
`NETWORK + WRITE`, and closing uses `WRITE`.

CloakBrowser and Playwright packages/binaries are third-party components with
their own supported-platform terms. Agenthicc never auto-installs them or uses
an external detection site; operators must install them from official sources
and authorize the destinations they automate. Static HTTP tools remain
governed by their existing network boundary and are not browser fallbacks.

## Capabilities and modes

Tools carry `ToolCapability` metadata. Modes restrict the available tool set;
agent roles and phase specifications can apply a narrower set. An
`AgentCapabilityScope` denies explicit patterns first, then applies an allow
set, call budget, and spawn-depth ceiling. Child scopes are intersections and
cannot expand a parent.

## Approvals

Destructive operations can require confirmation. The approval service is
session-scoped; requests are rendered by the TUI overlays or replaced with
mock/recording services in tests. `--dangerously-skip-permissions` is an
explicit CLI escape hatch and cannot be stored in TOML. It auto-approves
ordinary capability prompts only; it does not turn Safe into Yolo or bypass an
outside-workspace approval. Headless runs fail closed for outside-workspace
requests unless a caller supplies an explicit scope-aware approval adapter.
Recorded approvals include the canonical target and operation; cassette replay
matches those fields exactly and rejects a different outside target.

## Plugin trust

Project-local tools, agents, modes, workflows, skills, and commands are Python
code. Loading them is code execution. Review them before use. The repository
has a trust helper and trust-aware paths for some extension surfaces, but the
normal `.agenthicc/tools/` discovery path currently imports project tool files
without calling that helper or showing a prompt. A trust manifest is not an
automatic boundary for user-defined tools today.

Plugin dependency auto-install is a separate risk and should remain disabled
in unattended/headless environments. The normal session tool scanner currently
skips missing dependencies rather than installing them.

Trust manifests and audit records live under `.agenthicc/`; do not commit
secrets or accept a changed hash without review. See the [user-defined tools
guide](tools.md) for the exact current tool-loading path and its limitations.
The same manual-review warning applies to `.agenthicc/commands/`; the
`agenthicc trust cli` manifest protects `.agenthicc/cli/` plugins, not normal
TUI slash-command plugins. See the [user-defined commands guide](commands.md).

## Security checklist for a new integration

- What files, commands, hosts, credentials, and subprocesses can it reach?
- What capability tags and approval level does it require?
- What happens on timeout, partial failure, retry, or cancellation?
- Can a repeated call duplicate a side effect?
- Are inputs and outputs bounded?
- Is untrusted plugin code or dependency installation involved?
- Are denial and trust decisions visible in logs and tests?

## Try it

Capability metadata is the approval boundary, so it is worth printing the exact
set your build exposes:

```bash
PYTHONPATH=src python -c "
from agenthicc.tools.capabilities import ToolCapability
print(len(ToolCapability), 'capabilities')
print(sorted(c.name for c in ToolCapability))
"
```

```text
9 capabilities
['CONTROL', 'EXECUTE', 'GIT_READ', 'GIT_WRITE', 'NETWORK', 'READ', 'SEARCH', 'UNDECLARED', 'WRITE']
```

The most consequential entry is `UNDECLARED`: a tool with no capability
decorator lands there, which prompts in Safe and is blocked in Plan. Treat a
missing decorator as a security defect, not a default.

The security-relevant configuration can be validated without opening a session:

```bash
PYTHONPATH=src python -m agenthicc config validate
```

```text
Configuration is valid: legacy execution settings (anthropic/deepseek-v4.1-flash)
```

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| A tool is silently allowed in Safe | It declares no capability metadata, or the mode was changed mid-turn | Undecorated tools are `UNDECLARED`, which prompts. Blocked capabilities are read live on every call, so a mode change applies from the next call |
| A tool is blocked in Plan but prompts in Safe | That is the designed asymmetry | Safe prompts for the restricted set `{WRITE, GIT_WRITE, EXECUTE, NETWORK, UNDECLARED}`; Plan hard-blocks it; Yolo allows all |
| `--dangerously-skip-permissions` did not make a write happen | It auto-approves ordinary prompts only | It does not turn Safe into Yolo and does not bypass an outside-workspace approval. Plan still hard-blocks side effects |
| `--dangerously-skip-permissions` will not persist from TOML | Intentionally unsupported | It is a per-invocation CLI escape hatch by design; keep it in the command line |
| A read outside the workspace is refused with no prompt | Plan denies outside-scope access without prompting | Use Safe to get the approval overlay, or add the real path to `[security].allowed_paths` |
| Headless run hangs on an approval | Safe mode with no operator attached | Headless denies by default. Supply an explicit scope-aware approval adapter, or run in Yolo inside a sandbox you control |
| An absolute or symlinked path escapes the workspace | Correct rejection | All modes reject `..` traversal, absolute paths outside the workspace, and symlinks resolving outside it, and revalidate the canonical target immediately before I/O |
| A browser tool reaches an arbitrary host | `allow_all_domains` defaults to true for this local profile | Set `[tools] browser_backend` appropriately and `allow_all_domains = false` with `allowed_domains` to restore hostname and private-address restrictions |
| A browser tool tries to fill a password field | Refused by design | Browser tools cannot execute raw JavaScript, select a proxy, read cookies/storage, or fill password/token/card-like fields |
| A project tool file ran without a trust prompt | The `.agenthicc/tools/` discovery path imports project tool files without calling the trust helper | This is a known gap. Review project tool files before use; a trust manifest is not an automatic boundary for user-defined tools today |
| Plugin dependencies were installed unexpectedly | `auto_install` enabled | Keep dependency auto-install disabled in unattended/headless environments; the normal tool scanner skips missing dependencies rather than installing them |
| `agenthicc trust cli` did not protect a slash-command plugin | It protects `.agenthicc/cli/` plugins, not normal TUI slash-command plugins | For `.agenthicc/commands/` apply the same manual review you apply to any project Python code |
| A denial is invisible during debugging | Denials should be observable | Check the approval overlay and the audit record under `.agenthicc/`; enable verbose behaviour output if you need the decision trail |

### Which layer actually decides

An allow at one layer does not bypass a stricter layer. Order of authority:
capability metadata and mode filters gate tool selection; `PermissionChecker`
and `ToolCapabilityGate` authorize; `ApprovalService` obtains the user
decision; `WorkspaceScope`/`WorkspaceAccessPolicy` bound paths before
`WorkspaceView` performs the final check; `NetworkGuard` bounds destinations.
When a call is refused, identify the layer from the error rather than disabling
the strictest one.

### Grants are per operation and do not accumulate

An outside-workspace approval offers target-once, target-this-turn,
target-this-session, or deny. Even target-this-session does not imply write,
execute, or network capability approval — those are separate decisions.
Recorded approvals include the canonical target and operation, and cassette
replay matches those fields exactly, so a replay against a different outside
target is rejected rather than silently approved.
