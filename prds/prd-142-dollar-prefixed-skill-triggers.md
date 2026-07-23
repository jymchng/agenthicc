---
title: "PRD-142: Dollar-Prefixed Skill Triggers"
status: Implemented
version: 1.0.0
created: 2026-07-23
related_prds:
  - PRD-138  # Repository Improvement Roadmap
  - PRD-139  # OpenCode-Inspired Product Expansion and Privacy-First Advertisements
  - PRD-22   # Skills Metadata and Discovery
  - PRD-23   # Skills Runtime
  - PRD-36   # Slash Command Trigger
  - PRD-39   # Input Trigger System
  - PRD-40   # Slash Command and Help Surface
  - PRD-45   # Command Source Namespacing
  - PRD-69   # Trigger Picker and Rendering
supersedes: []
implemented: 2026-07-23
tags:
  - skills
  - commands
  - tui
  - triggers
  - namespace
---

# PRD-142 — Dollar-Prefixed Skill Triggers

Study date: 2026-07-23. This PRD defines the implemented replacement of the
user-facing explicit skill trigger from `/skill-name` with `$skill-name`, while
keeping `/` for commands and preserving the existing skill execution contract.

## 1. Executive summary

agenthicc currently presents skills and commands through the same `/` trigger.
For example, `/review-code` can invoke a skill, while `/config` invokes a
built-in command. The same symbol makes the picker, help output, input routing,
and user mental model treat two different extension surfaces as one namespace.

This change is feasible with a bounded TUI/input-layer migration:

- `/` remains the command trigger.
- `$` becomes the canonical explicit skill trigger, for example `$review-code`.
- Skill arguments, aliases, permissions, body processing, pending-skill
  injection, reload, and automatic topic matching remain unchanged.
- The existing command registry and skill handler remain authoritative; the
  new trigger adapts their user-facing spelling instead of creating a second
  execution path.
- The legacy `/skill-name` form is removed completely. It is not registered,
  shown in the `/` picker, or dispatched by the command router.

The change should be implemented as a product/UX migration, not as a rewrite
of the skill loader or command dispatcher.

## 2. Problem statement and repository evidence

### 2.1 User problem

The current symbol does not communicate whether an entry is a command or a
reusable skill. This creates several usability problems:

1. The `/` picker mixes built-in commands, project commands, and skills.
2. `/skills` reports skill names using command syntax, reinforcing the same
   namespace even though skills have different lifecycle and permission rules.
3. Users cannot infer from the input prefix whether an entry will dispatch a
   command immediately or prepare skill instructions for the next agent turn.
4. A command and a skill are forced through the same visible namespace even
   though they have different ownership and extension contracts.

### 2.2 Current implementation evidence

| Concern | Current implementation | Consequence for this PRD |
|---|---|---|
| Skill representation | `src/agenthicc/skills/loader.py` provides `SkillDef`, canonical slugs, aliases, permissions, and lazy bodies | No skill-file format change is needed |
| Skill command registration | `_build_skill_command()` in `src/agenthicc/runners/tui_session.py` creates `Command(name="$slug", group="Skills", source_id="skill:<slug>")` | Skill records use the same live registry with an explicit dollar namespace |
| Command registry | `src/agenthicc/commands/registry.py` stores canonical names and aliases and resolves them for dispatch | Existing command execution can remain authoritative |
| Skill execution | `_make_skill_handler()` in `src/agenthicc/commands/builtins.py` applies permissions, processes arguments/body, and calls `set_pending_skill` | `$` must call this same handler path |
| Generic trigger registry | `src/agenthicc/tui/trigger.py` maps one character to a handler | A second `$` handler fits the current architecture |
| Command picker | `src/agenthicc/tui/triggers/slash_command.py` uses the command registry and currently returns all command groups | It must filter out skill-owned commands for the canonical `/` picker |
| Input submission | `src/agenthicc/tui/input/unified_session.py` owns trigger-picker activation, while `TUISession.route()` handles submitted slash text | `$` needs both picker registration and submitted-text routing |
| Completion adapter | `src/agenthicc/tui/input/completions.py` contains the compatibility completer and adapter | Completion behavior is split into command-only slash and skill-only dollar surfaces |
| Reload | `TUISession._reload_skills()` replaces skill-owned command registrations while preserving the registry object | The `$` handler must retain the live registry reference and observe reloads |
| Automatic skills | `AgentTurnRunner._inject_skills()` uses topic matching independently of explicit syntax | Automatic activation must not change |

