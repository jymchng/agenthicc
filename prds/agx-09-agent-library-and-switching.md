---
title: "AGX-09: Agent library — Markdown definitions, management and switching"
status: Implemented (workspace snapshot)
version: 0.1.0
date: 2026-10-23
repository: jymchng/agenthicc
priority: P1
series: AGX
related_prds: [agx-00, agx-04, agx-05, agx-13, prd-87, prd-139, prd-155, prd-197, prd-233, prd-250]
tags: [agents, profiles, tui, ux, configuration]
---

# AGX-09 — Agent library: Markdown definitions, management and switching

## Implementation evidence (2026-10-29)

The agent-library and switching implementation is present in the designated
workspace snapshot; no agenthicc commit or push was made. The snapshot includes
Markdown definitions and validation, definitions CLI operations, library/editor
flows, agent selection, and application of trusted edits from the next turn.

Focused verification passed 12 tests across
`test_agent_library.py`, `test_agent_library_editor.py`, and
`test_agent_definitions_cli.py`; the Python 3.12 unit suite passed 4,508 tests
with 14 skips.

## 1. Summary

Agenthicc's agent definitions are seven built-in roles plus trusted Python
`AgentPlugin` classes. There is no way to list them, see what one does, create
one without writing Python, or choose which agent you are talking to. PRD-233
proposes the profile *contract* (what a definition may set and the policy
ceiling); this PRD proposes the *user experience*: a Markdown definition
format, a Library tab to list, create, generate, edit and validate definitions,
switching the primary agent from the composer, invoking subagents by mention,
and hot reload.

## 2. Problem and evidence

- `build_agents_registry` (`src/agenthicc/agents/registry.py:135`) registers
  the built-ins `planner`, `executor`, `reviewer`, `explorer`, `verifier`,
  `human` and `auto`, then executes trusted `*.py` files from
  `~/.agenthicc/agents/` and `.agenthicc/agents/`. Only Python is accepted.
- No CLI command or TUI view lists definitions, their source or validity;
  `agenthicc agents` shows background sessions instead (AGX-05).
- The composer offers modes (Shift+Tab cycles Safe/Plan/Yolo) but no agent
  choice; Tab in the composer is unused outside the trigger picker.
- Loading errors go to the log (`log.warning("Failed to load agents from …")`)
  where users do not see them.

## 3. How others manage definitions

| Product | Format and locations | Management | Choosing |
|---|---|---|---|
| Claude Code | Markdown + YAML frontmatter (`name`, `description`, `tools`, `model`; body is the prompt); managed settings > `--agents` JSON > `.claude/agents/` > `~/.claude/agents/` > plugins; changes detected within seconds | `/agents` had a Library tab with guided creation and "Generate with Claude" until v2.1.197; now edit the files | `claude --agent <name>` runs a session as that agent |
| OpenCode | Markdown with frontmatter or JSON in `opencode.json`; `~/.config/opencode/agents/`, `.opencode/agents/`; fields `mode` (primary/subagent/all), `model`, `steps`, per-tool `permission`, `color`, `hidden` | `opencode debug agents` lists loaded agents | Tab cycles primary agents; `@name` invokes a subagent |
| Codex | TOML per agent (`name`, `description`, `developer_instructions`, model and feature overrides) in `~/.codex/agents/`, `.codex/agents/`; sandbox and approval policy not overridable | Files only | Spawned by the main agent |
| Grok Build | Custom types in `.grok/agents/`, `~/.grok/agents/`; personas in `.grok/personas/*.toml` | `/config-agents` (alias `/agents`); `/personas` to create, edit and delete personas | Choose the active agent in `/config-agents` (*third-party*) |

## 4. Goals and non-goals

**Goals**

1. Users define agents in Markdown without code, at project and user scope.
2. The Library tab lists every definition with source, mode, model, tools and
   validation state, and supports create, generate, edit, duplicate, delete and
   validate.
3. The primary agent can be chosen and cycled from the composer; subagent
   definitions can be invoked by mention.
4. Edits apply to the next turn without restarting; invalid edits are reported
   where users look.

**Non-goals**

- The policy semantics of what a definition may set (PRD-233). This PRD uses
  PRD-233's contract: a definition can only restrict the session's mode,
  capabilities, network and sandbox, never widen them.
- Removing Python `AgentPlugin`s: they remain the way to ship agents that need
  code, and feed the same registry.

## 5. Design

### 5.1 Markdown format

`.agenthicc/agents/reviewer.md`:

```markdown
---
name: reviewer
description: Reviews a diff for correctness and security; use after code changes.
mode: subagent            # primary | subagent | all
model: anthropic/claude-sonnet   # or a provider profile name (PRD-162)
effort: high                   # optional reasoning-effort routing (SUBAGENT-02)
context: 24000                 # optional worker memory budget (tokens)
output: {"type":"object","properties":{"summary":{"type":"string"}},"required":["summary"],"additionalProperties":false} # optional SUBAGENT-03 result contract
tools: [read, grep, glob, git_diff]   # allow-list, intersected with the session
capabilities: [read, search, git_read]
max_steps: 30
ceiling: Plan             # may only restrict the session mode (PRD-155)
color: cyan
hidden: false             # hide from the @ picker
override: false           # explicitly replace a built-in subagent role
---

You are a careful code reviewer. Report findings with file:line references…
```

- `name` and `description` are required; the body is the system prompt.
- Unknown keys are validation errors, not silently ignored (PRD-250's inert-field
  rule).
