# Agenthicc skills

Skills are self-contained reference guides written for AI assistants to read.
Each skill is a directory containing a `SKILL.md` file whose YAML frontmatter
describes the skill and whose body carries the guidance.

A skill is not a prompt template and not a plugin. It is documentation that the
agent can pull into context on demand, either because the user invoked it
explicitly or because its description matched the task at hand.

## How skills are invoked

| Route | Example | Notes |
|---|---|---|
| Explicit trigger | `$running-the-tui` | Type `$` followed by the skill's canonical name |
| Slash listing | `/skills`, `/skills reload` | List available skills or re-scan the roots |
| Natural language | "how do I use the TUI?" | The model may load a skill whose description matches |

Canonical names are kebab-case and derive from the directory name. A skill may
also declare compatibility `aliases` so older names keep working:

```text
$headless-mode      # canonical
$headless-api       # declared alias, resolves to the same skill
```

## Skill frontmatter

Frontmatter is a YAML mapping between `---` delimiters. The loader validates it
and reports anything it does not recognize, so a typo degrades the skill
silently only if you ignore the diagnostics.

```yaml
---
name: running-the-tui
version: 1.0.0
tags: [tui, terminal, rich, slash-commands, approvals]
description: >-
  Operate the interactive Rich Live terminal workspace: the scroll buffer and
  pinned composer layout, modes, overlays, slash commands, input handling,
  approval flows, background sessions, and the platform terminal backends.
---
```

### Canonical fields

| Field | Type | Required | Purpose |
|---|---|---|---|
| `name` | string | effectively yes | Display name. Falls back to the directory name with a `missing-name` warning |
| `description` | string | effectively yes | Shown in listings and used for relevance matching. Absent produces `missing-description` |
| `tags` | list of strings | no | Free-form discovery labels |
| `version` | string | no | Author's own version marker |
| `author` | string | no | Attribution |
| `aliases` | list of strings | no | Compatibility trigger names |
| `suggestedTopics` | list of strings | no | Topics this skill should be surfaced for |
| `disallowAutoTriggering` | boolean | no | Set `true` to require explicit `$name` invocation |
| `allowedAgents` | list of strings | no | Restrict which agent roles may use the skill |
| `deniedAgents` | list of strings | no | Exclude specific agent roles |
| `permissions` | mapping | no | Per-agent allow/deny rules; `permissions.agents.allow` and `.deny` mirror `allowedAgents`/`deniedAgents` |
| `tools` | list of strings | no | Tools the skill expects to be available |
| `disabledTools` | list of strings | no | Tools to withhold while the skill runs |
| `maxTurnDepth` | positive integer | no | Turn budget for the skill; defaults to `200` |
| `model` | string | no | Preferred model override |
| `source` | string | no | Provenance marker |

### Compatibility field aliases

The `snake_case` and mixed-case spellings below are accepted and reported as an
`info` diagnostic recommending the canonical spelling. Using a canonical field
and its alias together produces a `duplicate-field-alias` warning, and the
canonical field wins.

| Canonical | Accepted aliases |
|---|---|
| `suggestedTopics` | `suggested_topics`, `topics` |
| `disallowAutoTriggering` | `disallow_auto_triggering` |
| `disabledTools` | `disabled_tools` |
| `maxTurnDepth` | `max_turn_depth` |
| `allowedAgents` | `allowed_agents`, `allowAgents`, `allow_agents` |
| `deniedAgents` | `denied_agents`, `denyAgents`, `deny_agents` |
| `aliases` | `alias` |

### Limits

| Rule | Value |
|---|---|
| Canonical name length | 64 characters |
| Description length | 1 536 characters |
| Canonical name shape | `^[a-z0-9]+(?:-[a-z0-9]+)*$` |
| Directory name | Must already be canonical kebab-case, or a `legacy-directory-name` warning is emitted and the directory name is kept as a compatibility alias |

A description over the limit is an `error` and the skill is rejected. Keep
descriptions to a sentence or two: they are read far more often than the body.

## Directory rules