### 2.3 Feasibility result

**Feasibility: high.** The existing trigger abstraction already separates
activation characters from input dispatch. The main implementation work is
source-aware filtering/presentation and a small routing boundary for
submitted `$` tokens. No model, workflow, memory, tool, security, or skill
body-processing redesign is required.

## 3. Goals

### 3.1 Primary goals

- Make `$skill-name` the canonical explicit skill syntax in the interactive
  TUI.
- Keep `/command` as the canonical command syntax.
- Keep the existing skill handler, arguments, aliases, permissions, body
  processing, pending-skill mechanism, and agent-turn execution unchanged.
- Make the trigger picker distinguish skills from commands.
- Keep skill reloads, project/user discovery, diagnostics, and agent-specific
  allow/deny policies working without a session restart.
- Make the removal of `/skill-name` explicit so command and skill namespaces
  cannot cross-dispatch.

### 3.2 Secondary goals

- Make `/skills` and command/help surfaces display the canonical `$` spelling.
- Make permission and invocation feedback identify the `$` form.
- Provide an explicit escape for users who need a literal dollar-prefixed
  line.
- Keep non-interactive and automatic skill behavior explicit and stable.

## 4. Non-goals

- Changing `SKILL.md` frontmatter, directory layout, canonical slug rules, or
  alias metadata.
- Changing automatic topic-based skill activation.
- Changing `process_skill_body()`, `{args}` substitution, `!\`shell\``
  substitution, reference loading, templates, or skill execution limits.
- Changing skill permissions, tool restrictions, model overrides, or agent
  allow/deny rules.
- Renaming built-in, plugin, workflow, or MCP commands.
- Introducing a second command registry or a second skill execution pipeline.
- Changing shell tools, environment-variable expansion, Markdown, or model
  prompt semantics outside the input line that begins with `$`.
- Adding a new remote API or changing headless workflow protocol semantics.
- Preserving the removed `/skill-name` syntax or adding a compatibility flag for
  it. The cutover is intentionally immediate and unambiguous.

## 5. User-facing contract

### 5.1 Canonical syntax

| Surface | Canonical form | Example |
|---|---|---|
| Built-in command | `/name [args]` | `/model openai/gpt-5` |
| Project command | `/name [args]` | `/deploy staging` |
| Explicit skill | `$name [args]` | `$review-code src/app.py` |
| Explicit skill alias | `$alias [args]` | `$review src/app.py` |
| Automatic skill | Ordinary natural-language input | `review this change` |

The `$` token is recognized only when it begins the current input line, or
follows a newline in a multiline input, matching the current slash-command
activation boundary. A `$` in the middle of ordinary prose does not open the
skill picker.

### 5.2 Arguments and execution

`$skill-name arg1 arg2` must produce exactly the same skill body and pending
skill state as the prior skill execution path.
Argument splitting, empty arguments, invalid arguments, and errors retain the
current behavior. The same `CommandContext` permission checks and
`set_pending_skill` callback remain in use.

Selecting a skill from the picker inserts `$skill-name` into the composer. It
does not submit automatically, matching current slash-command picker behavior.

### 5.3 Picker behavior

- Typing `$` at an activation boundary opens a skill-only picker.
- The picker filters canonical skill names and aliases by the current fragment.
- Rows show `$name`, the skill description, and the existing argument hint.
- Enter inserts the selected `$name`; Escape restores the literal buffer.
- The picker remains line-aware, scrollable, and compatible with the existing
  overlay/live-region rendering path.
- Typing `/` opens a command-only picker. Skill rows do not appear there.
- Built-in commands such as `/skills`, `/commands`, `/workflow`, and `/mcp`
  remain slash commands.

### 5.4 Literal dollar input

The escape form `\$` prevents picker activation at the beginning of a line and
is submitted as ordinary literal input; the migration must not silently strip
the escape character. A user may also cancel the picker and submit the literal
text. Unknown `$name` input that is submitted without selecting a known skill
is treated as ordinary user text rather than an error or command.

This prevents the migration from turning every dollar-prefixed prompt into a
failed command while still reserving the unescaped line-leading `$` form for
known skill activation.

### 5.5 Removed legacy syntax

