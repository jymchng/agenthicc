# FAQ

## What is agenthicc?

A state-driven agent operating system for autonomous software engineering. It
runs agent turns with filesystem, git, and command tooling, keeps durable
session records, and provides a Rich Live TUI, headless mode, workflows,
subagents, memory, and a client-neutral session service.

## How is it different from other coding agents?

The runtime is state-driven and event-sourced: a frozen kernel `AppState`
advances through a pure reducer, tools carry declared capabilities, workflows
are pluggable, and sessions are durable and resumable. It is not a thin wrapper
around an LLM API.

## Is my working tree safe?

Yes, by construction. Tools declare capabilities, and the active mode plus the
workspace boundary decide what runs. Writes, commands, and network access
prompt in Safe mode and are hard-blocked in Plan mode. Traversal, absolute
escapes, and symlinks that resolve outside the workspace are rejected in every
mode.

## How do approvals work?

Approval is **capability-based**, not severity-based — there is no
Low/Medium/High rating. A tool declares which `ToolCapability` values it needs,
and the mode gates them:

| Capability | Safe | Plan | Yolo |
|---|---|---|---|
| `READ`, `SEARCH`, `GIT_READ` | runs | runs | runs |
| `WRITE`, `GIT_WRITE`, `EXECUTE`, `NETWORK`, `UNDECLARED` | prompts | **blocked** | runs |

A tool with no capability decorator is `UNDECLARED`, which prompts in Safe and
is blocked in Plan. `--dangerously-skip-permissions` auto-approves ordinary
prompts for a session but does not turn Safe into Yolo and does not bypass an
outside-workspace approval.

## Can agenthicc run without a TUI?

Yes. `agenthicc --headless` reads stdin lines and emits newline-delimited JSON.
It exits 0 even with no input, so it works as a smoke test.

## Which providers are supported?

Anthropic (default), OpenAI, Ollama, and LiteLLM, configured through provider
profiles.

## What are modes?

Safe (prompts before side effects), Plan (plan only; side effects hard-blocked),
and Yolo (no per-action prompts). Shift+Tab cycles them; Safe is the default.
`Auto`, `Guard`, `Ask`, and `Review` are accepted aliases, and `Replay` is
internal-only.

## What workflows are built in?

Eight: `code_plan` (alias `Plan`), `copy_website`, `create_workflow`,
`goal_flow`, `make_agenthicc_tool`, `make_book`, `reconstruct_site`, and
`site_imitate`. Run `agenthicc workflows list` rather than trusting a table.

## Can I add my own tools?

Yes. Tools are class-based: implement the contract with an input/output schema,
add capability metadata, and register it. Project tools are discovered from
`.agenthicc/tools/`; MCP servers contribute tools too.

!!! warning "Your own tools are code execution"
    Project-local tools, agents, modes, workflows, skills, and commands are
    Python. Review them before use. `agenthicc trust cli` covers
    `.agenthicc/cli/` only, not `.agenthicc/tools/`.

## How do sessions work?

Every run creates a session. `agenthicc sessions list` shows them, and
`--resume <id>` / `--continue` reload prior context. The singular `session`
group reads the client-neutral projection that new adapters should use.

## Does agenthicc remember things between sessions?

Yes — tiered session/project/global memory with conversation journaling and
semantic search. Session-tier values do not survive the process; use the project
or global tier for anything durable.

## What are background sessions?

Detached long-running work managed with `/bg` or `agenthicc jobs ...`. The
manager keeps a rebuildable lifecycle index; the underlying events, approvals,
and memory remain under their own owners.

## How do I contribute?

Read `CONTRIBUTING.md` and `docs/contributing.md`. Write a PRD under `prds/`,
add tests, run the `nox` sessions, and update `CHANGELOG.md`.

## Related

- [Troubleshooting →](12-troubleshooting.md)
- [Using agenthicc →](index.md)
- Depth: [Architecture](../guides/architecture.md) · [Quickstart](../guides/quickstart.md)