```text
skills/
├── authoring-workflows/
│   └── SKILL.md
├── connecting-mcp-servers/
│   └── SKILL.md
├── extending-with-hooks/
│   └── SKILL.md
├── headless-mode/
│   └── SKILL.md
├── kernel-events-and-reducers/
│   └── SKILL.md
├── running-the-tui/
│   └── SKILL.md
├── security-modes-and-approvals/
│   └── SKILL.md
├── subagents/
│   └── SKILL.md
├── testing-agenthicc/
│   └── SKILL.md
├── using-memory/
│   └── SKILL.md
└── README.md          # this file — informational, not a skill
```

A directory without a `SKILL.md` produces a `missing-skill-file` warning. A
non-directory entry in a skills root is skipped with an `ignored-entry` `info`
diagnostic, which is why this `README.md` is reported once per scan. That notice
is expected and harmless; it is how the loader tells you it looked at a file and
deliberately moved on.

## Discovery and precedence

The loader scans two roots and resolves conflicts by scope:

| Scope | Root | `source` |
|---|---|---|
| User | `~/.agenthicc/skills` | `user` |
| Project | `<project>/.agenthicc/skills` | `project` |

Project scope is applied second, so a project skill with the same slug
**overrides** the user skill and emits a `scope-override` warning. Two skills
claiming the same alias produce an `alias-conflict` error and the later claim is
dropped.

Change the user root with `[skills] default_skill_directory` in
`agenthicc.toml`; the default `""` means `~/.agenthicc/skills`
(`src/agenthicc/config.py:1459`). `[skills] install_default_skills` defaults to
`true` (`config.py:1458`).

## Installing third-party skills

```bash
agenthicc skills add <source>              # install into the project
agenthicc skills add <source> --global     # install into the user root
agenthicc skills add <source> --skill name1,name2
agenthicc skills add <source> --all
```

## Available skills

| Skill | Tags | Summary |
|---|---|---|
| [`running-the-tui`](running-the-tui/SKILL.md) | `tui`, `terminal`, `rich`, `slash-commands`, `approvals` | Operate the interactive Rich Live terminal workspace: layout, modes, overlays, slash commands, input handling, approvals, and background sessions |
| [`headless-mode`](headless-mode/SKILL.md) | `headless`, `json-lines`, `cli`, `automation`, `ci` | Run agenthicc non-interactively: the JSON-lines stdin/stdout contract for pipelines and CI. Alias: `headless-api` |
| [`security-modes-and-approvals`](security-modes-and-approvals/SKILL.md) | `security`, `modes`, `approvals`, `capabilities`, `permissions`, `safe`, `plan`, `yolo` | Control what the agent may do: the Safe, Plan, and Yolo modes, the capability taxonomy, and the hard-block then soft-approval gate pair |
| [`authoring-workflows`](authoring-workflows/SKILL.md) | `workflows`, `plugins`, `phasespec`, `authoring`, `registry`, `checkpoints` | Author and register workflows: the `WorkflowPlugin` contract, `PhaseSpec` fields, custom runners and checkpoint hooks, and discovery order |
| [`subagents`](subagents/SKILL.md) | `subagents`, `concurrency`, `delegation`, `spawn`, `workers` | Delegate to isolated concurrent subagents: the nine built-in types, tool ceilings, the `spawn_subagents` contract, and pool aggregation |
| [`kernel-events-and-reducers`](kernel-events-and-reducers/SKILL.md) | `kernel`, `events`, `reducers`, `effects`, `state`, `appstate` | Work with the kernel: immutable `AppState`, `Event` and `Effect`, pure reducers and the dispatch table, the `EventProcessor`, and log replay |
| [`connecting-mcp-servers`](connecting-mcp-servers/SKILL.md) | `mcp`, `integrations`, `tools`, `stdio`, `oauth` | Connect MCP servers: the `mcp` CLI subcommands, the `[[tools.mcp_servers]]` schema, transports, auth, lifecycle, and diagnosis |
| [`extending-with-hooks`](extending-with-hooks/SKILL.md) | `hooks`, `lifecycle`, `audit`, `tool-policy`, `plugins` | Implement and register `LifecycleHook` subclasses that observe, rewrite, abort, or recover tool calls |
| [`using-memory`](using-memory/SKILL.md) | `memory`, `sqlite`, `layers`, `artifacts`, `router` | Use the three-tier memory model: session, project SQLite, and global SQLite, plus artifacts and `MemoryRouter` |
| [`testing-agenthicc`](testing-agenthicc/SKILL.md) | `testing`, `pytest`, `cassettes`, `fixtures`, `replay` | Test agenthicc with its fixtures and cassette replay: `SessionCassette`, `MockApprovalService`, `run_headless_replay` |