`/skill-name` and `/alias` are not valid skill invocations. The runtime does
not register slash-prefixed skill records, the command picker and completer do
not expose them, and submitted legacy text never reaches a skill handler. A
legacy line can still be displayed in an old transcript, but it is never
executed as a skill.

This is a deliberate breaking cutover: users and integrations must migrate to
`$skill-name` and `$alias`. `/skills` documents only the dollar form.

### 5.6 Help and status text

All user-facing skill references use `$` as the canonical spelling:

- `/skills` lists `$canonical-name` and `$alias`.
- Skill invocation, denied-permission, and pending-body messages use `$name`.
- Command/help tables label skill entries with `$` because the canonical
  command records are dollar-prefixed.
- Documentation examples use `$skill-name`.

## 6. Implemented technical design

### 6.1 Preserve internal ownership boundaries

The implementation retains the current ownership boundaries:

1. `SkillDef` and discovery remain in `skills/loader.py`.
2. The existing skill handler factory remains in `commands/builtins.py`.
3. `UnifiedCommandRegistry` remains the live registry used by dispatch and
   reload. Skill records are registered with `$slug` and `$alias` names.
4. `TriggerManager` receives a new skill-specific handler for `$`.
5. `UnifiedInputSession` remains responsible for generic picker behavior.
6. `TUISession.route()` remains responsible for session-aware submitted-input
   routing and dispatches a recognized `$` skill token through the existing
   skill command handler.

This avoids duplicating permission checks or skill-body execution in a new
`SkillTrigger` implementation.

### 6.2 New skill trigger handler

Add a handler in `src/agenthicc/tui/triggers/` dedicated to skills, with the
same `TriggerHandler` contract as the existing slash handler. It should:

- declare `char = "$"` and a human-readable label such as `Skill`;
- use the live command registry reference or an equivalent source-aware view;
- include only commands whose source is `skill:<slug>` or whose group is
  `Skills`;
- match `$fragment` against canonical skill names and aliases;
- return `$name` values from `MatchItem`, never slash values;
- reuse the existing description, hint, wrapping, cancel, and activation
  semantics.

The handler must not create a parallel list of built-in skills. The live
registry/skill mapping already changes during `/skills reload` and must remain
the source observed by the picker.

### 6.3 Slash command filtering

Update the slash trigger and its completion adapter to distinguish
skill-owned entries from commands:

- default slash-picker results exclude `group == "Skills"` and
  `source_id.startswith("skill:")`;
- built-in, project, plugin, workflow, MCP, and other non-skill commands remain
  unchanged;
- filtering must use source metadata rather than a hard-coded skill-name list.

The command registry must not resolve a slash-prefixed skill entry.

### 6.4 Submitted `$` routing

When the submitted message is a line-leading `$` token:

1. Parse the first token and preserve the remainder as the existing argument
   string.
2. Resolve the token only against skill-owned command entries.
3. If it resolves, invoke the same command handler and permission path used by
   the slash form.
4. If it does not resolve, return `False` from command routing so the ordinary
   user message path receives the text.
5. Never route a `$` occurring after ordinary text as a skill command.

The route passes the `$` token directly to the existing `CommandDispatcher`,
so user-facing context and diagnostics retain the `$` spelling and there is no
legacy slash translation path.

### 6.5 Registration and reload

The initial trigger registry in `_build_session_context()` registers both:

- `SlashCommandTrigger` for `/` and non-skill commands;
- `SkillTrigger` for `$` and skill commands.

Both handlers must retain a reference to the same session-owned registry or a
live source view. After `_reload_skills()` replaces skill-owned command entries,
the `$` picker and route must see additions, removals, aliases, conflicts, and
permission metadata without rebuilding the input session.

### 6.6 Completion adapter

The current `SlashCommandCompleter` remains a compatibility adapter, not a
second command list. The implemented design adds a source-aware
`SkillCompleter` for `$` and makes the slash completer command-only. Canonical
command definitions remain in the existing registry.

## 7. Behavior matrix

| Input | Expected result |
|---|---|
| `/model` | Existing built-in command dispatch |
| `/my-command` | Existing project/plugin command dispatch |
| `$my-skill` | Canonical skill dispatch |
| `$skill-alias` | Same skill dispatch through its alias |
| `/my-skill` | Never dispatches a skill; remains a local unknown slash input |
| `$unknown` | Ordinary user message if submitted; picker may show no matches |
| `explain $my-skill` | Ordinary user message; no picker and no skill dispatch |
| `\$literal` | Ordinary user message; no picker or skill dispatch |
| `\n$my-skill` in multiline input | Skill picker/dispatch at the new line |
| `$my-skill args` when denied | Existing denial behavior, with `$my-skill` display |
| `$my-skill` after `/skills reload` | Uses the current reloaded skill definition |
| natural-language topic match | Existing automatic skill injection, unchanged |

