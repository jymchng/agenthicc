# FAQ

## What is agenthicc?

A state-driven agent operating system for autonomous software engineering: it
runs agent turns with full filesystem/git/command tooling, keeps durable
session records, and provides a Rich Live TUI, headless mode, workflows,
subagents, and memory.

## How is it different from other coding agents?

agenthicc is a state-driven system with a durable event-sourced kernel,
capability-gated tools, configurable workflows, background sessions, and a
session service — not a thin wrapper around an LLM API.

## Is my working tree safe?

Yes. Tools are capability- and approval-gated; writes, commands, and network
actions require approval in Safe mode and are hard-blocked in Plan mode.

## How do approvals work?

Every tool has a risk level. Low auto-approves, Medium auto-approves unless
strict, High/Critical always ask. `--dangerously-skip-permissions` disables
prompts for a session (Plan still hard-blocks).

## Can agenthicc run without a TUI?

Yes. `agenthicc --headless` reads stdin lines and emits JSON-lines.

## Which providers are supported?

Anthropic (default), OpenAI, Ollama, LiteLLM — via provider profiles.

## What are modes?

Safe (read-only + approval), Plan (read-only planning), Yolo (full
capabilities). Shift+Tab cycles them.

## What workflows are built in?

`code_plan` (plan-and-execute with approval gates), `site_imitate` (mobile-
first responsive websites), plus user-authored workflows.

## Can I add my own tools?

Yes — class-based tools with capability metadata, registered via plugin
loaders. MCP servers also contribute tools.

## How do sessions work?

Every run creates a session. `agenthicc sessions list` shows them;
`--resume <id>` / `--continue` reload prior context.

## Does agenthicc remember things between sessions?

Yes — tiered memory (session/project/global) with conversation journaling and
semantic search.

## What are background sessions?

Detached long-running work managed via `/bg` or `agenthicc jobs ...`.

## How do I contribute?

Read `CONTRIBUTING.md` and `docs/contributing.md`. Write a PRD under `prds/`,
add tests, run `nox` sessions, and update `CHANGELOG.md`.

## Related

- [Troubleshooting →](12-troubleshooting.md)
- [Quickstart](../guides/quickstart.md)
