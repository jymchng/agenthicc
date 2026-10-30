# Agent Library

Agent definitions are reusable roles stored as Markdown. The same definition
registry feeds primary-agent selection, subagent mentions, the TUI Library, and
the `agenthicc agents definitions` commands.

## Locations and precedence

- Built-ins ship in `src/agenthicc/agents/builtin/`.
- User definitions live in `~/.agenthicc/agents/*.md`.
- Project definitions live in `.agenthicc/agents/*.md`.

When names collide, project definitions win over user definitions, Python agent
plugins, and built-ins. The Library and CLI retain shadowed entries and show
which source wins. Project Markdown definitions must have digest-bound trust
before they are loaded; creating one in the Library records trust, while editing
an existing project definition revokes its prior trust so changed content is
reviewed before use. See [Extensions](plugins.md#trust-and-dependencies).

## Markdown format

A definition starts with strict YAML-like frontmatter and uses its Markdown body
as the role prompt:

```markdown
---
name: reviewer
description: Reviews a change for correctness and security.
mode: subagent
model: ""
tools: [read_file, git_diff]
capabilities: [read, git_read, search]
max_steps: 30
ceiling: Plan
color: cyan
hidden: false
---

Review the change carefully. Report actionable findings with file and line.
```

`name` and `description` are required. `mode` is `primary`, `subagent`, or
`all`; blank `model` uses the session's configured model. `tools` and
`capabilities` are optional allow-lists and are intersected with the session's
available tools and policy. `ceiling` can only restrict the active mode. Unknown
fields, malformed values, and invalid names are errors with file and line
information; they are not silently ignored.

The supported frontmatter fields are `name`, `description`, `mode`, `model`,
`tools`, `capabilities`, `max_steps`, `ceiling`, `color`, and `hidden`. Lists may
use `[item, item]` or one `- item` per indented line. The parser intentionally
supports a small YAML subset; YAML features such as anchors and block scalars
are not accepted.

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