## 8. Collision and security policy

### 8.1 Name collisions

- A command name continues to own its `/name` namespace.
- A skill name/alias owns its `$name` namespace when the skill is registered.
- A command and skill with the same slug may coexist because they own separate
  `/name` and `$name` namespaces.
- Duplicate names within one namespace retain deterministic registry conflict
  handling and never silently select a different owner.

### 8.2 Permission and trust boundaries

- `$` is only an alternate spelling; it does not grant a skill additional
  tools, agents, models, filesystem scope, network access, or approval bypass.
- `SkillDef.is_allowed_for()` and configured skill allow/deny policies are
  evaluated exactly once through the existing skill handler.
- User-controlled skill arguments remain subject to existing body processing
  and tool security boundaries.
- `$` must not be interpreted as shell expansion by the input layer.
- The removed slash spelling must not bypass the namespace boundary or reach a
  skill handler.
- Automatic skill injection must continue to pass through the existing
  agent-specific filtering path.

### 8.3 Logging and redaction

User input and skill names may appear in the local conversation/log surfaces
under current policy. The migration must not add API keys, skill bodies,
arguments, or prompt contents to new telemetry. No migration warning or
remote telemetry is added.

## 9. Rollout and migration

### Phase 1 — Trigger and routing implementation

- Add the `$` skill trigger and source-aware matching.
- Keep the existing skill handler and command dispatcher authoritative.
- Register `$` in the existing trigger manager.
- Add submitted `$` routing and literal escape behavior.

### Phase 2 — UX surface cutover

- Remove skill rows from the canonical `/` picker.
- Update `/skills`, `/commands`, permission messages, picker hints, and
  documentation to show `$`.
- Add completion coverage and reload coverage.

### Phase 3 — Verification and release

- Verify that `/skill-name` and `/alias` cannot dispatch, including through a
  stale manually injected skill record.
- Verify command/skill namespace separation, permissions, reloads, completion,
  and the interactive picker.
- Publish the breaking syntax change in the user guide and release notes.

Existing `SKILL.md` files require no migration. A session transcript may
contain old `/skill` text; replay and historical display remain readable, but
re-submitting that text does not execute a skill. New picker selections and
documentation use `$`.

## 10. Acceptance criteria

### Trigger and picker

- **A1** — The TUI registers a `$` trigger without changing the existing
  `TriggerManager` contract.
- **A2** — `$` activates only at the same line-boundary positions as `/`.
- **A3** — The `$` picker lists canonical skill names and aliases with `$`
  prefixes, descriptions, hints, wrapping, selection, cancellation, and
  scrolling equivalent to the current slash picker.
- **A4** — The `/` picker lists commands and no skill-owned entries.
- **A5** — `$` picker results update after `/skills reload` without restarting
  the TUI.

### Dispatch and behavior preservation

- **A6** — `$skill args` invokes the existing skill handler with the same
  arguments and processed body as the former skill execution path.
- **A7** — Skill aliases work as `$alias`; slash-prefixed aliases do not invoke
  the skill.
- **A8** — Unknown `$` text is not sent to a command handler and remains a
  normal user message when submitted.
- **A9** — `\$text` remains literal input.
- **A10** — Automatic topic-based activation is behaviorally unchanged.
- **A11** — All skill permission, agent allow/deny, disabled-tool, model, and
  maximum-depth policies remain enforced.

### User-facing surfaces

- **A12** — `/skills` reports `$` as the only explicit skill syntax.
- **A13** — Skill invocation and denial messages use the canonical `$` spelling.
- **A14** — Command help and command-plugin discovery remain slash-based.
- **A15** — Existing built-in, project, plugin, workflow, and MCP commands are
  unchanged.

### Namespace removal and safety

- **A16** — `/skill` and `/alias` never invoke a skill, are absent from the
  slash picker/completer, and cannot dispatch through a stale slash-named
  skill record.
- **A17** — Command/skill name conflicts produce deterministic diagnostics and
  do not route to the wrong owner.
- **A18** — No new command list, skill execution path, permission bypass, or
  telemetry surface is introduced.