- `model` may be a model identifier or PRD-162 provider profile. For subagents,
  blank/omitted uses the operator default when configured, otherwise the parent;
  `model: inherit` explicitly selects the parent. `effort` and `context` are
  subagent routing settings defined by SUBAGENT-02; operator defaults and model
  window clamping are documented there.
- `tools` and `capabilities` are optional ceilings intersected with the session;
  `ceiling` may only restrict the active mode. `override: true` is required
  before a project subagent definition can replace a built-in role. `output` is
  an optional single-line JSON Schema object; for subagent types it requires a
  validated `submit_subagent_result` value (SUBAGENT-03).
- Supported fields in the current format: `name`, `description`, `mode`,
  `model`, `tools`, `capabilities`, `max_steps`, `ceiling`, `color`, `hidden`,
  `override`, `effort`, `context`, and `output`. SUBAGENT-05 adds its permissions
  key with the corresponding reader.
- Locations and precedence: project `.agenthicc/agents/` > user
  `~/.agenthicc/agents/` > Python plugins > built-in. A shadowed definition is
  listed with "shadowed by …".
- Built-in roles ship as Markdown in the package
  (`src/agenthicc/agents/builtin/*.md`) so users can read and duplicate them;
  their prompts move out of Python.
- Project definitions are covered by the digest-bound extension trust service
  (`agenthicc trust`, PRD-250 S-02), like other project extensions: an
  untrusted project definition is listed as "untrusted" and not loaded.

### 5.2 Library tab

```text
 Agents   Running 4 · Needs you 2 · History 214 · Library 9
 Project
 ● reviewer      subagent  claude-sonnet   read grep glob git_diff   Reviews a diff for…
 ⚠ migrator      primary   —               invalid: unknown key "toolz" (line 4)
 User
 ● docs-writer   all       gpt-5           read edit                  Writes and updates docs…
 Built-in
 ● planner       primary   (session)       read …                     Plans before acting
 ● executor ★    primary   (session)       all                        Default build agent
 n new  g generate  e edit  d duplicate  Ctrl+X delete  * set default  v validate   Enter view
```

| Key | Action |
|---|---|
| Enter | View: rendered prompt, effective tools after intersection with the session, model, ceiling, source path |
| `n` | Guided form (name, description, mode, model, tools, prompt) → writes the Markdown file at the chosen scope |
| `g` | Generate from a one-line description with the current model; opens the draft for review before saving |
| `e` | Open the file in `$EDITOR`; reload on save |
| `d` | Duplicate (built-ins included) to project or user scope |
| Ctrl+X | Delete a project or user definition after confirmation; built-ins cannot be deleted |
| `*` | Set the default primary agent for this project |
| `v` | Validate all definitions and show errors with file and line |

### 5.3 Switching and invoking

- **Primary agent:** Tab (when no picker is open) cycles definitions whose mode
  is `primary` or `all`; the status bar shows the active agent next to the mode
  (`executor · ⊘ Safe`). `/agent [name]` opens a picker. Tab joins Shift+Tab
  (mode) as OpenCode does; the binding is configurable (AGX-13).
- **Subagents:** the `@` picker gains an **Agents** section listing visible
  subagent definitions; choosing one inserts `@agent:reviewer`, and the turn
  spawns that definition through `SubagentPool` with its declared tools
  (intersected with the session's).
- **CLI:** `agenthicc --agent NAME` starts a session with that primary agent;
  `agenthicc agents definitions list [--json]`, `… show NAME` and `… validate`
  mirror the Library for scripts.

### 5.4 Hot reload

The agents directories are watched with AGX-04's change primitive. A changed
definition is re-validated and applies from the next turn; an invalid edit
keeps the last valid version active and marks the row `⚠` with the error. A
running turn never changes agent mid-turn.

## 6. Acceptance criteria

1. A Markdown definition in `.agenthicc/agents/` appears in the Library and in
   `agents definitions list --json` within 2 s of saving, without restarting.
2. A definition that requests more than the session allows runs with the
   intersection, and the Library view shows the effective tools.
3. An invalid definition is shown with its file, line and error; the previous
   valid version stays active.
4. Tab cycles primary agents; the active agent is shown in the status bar and
   recorded in the session so resume restores it.
5. `@agent:NAME` spawns a subagent of that definition; hidden definitions are
   absent from the picker but invocable by exact name.
6. Untrusted project definitions are not loaded and are labelled as such.

## 7. Test plan

- Parser and validator unit tests (required fields, unknown keys, precedence,
  shadowing).
- Library golden renders and form/generate flows with a mock model.
- Policy intersection tests against Safe, Plan and Yolo ceilings.
- Hot-reload test with valid and invalid edits.

## 8. Risks

- **Prompt quality of generated definitions.** Generation always produces a
  draft for review, never a saved file.
- **Two definition formats.** Markdown is declarative; Python plugins are for
  behaviour that needs code. Both populate the single `AgentsRegistry`.

## Sources

- Claude Code subagents: <https://code.claude.com/docs/en/sub-agents>
- OpenCode agents: <https://opencode.ai/docs/agents/>
- Codex subagents (custom agents): <https://learn.chatgpt.com/docs/agent-configuration/subagents>
- Grok Build subagents: <https://docs.x.ai/build/features/subagents>
- Grok Build modes and commands (`/config-agents`, `/personas`): <https://docs.x.ai/build/modes-and-commands>
- Grok Build cheat sheet (*third-party*): <https://www.scriptbyai.com/grok-build-cheat-sheet/>
