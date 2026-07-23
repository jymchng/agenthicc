# Security model

agenthicc runs tools that can read and change a project. Security is layered;
an allow decision at one layer does not bypass a stricter layer.

## Path boundaries

`WorkspaceView` resolves real paths and rejects:

- `..` traversal outside the workspace;
- absolute paths outside the workspace;
- symlinks that resolve outside the workspace.

Configure the real project paths in `[security].allowed_paths`. The illustrative
`/workspace` default may not match a local checkout.

## Network boundaries

`NetworkGuard` permits exact hostnames and their subdomains. An empty
`network_allow_list` denies outbound destinations. Network tools must use the
shared HTTP client so connect and read timeout policy is consistent.

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
explicit CLI escape hatch and cannot be stored in TOML.

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

## Security checklist for a new integration

- What files, commands, hosts, credentials, and subprocesses can it reach?
- What capability tags and approval level does it require?
- What happens on timeout, partial failure, retry, or cancellation?
- Can a repeated call duplicate a side effect?
- Are inputs and outputs bounded?
- Is untrusted plugin code or dependency installation involved?
- Are denial and trust decisions visible in logs and tests?