- **A19** — Non-interactive/headless behavior is documented and existing
  behavior is covered; this PRD does not silently change JSON-lines semantics.

## 11. Verification plan

### Unit tests

- `tests/unit/test_skill_trigger.py`
  - `$` character and label;
  - canonical and alias matching;
  - selection/cancel formatting;
  - description/hint/wrapping;
  - activation at empty/newline buffers and rejection mid-line;
  - permission-filtered or unavailable entries.
- `tests/unit/test_slash_trigger.py`
  - command-only filtering;
  - preservation of built-in/plugin/MCP rows;
  - no legacy slash skill rows.
- `tests/unit/test_trigger_system.py`
  - `$` registration/resolution and input-boundary behavior.
- `tests/unit/test_tui_session_coverage.py` or a focused route test
  - `$` dispatch, unknown `$`, escaped `\$`, rejected legacy slash input, and
    argument preservation.
- `tests/unit/test_skill_reload.py`
  - `$` trigger sees added/removed skills and aliases after reload.
- `tests/unit/test_skills_loader.py` / command lifecycle tests
  - existing canonical slug/alias/permission behavior remains unchanged.

### Integration and end-to-end tests

- Submit `$skill args` through the command and TUI routing layers and verify
  the expected pending skill body and argument preservation.
- Exercise the trigger picker through `UnifiedInputSession` and verify both
  activation characters select the correct surfaces.
- Verify a command and a skill with similar names cannot cross-dispatch.
- Test interactive and non-interactive input paths.
- `tests/e2e/test_skill_trigger_e2e.py` exercises discovered skill metadata,
  trigger registration, picker filtering, alias dispatch, and the negative
  slash-dispatch path end to end.
- Run the repository unit, integration, E2E, full test, Ruff, mypy, and type
  audit commands defined in `AGENTS.md`.
- Update the user guide and run the strict docs build.

## 12. Implementation ownership and likely files

| Change | Canonical owner |
|---|---|
| `$` handler | `src/agenthicc/tui/triggers/` |
| Slash command filtering | `src/agenthicc/tui/triggers/slash_command.py` and the completion adapter |
| Submitted `$` routing | `src/agenthicc/runners/tui_session.py` |
| Trigger registration | `_build_session_context()` in `src/agenthicc/runners/tui_session.py` |
| Skill presentation text | `src/agenthicc/commands/builtins.py` and user-facing guides |
| Skill discovery/permissions | No ownership change; `src/agenthicc/skills/loader.py` remains authoritative |
| Input overlay/rendering | No ownership change; `UnifiedInputSession` and `TriggerPickerOverlay` remain generic |

No changes should be made to historical paths such as `tui/app.py`,
`tui/events.py`, or `tools/executor.py`.

## 13. Risks and decisions

| Risk or decision | Treatment |
|---|---|
| Existing users rely on `/skill` | Document the breaking cutover; do not retain an executable alias |
| Slash picker currently assumes all registry rows are commands | Add source-aware filtering, not a second registry |
| `$` has shell/environment-variable meaning | Restrict activation to line-leading boundaries and support `\$` escape |
| Skill and command names collide | Keep `/name` and `$name` as independent namespaces |
| `$` route duplicates command execution | Invoke the existing dispatcher; do not copy permission/body logic |
| Skill reload replaces registry contents | Keep handlers bound to the live registry identity and test reload |
| Help output exposes internal names | Store and display canonical `$` skill names |
| Headless input has no TUI picker | Document that this PRD changes interactive explicit syntax only unless a separate headless routing requirement is approved |
| Third-party trigger plugins expect `/` to contain every entry | Preserve the generic trigger protocol and provide source-aware filtering |

The implementation is complete for the interactive TUI. Headless JSON-lines
semantics remain unchanged and are not treated as an implicit compatibility
path for `/skill-name`.

## 14. Related documentation

- [PRD-138 — Repository Improvement Roadmap](prd-138-repository-improvement-roadmap.md)
- [PRD-139 — OpenCode-Inspired Product Expansion and Privacy-First Advertisements](prd-139-opencode-inspired-features-and-privacy-first-ads.md)
- [Skills, plugins, and extension discovery](../docs/guides/plugins.md)
- [Plugins and MCP](../docs/guides/plugins.md)
- [Commands](../docs/guides/commands.md)
- [TUI guide](../docs/guides/tui.md)
