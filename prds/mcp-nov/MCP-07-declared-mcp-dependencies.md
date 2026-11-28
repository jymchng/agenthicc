---
title: "MCP-07: Skills, agents and workflows declare the MCP servers they need"
status: Proposed
version: 1.0.0
date: 2026-11-28
priority: P2
related_prds:
  - MCP-00
  - MCP-01
  - MCP-05
  - prd-191
  - WFC-00
---

# MCP-07 — Skills, agents and workflows declare the MCP servers they need

The MCP guide promises that an unavailable server causes "the dependent operation
\[to report\] the missing required resource rather than silently proceeding without
the tools it declared" (`docs/guides/mcp.md`, "Deny by default"). Nothing lets an
operation declare what it depends on, so the promise cannot be kept. In the
incident behind MCP-01 a `$opengame` flow ran four tools without the server it was
written for.

## Evidence

| # | What is true today | Where |
|---|---|---|
| 1 | The only dependency declaration is the server-level `required = true`. It decides whether **session startup** continues (`McpRequiredServerError`), for every flow in the project, whether or not it uses that server. | `tools/mcp_manager.py:225-236,294-312`; `runners/tui_session.py:1455`, `:6315-6331` |
| 2 | Skills, agents and workflow specs cannot name an MCP server. There is no `mcp` reference in `skills/`, `agents/` or `workflows/plugin.py`. | `grep -rni mcp src/agenthicc/skills src/agenthicc/agents src/agenthicc/workflows/plugin.py` finds one unrelated comment |
| 3 | A skill can narrow the visible tools with `tools:` and remove some with `disabledTools:`. Loading it intersects the allowed set with the names it lists; a name that is not in the registry is not reported. | `skills/loader.py:33-52`, `skills/tools.py:163-166` |
| 4 | Optional servers start **in the background** so the session can be used at once. A skill invoked before a server is `ready` therefore runs without it, with no signal. | `docs/guides/mcp.md` troubleshooting ("Optional connections are started in the background") |
| 5 | A failed optional server produces one notice and `continuing without its tools` (MCP-01 row 6); it does not stop or pause the operation that needed it. | `tools/mcp_manager.py:1045` |
| 6 | Agent definitions accept a `tools` list of names validated only for syntax. Workflow phases select tools by capability, not by server. | `agents/definitions.py:77,373-376`; `workflows/plugin.py` (`PhaseSpec.allowed_capabilities`) |

## Goals

1. A skill, agent definition or workflow can say which MCP servers (and optionally
   which tools) it needs.
2. Before it starts, agenthicc checks them and either proceeds, waits briefly for a
   server that is still starting, or refuses with the cause and the fix.
3. A dependency marked optional degrades visibly instead of silently.
4. Nothing else changes: `required = true` keeps its meaning.

## Non-goals

- Installing or starting servers the operator did not configure.
- Letting an artifact configure a server (that would let a skill add a command to
  run; MCP-05 governs where servers come from).
- Ordering or retrying a flow's own steps.

## Design

### D1 — The declaration

One shape in all three places. In a skill's frontmatter:

```yaml
requiresMcp:
  - server: opengame
  - server: github
    tools: [create_issue, get_issue]
    optional: true
```

In an agent definition as `requires_mcp`, and in a workflow as
`WorkflowPlugin.requires_mcp` (workflow-wide) and `PhaseSpec.requires_mcp` (one
phase), all as a tuple of the same frozen `McpRequirement(server, tools, optional)`.
`tools` names the server's own tool names (not the `mcp:<server>:<tool>`
identity), validated at load for syntax like every other tool name.

### D2 — The check

Run at the single point where each artifact begins:

| Artifact | Seam |
|---|---|
| Skill (`$name`, `use_skill`) | where the body is rendered and `skill.tools` narrows the surface (`skills/tools.py`); the explicit `$` path in the TUI and in `_dispatch_background_command` (background worker) call the same resolver |
| Agent | when the turn's agent is selected (`agents.selection.turn_agent`) |
| Workflow / phase | before the run or phase starts (`workflows/runner` entry) |

The resolver reads `McpSessionManager.status()` and returns one outcome per
requirement:

