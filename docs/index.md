# Agenthicc documentation

agenthicc is a Python agent runtime for software-engineering work. The
documentation in this site follows the current source tree: a Rich Live TUI, a
headless stdin runner, an event-sourced kernel, configurable workflows, and
capability-gated tools.

!!! warning "Supported surfaces"
    The repository does not currently contain a FastAPI server package or the
    older `tui.app`/`TranscriptModel` implementation described by some
    historical documents. Those gaps and the API product decision are tracked
    in [PRD-138](https://github.com/agenthicc/agenthicc/blob/main/prds/prd-138-repository-improvement-roadmap.md).

## Start here

- [Quickstart](guides/quickstart.md) — install, configure a provider, and run a
  TUI or headless session.
- [Architecture](guides/architecture.md) — understand the kernel, runners,
  reactive UI state, tools, and persistence.
- [Architecture diagram](guides/architecture-diagram.md) — the same boundaries
  as a diagram, with every node mapped to a real file.
- [Configuration](guides/configuration.md) — config discovery, precedence,
  providers, security, memory, and MCP.
- [Connecting MCP servers](guides/mcp.md) — install MCP support, register
  local or remote servers, authenticate, diagnose, and manage connections.
- [Project bootstrap](guides/project-bootstrap.md) — preview and safely write
  project-specific `AGENTS.md` guidance.
- [TUI guide](guides/tui.md) — workspace layout, modes, input, overlays, and
  slash commands.

## Build and extend

- [Workflows](guides/workflows.md) — define phases, roles, transitions, and
  resume behaviour.
- [Subagents](guides/subagents.md) — delegate typed concurrent work with
  capability, approval, timeout, cache, and troubleshooting details.
- [Custom workflows and TOML configuration](guides/custom-workflows-and-config.md)
  — define project workflows and configure per-phase models.
- [Extensions](guides/plugins.md) — tools, agents, modes, skills, commands, and
  MCP servers.
- [Memory](guides/memory.md) — session/project/global memory and semantic
  retrieval.
- [Security](guides/security.md) — path, network, capability, approval, and
  plugin trust boundaries.
- [Testing](guides/testing.md) — test layers, cassettes, TUI tests, and release
  checks.
- [Contributing](contributing.md) — repository workflow and review checklist.

## Reference

- [Reference index](reference/index.md) — every specification page, register,
  and baseline in one place.
- [CLI](reference/cli.md) — every global flag, command group, and built-in
  command, validated against the argument parser.
- [Kernel](reference/kernel.md) — the 22 exported symbols, the 20 event
  handlers, reducer purity, and persistence guarantees.
- [Storage](reference/storage.md) — session, journal, memory, cache, and
  cassette files, with their retention bounds and path provenance.
- [`code_plan` structure](reference/code-plan.md) — the reference typed-phase
  workflow.
- [Workflow package review](reference/workflow-review.md) — historical audit of
  `src/agenthicc/workflows/` with a per-finding current status.
- [Current repository state](reference/repository-state.md) — evidence-backed
  architecture boundaries, supported surfaces, and remaining release-gate
  risks.
- [API status](reference/api.md) — why there is no server API package.
- [Documentation fact base](reference/fact-base.md) — the `path:line`
  provenance register behind these claims.
- [Glossary](glossary.md) — every domain term defined with a source citation.
- [Troubleshooting index](reference/troubleshooting-index.md) — symptom-first
  navigation across the usage pages and the guide traps.
- [PRD-138 roadmap](https://github.com/agenthicc/agenthicc/blob/main/prds/prd-138-repository-improvement-roadmap.md) — the
  evidence-backed improvement backlog.
- [`llms-full.txt`](https://github.com/agenthicc/agenthicc/blob/main/llms-full.txt) — public symbol reference for AI tools.

## A minimal kernel example

```python
import asyncio

from agenthicc.kernel import AppState, Event, EventProcessor


async def main() -> None:
    processor = EventProcessor(AppState.create(), persist=False)
    task = asyncio.create_task(processor.run())
    try:
        await processor.emit(Event.create("IntentCreated", {
            "intent_id": "example",
            "raw_text": "inspect the repository",
        }))
        await processor.drain()
        print(processor.get_state().intents["example"])
    finally:
        await processor.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


asyncio.run(main())
```

The full session runner adds configuration, provider setup, workflow
selection, memory, approvals, TUI rendering, and durable journals around this
kernel loop.