## Writing a new skill

1. Create `skills/<kebab-case-name>/SKILL.md`. The directory name becomes the
   canonical name, so pick it deliberately — it is what users type after `$`.
2. Add frontmatter with at least `name`, `description`, `tags`, and `version`.
3. Write the body so a reader who is new to the subsystem can act on it:
   preconditions, the core types and signatures, complete runnable examples,
   common errors and their fixes, and a short key-points list.
4. Verify the skill loads cleanly:

```bash
PYTHONPATH=src python -c "
from pathlib import Path
from agenthicc.skills.loader import discover_skills_with_diagnostics

result = discover_skills_with_diagnostics(project_dir=Path('.'), user_dir=Path('/nonexistent'))
for slug, skill in sorted(result.skills.items()):
    print(f'{slug:24} name={skill.name!r} description_len={len(skill.description)}')
for diagnostic in result.diagnostics:
    print('DIAG', diagnostic)
"
```

Both arguments must be `Path` objects; the loader joins them with `/` and
passing strings raises `TypeError`. A clean scan prints one `ignored-entry`
`info` for this `README.md` and nothing else.

## Diagnostic codes

Every diagnostic carries a path, a code, a message, and a severity.

| Code | Severity | Meaning |
|---|---|---|
| `missing-skill-file` | warning | Directory has no `SKILL.md` |
| `invalid-canonical-name` | error | Directory name cannot form a valid kebab-case slug within the length limit |
| `legacy-directory-name` | warning | Directory name is not canonical; it is retained as a compatibility alias |
| `missing-frontmatter` | warning | No YAML frontmatter; fallback metadata is used |
| `invalid-frontmatter` | error | Unclosed delimiters, or the frontmatter is not a mapping |
| `invalid-yaml` | error | Frontmatter is not parseable YAML |
| `missing-yaml-dependency` | error | PyYAML is unavailable |
| `unknown-frontmatter-field` | warning | Field is not recognized and is ignored |
| `compatibility-field` | info | An accepted alias was used; prefer the canonical name |
| `duplicate-field-alias` | warning | Both a canonical field and its alias were supplied; canonical wins |
| `missing-name` | warning | `name` absent; the directory name is used |
| `invalid-name` | error | `name` is not a non-empty string |
| `missing-description` | warning | `description` absent |
| `invalid-description` | error | `description` is not a string |
| `description-too-long` | error | Description exceeds 1 536 characters |
| `invalid-alias` | error | Alias is empty or contains whitespace or `/` |
| `redundant-alias` | warning | Alias duplicates the canonical name |
| `alias-conflict` | error | Alias collides with another skill's canonical name or alias |
| `scope-override` | warning | A project skill overrides a user skill with the same slug |
| `invalid-max-turn-depth` | error | `maxTurnDepth` is not a positive integer |
| `invalid-boolean` | error | `disallowAutoTriggering` is not a boolean |
| `invalid-author`, `invalid-model` | error | Field is not a string |
| `invalid-permissions` | error | `permissions` or `permissions.agents` is not a mapping |
| `invalid-field-type` | error | A list-typed field is not a string list |
| `ignored-entry` | info | A non-directory in a skills root was skipped |
| `read-error` | error | `SKILL.md` could not be read |
| `scan-error`, `invalid-skill-root` | error | The skills root is missing, unreadable, or not a directory |

## Related

- [Documentation fact base](../docs/reference/fact-base.md) — where documented claims are verified against source
- [Configuration guide](../docs/guides/configuration.md) — `[skills]` settings and precedence
- [Extensions guide](../docs/guides/plugins.md) — tools, agents, modes, and MCP servers alongside skills
