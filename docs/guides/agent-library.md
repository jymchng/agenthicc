# Agent Library

Agent definitions are reusable roles stored as Markdown. The same definition
registry feeds primary-agent selection, subagent mentions, the TUI Library, and
the `agenthicc agents definitions` commands.

## Locations and precedence

- Built-ins ship in `src/agenthicc/agents/builtin/`.
- User definitions live in `~/.agenthicc/agents/*.md`.
- Project definitions live in `.agenthicc/agents/*.md`.

Among valid definitions, project definitions take precedence over user definitions,
Python agent plugins, and built-ins. The Library and CLI retain shadowed entries
and show which source wins. For subagent definitions, a project definition that
shadows a built-in worker role must set `override: true`; otherwise that subagent definition is refused. Invalid or untrusted
higher-precedence definitions do not fall through
to a broader built-in subagent. Project Markdown definitions need digest-bound
trust before they are loaded. Creating one in the Library records trust, while
editing an existing project definition revokes trust so changed content is
reviewed before use. See [Extensions](plugins.md#trust-and-dependencies).

## Markdown format

A definition starts with strict YAML-like frontmatter and uses its Markdown body
as the role prompt:

```markdown
---
name: reviewer
description: Reviews a change for correctness and security.
mode: subagent
model: ""                # model id or provider profile; blank uses session/default
effort: high
context: 24000
output: {"type":"object","properties":{"summary":{"type":"string"},"findings":{"type":"array","items":{"type":"string"}}},"required":["summary","findings"],"additionalProperties":false}
tools: [read_file, git_diff]
capabilities: [read, git_read, search]
max_steps: 30
ceiling: Plan
color: cyan
hidden: false
override: false
permissions:
  deny_tools: [outlook_*]
  deny_capabilities: [write]
  require_approval: [execute]
  workspace_paths: [docs]
---

Review the change carefully. Report actionable findings with file and line.
```

`name` and `description` are required. `mode` is `primary`, `subagent`, or
`all`. For subagent definitions, blank `model` uses the operator's
`subagents.default_model` when set, otherwise the parent model; `model: inherit`
explicitly selects the parent and bypasses that default. Primary definitions use
the session's configured model when `model` is blank. `effort` may be `none`,
`minimal`, `low`, `medium`, `high`, or `xhigh`; `context` is a positive token
budget. These fields may also use operator defaults from `[subagents]`; context
is clamped to the selected model's window. See [subagent routing](subagents.md#memory-and-provider-calls)
for precedence and provider-fallback behavior. `tools` and `capabilities` are
optional allow-lists and are intersected with the session's available tools and
policy. `ceiling` can only restrict the active mode. Set `override: true` only
when a project subagent definition is intended to replace a built-in worker
role. Unknown fields, malformed values, and invalid names are errors with file
and line information; they are not silently ignored.

`output` is an optional, single-line JSON Schema object for subagent definitions.
When present, the worker must submit its result with `submit_subagent_result`;
prose alone cannot satisfy the contract. The declared schema becomes that tool's
input schema, and the runtime validates the submitted object before returning it
as a structured value. Schemas are bounded to 16 KiB and a restricted local
Draft 2020-12 subset; remote references and secret-shaped property names are
rejected. Submitted JSON is bounded to 64 KiB. Types without `output` continue
to return plain text. See [structured subagent results](subagents.md#structured-result-contracts).

The supported frontmatter fields are `name`, `description`, `mode`, `model`,
`tools`, `capabilities`, `max_steps`, `ceiling`, `color`, `hidden`, `override`,
`effort`, `context`, `output`, and the optional nested `permissions` mapping.
Permission constraints apply to subagent execution (`mode: subagent` or
`mode: all`) and can only reduce the inherited session policy; a primary-only
definition with `permissions` is rejected:

- `deny_tools` blocks matching tool names; `fnmatch` patterns such as
  `outlook_*` are supported and take precedence over allow-lists.
- `deny_capabilities` hard-blocks those capability classes for this type.
- `require_approval` adds approval requirements for those capabilities where
  the inherited mode does not already block them.
- `workspace_paths` narrows filesystem access to roots within the inherited
  workspace scope. An empty list denies all workspace paths; a path outside the
  inherited scope makes the definition unavailable rather than widening access.

Lists may use `[item, item]` or one `- item` per indented line, including under
`permissions`. The parser intentionally supports a small YAML subset; YAML
features such as anchors and block scalars are not accepted.

## Manage definitions

Open `agenthicc` and switch the agents hub to **Library** (`4`), or run
`agenthicc agents --tab library`. Use the Library keys shown in the footer:

- `n` guides you through name, description, mode, model, tools, and prompt.
- `g` asks the current configured model for a draft. Review it before saving;
  generation never writes a file by itself.
- `e` opens the selected project or user Markdown definition in `$VISUAL` (or
  `$EDITOR`). The editor is launched without a shell, with the TUI terminal
  safely handed over; if neither variable is set, the built-in multiline editor
  is used. The Library reloads the definition when editing ends.
- `d` duplicates a valid definition, including a built-in, to project or user
  scope.
- `Ctrl+X` deletes a project or user definition only after confirmation.
- `*` sets an active primary definition as the project default.
- `v` reloads the catalog and reports definitions that need attention.
- `Enter` shows the prompt, declared tools and capabilities, model, ceiling,
  source path, and current validation state.

Project writes are atomic and stay under `.agenthicc/agents/`; user writes stay
under `~/.agenthicc/agents/`. The manager refuses symlink targets and refuses to
overwrite an existing file except when editing that definition.

## CLI

```console
agenthicc agents definitions list
agenthicc agents definitions list --json
agenthicc agents definitions show reviewer
agenthicc agents definitions validate
```

`list` includes source, winning/shadowed/invalid/untrusted state, mode, model,
path, and default status. Its JSON form emits the same catalog metadata, with
`tools` and `capabilities` as arrays when defined. `show NAME` prints all
candidates with that name (including shadowed candidates) and the prompt for
valid definitions. `validate` reports invalid and untrusted entries and exits
with status 1 when attention is required. These commands do not edit files or
change trust.

## Choosing and invoking agents

The composer can cycle the available primary agents with Tab, and `/agent` opens a
picker for direct selection. The status bar shows the selected agent; the choice
is saved with the session and restored on resume. Start a new session with
`agenthicc --agent NAME` to choose its initial primary agent. Definitions whose
mode is `subagent` are available to the `@agent:NAME` mention picker when they
are visible; `hidden: true` removes a definition from the picker but does not
remove exact-name invocation. A definition can restrict tools, capabilities,
model, step count, and mode ceiling, but cannot grant permissions beyond the
current session policy.