| Server state | Result |
|---|---|
| `ready`, and every named tool is in the published catalog | **satisfied** |
| `starting` or `refreshing` | wait up to `mcp_wait_s` (default `15`, config `[tools] mcp_wait_s`), then re-evaluate |
| `ready` but a named tool is absent | **missing_tool**, listing the absent names |
| `failed`, `needs_auth`, `untrusted`, `disabled`, `stopped`, or not configured | **unavailable**, with the MCP-01 cause line and fix |

### D3 — What an outcome does

- Required requirement, not satisfied: the artifact **does not start**. The user sees
  `$opengame needs MCP server "opengame": <cause>. Fix: <command>.` In a workflow
  the phase fails with `error_code: "missing_dependency"` and the run pauses at that
  phase (the existing pause path), so it can be resumed after the fix.
- Optional requirement, not satisfied: the artifact starts, a notice names what is
  missing, and the rendered skill body is prefixed with a short, factual line the model
  can act on (`MCP server "github" is unavailable; its tools are not offered.`).
- A skill's `tools:` narrowing drops names of unavailable servers **and says so**
  (today it drops them silently).

### D4 — Authoring and `/mcp`

`create_skills`, `create_workflow` and `create_tools` already receive a capability
catalog that includes an `mcp` section (`workflows/create_workflow/catalog.py`).
Their validation checks `requiresMcp` syntax and **warns, never rejects**, when a
named server is not configured on this machine: a skill may be shared with people who
have the server. `/mcp status` lists, for each server, the artifacts in the session
that declare it.

### D5 — Background and headless

A background worker has no one to prompt. An unsatisfied required dependency sets the
session to `WAITING_INPUT` with the item text from D3 (the operator inbox), instead of
running the turn.

## Failure handling

| Case | Result |
|---|---|
| Server ready but slow (`mcp_wait_s` elapses) | unavailable, cause `still starting after 15s` |
| Tool named but renamed by the server | `missing_tool` listing the names the server does offer |
| Catalog changes between the check and the call | the existing stale-catalog guard (`McpStaleCatalogError`) still applies |
| Declaration syntax error | the artifact is skipped at load with a diagnostic (the skill loader already reports `SkillDiagnostic`) |

## Security

A declaration only reads state; it never starts, installs or configures anything. It
cannot make a server trusted (MCP-05) or add a tool to the model's surface: an
artifact that names `github` gains nothing unless the operator configured and trusted
it. A requirement string is shown as data, never executed.

## Rollout and migration

1. Needs MCP-01 (cause lines) and, for the `untrusted` row, MCP-05.
2. Skills first (the incident), then agents, then workflows and phases.
3. Existing artifacts have no requirements and behave as today.
4. Document the key where skill frontmatter is documented (`docs/guides/plugins.md`),
   in `docs/guides/agent-library.md` (agent definitions), `docs/guides/workflows.md` and
   `docs/guides/mcp.md`; update `llms-full.txt` and `CHANGELOG.md`.

## Acceptance criteria

1. A skill declaring `requiresMcp: [{server: s}]` invoked with `s` failed does not
   run; the transcript shows the cause and the fix, and no tool executed.
2. Invoked while `s` is `starting` and then becomes ready within `mcp_wait_s`, the
   skill runs once, with `s`'s tools offered.
3. `optional: true` with `s` failed: the skill runs, a notice names `s`, and the body
   carries the one-line unavailability statement.
4. A workflow phase with an unsatisfied required dependency fails
   `missing_dependency`, pauses, and resumes after the server is fixed.
5. A background session with an unsatisfied dependency reaches `WAITING_INPUT` with
   the cause as its item text and runs no turn.
6. A named tool that the server does not publish gives `missing_tool` with the
   server's actual tool names.

## Verification

The MCP-03 `basic` and `changing_catalog` scenarios supply ready, failed and renamed
states; a mutation that skips the check must fail criterion 1; a test asserts that
`required = true` startup behaviour is unchanged.

## Risks

- One more frontmatter key to document in three places. It is the same shape in each.
- The `mcp_wait_s` default (15 s) is a judgement; it is configurable and applies only
  while a server is already starting.
