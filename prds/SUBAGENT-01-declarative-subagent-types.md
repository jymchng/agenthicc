---
title: "SUBAGENT-01: Declarative subagent type definitions and discovery"
status: Proposed
version: 0.1.0
date: 2026-10-29
scope: "user- and project-scoped subagent type definitions, their discovery precedence, and the model-facing description used to select a type"
area: A-1
related_prds:
  - PRD-233
  - AGX-09
  - PRD-124
tags:
  - subagents
  - type-system
  - configuration
  - declarative
---

# SUBAGENT-01: Declarative subagent type definitions and discovery

## 1. Executive summary

Subagent types can only be created by writing Python, and the model is told
nothing about them beyond a bare name in a hard-coded sentence. This PRD makes a
subagent type a **declarative definition file** at user and project scope,
carrying a description the model can select on, and defines how those
definitions project into the runtime's `SubagentTypeSpec` and into the
`spawn_subagents` schema the model actually reads.

It does **not** invent a definition format. AGX-09 §5.1 already specifies one,
for agents generally, with `mode: subagent` as a supported value; this PRD
adopts that format unchanged and owns only the subagent-specific projection.

## 2. Problem statement

All claims verified at commit
`0115674bc61298975bd0f992dd6f9b78620a6138` (2026-10-29).

**The type record has no description.** `SubagentTypeSpec` is a five-field
dataclass (`src/agenthicc/subagents/types.py:60-91`) and none of the fields is a
description. The nine built-ins are constructed by hand with no description
field to set (`src/agenthicc/subagents/types.py:309`).

**The model-facing type list is a static sentence, but the enum is dynamic.**
The `spawn_subagents` docstring that the model reads hard-codes the list:

> Available agent types: explorer, planner, implementer, executor, tester,
> reviewer, documenter, verifier, researcher.

That sentence is at `src/agenthicc/subagents/tool.py:198-199`, and it is a
string literal. Meanwhile the JSON-schema enum *is* rebuilt from the live registry on
every tool construction (`src/agenthicc/subagents/tool.py:512`, called at
`:373`). So a custom type appears in the enum and never in the prose: the two
halves of the model-facing contract disagree, and neither half can say what a
type is *for*.

**The type name is a static `Literal`.** `BuiltinSubagentType` is a
`Literal[...]` of nine names (`src/agenthicc/subagents/tool.py:55`), used as the
declared type of the task field (`:82`). The runtime patches the generated
schema afterwards, which is why the dynamic enum works at all — but the source
of truth for the model's schema is still a literal in the module.

**Definitions are Python, in a plugin, into a process global.** The only
extension point is a plugin module exporting `SUBAGENT_TYPES`
(`src/agenthicc/plugins/discovery.py:163-178`), which registers each spec into
the module-level mutable `DEFAULT_REGISTRY`
(`src/agenthicc/subagents/types.py:441`). That registry is bound at import time
in four places: as a literal *default argument* in three signatures
(`src/agenthicc/subagents/pool.py:1124`, `:1591`, `:1647`) and as a `None`
default that the function body replaces with the global
(`src/agenthicc/subagents/tool.py:99`, substituted at `:180`). The set of types
a session sees therefore depends on import order and on which plugins ran first
— three cases by default binding, the fourth by fallback. §7's migration treats
them differently for that reason.

**Overwrites are silent.** `SubagentTypeRegistry.register` is
`self._types[spec.name] = spec` (`src/agenthicc/subagents/types.py:405-407`): a
second definition of the same name replaces the first and nothing records what
was replaced. Today that is merely unhelpful; once a precedence ladder exists it
would make a shadowed definition invisible, which is why detection belongs to
this PRD rather than to the UI that renders it.

**Consequence for users.** Adding a type requires authoring Python and
understanding plugin trust. There is no way for a project to ship a `.md` file
that says "we have a `migration-reviewer` type that reads SQL and diffs"; the
only path is a Python plugin that constructs a dataclass and mutates a global.

## 3. Current-source investigation

Run on 2026-10-29 against `0115674bc61298975bd0f992dd6f9b78620a6138`.

```console
$ git rev-parse HEAD
0115674bc61298975bd0f992dd6f9b78620a6138

$ grep -n "description" src/agenthicc/subagents/types.py
75:        Must be a self-contained instruction — the task description is appended
119:    "a prose description of a change you would make is not completion. "

$ grep -rn "DEFAULT_REGISTRY" --include=*.py src tests | wc -l
48

$ sed -n '198,199p' src/agenthicc/subagents/tool.py
        Available agent types: explorer, planner, implementer, executor, tester,
        reviewer, documenter, verifier, researcher.

$ grep -n "_EXTENSION_DIRS" -A9 src/agenthicc/cli/commands/trust.py
17:_EXTENSION_DIRS: dict[str, str] = {
18:    "tools": "tool",
19:    "commands": "command",
20:    "workflows": "workflow",
21:    "modes": "mode",
22:    "agents": "agent",
23:    "cli": "cli",
24:}

$ grep -rnE "\.md|markdown|frontmatter|yaml" src/agenthicc/subagents/
src/agenthicc/subagents/tool.py:440:    return hashlib.md5(json.dumps(entries, ensure_ascii=False).encode()).hexdigest()[:16]

$ sed -n '405,407p' src/agenthicc/subagents/types.py
    def register(self, spec: SubagentTypeSpec) -> None:
        """Register or replace a subagent type."""
        self._types[spec.name] = spec
```

Absence claims, with the search that establishes them:

- **No description *field* in the type layer.** Pattern `description`, scope
  `src/agenthicc/subagents/types.py`: two matches, both prose rather than a
  field — `:75` inside the `system_prompt` docstring and `:119` inside a prompt
  string literal. Neither is an attribute of `SubagentTypeSpec`, so the record
  still has no place to put a description. The file is 441 lines, so this is an
  exhaustive result for that file, not a sample.
- **No Markdown/declarative loader for types.** Pattern
  `\.md|markdown|frontmatter|yaml`, scope `src/agenthicc/subagents/`, returns
  exactly one match: `tool.py:440`, which is `hashlib.md5` in a cache-key hash
  and therefore a false positive on the `.md` alternative. No frontmatter,
  Markdown or YAML reader exists in the package, so there is no loader for
  declarative type definitions. Transcript writing is not the exception it might
  look like: `transcripts.py:3-4` records that a pool writes `pool.json` and one
  `<worker_id>.jsonl` per worker — JSON and JSONL, no Markdown.
- **No `subagents` trust kind.** `src/agenthicc/cli/commands/trust.py:17-24`
  lists six extension kinds and `subagents` is not among them. Definitions
  therefore cannot live in a `.agenthicc/subagents/` directory under today's
  trust model without a new entry.

The commit quoted above is the one HEAD pointed at while the checks ran; this
document was then committed as `61c81c66604f40cbf5007209b5b0992df2f275c1`, and
`git diff --name-only 0115674 61c81c6` lists this file and nothing else, so every
claim here holds at both commits.

Diff against the research report's `f843040` snapshot: **nothing has moved** for
these claims. The report's §1.3, §1.9 (G-1) and §5.3 (A-1) are still accurate at
this commit.

One correction to the report, recorded here because the PRD follows the source:
§2.5 of the report describes opencode's `mode` field as answering "may this
agent be spawned?". That is right, and the same field name in AGX-09 §5.1
(`mode: primary | subagent | all`) carries the same meaning, which is why this
PRD can reuse it rather than inventing an `is_subagent: bool`.

## 4. Goals

1. A subagent type is defined by a Markdown file with YAML frontmatter, at
   project scope (`.agenthicc/agents/`) and user scope
   (`~/.agenthicc/agents/`), using the AGX-09 §5.1 format unchanged.
2. Each definition carries a `description`, and that description reaches the
   model: in the tool description *and* in the per-type enumeration, so the two
   halves of the model-facing contract cannot disagree again. (Gap G-1)
3. The nine built-ins remain available and become the lowest-precedence source;
   project definitions override user definitions, which override built-ins.
4. `SubagentTypeSpec` carries `description` as a field, so the description has a
   runtime home and is not re-read from disk on every spawn.
5. The registry a session uses is explicit, not a mutated process global; the
   existing plugin `SUBAGENT_TYPES` export keeps working through a documented
   compatibility path.
6. Loading **reports shadowing and refuses collisions**: when a project
   definition overrides a user definition or a built-in, the winning source and
   the shadowed source are recorded on the load result and reported once; two
   definitions of the same name *within one directory* are an error naming both
   files. (Research report §5.3 A-1 absorbs M-2: detection and attribution are
   here, rendering is AGX-09's.)
7. A definition may not silently override a built-in it did not intend to: a
   project definition that shadows a built-in name does so only with an explicit
   `override: true` key, otherwise it is refused with the built-in named in the
   message.

## 5. Non-goals

These are the A-1 boundaries from the research report §5.3, restated as
commitments:

- **What a type may do with its tools.** A definition's `tools:` key remains a
  ceiling with today's semantics; deny-lists, approval overrides and per-type
  narrowing are SUBAGENT-05 (A-5).
- **Which model a type runs on.** `model:` in the file is parsed and preserved
  but does not change worker routing until SUBAGENT-02 (A-2). Until then a
  definition that sets `model:` is accepted and warned about as not yet
  effective — it is not silently dropped.
- **The registry's runtime lookup API.** `SubagentTypeRegistry.get`,
  `register`, `names` and `__contains__` stay as they are; this PRD changes who
  builds registries and where definitions come from, not how lookup works.
- **The Library tab, editing, hot reload and definition *rendering*.** AGX-09
  owns those. This PRD produces the shadowing and collision report on the load
  result (§4 goals 6-7); it does not design the surface that displays it.
- **Removing Python plugins.** `AgentPlugin` and `SUBAGENT_TYPES` remain valid
  ways to ship types that need code.
- **Policy semantics and the mode ceiling.** PRD-233 owns the profile contract;
  `ceiling:` in a definition is honoured only once PRD-233 lands.

## 6. Acceptance criteria

Each criterion names the check that decides it.

1. A `.agenthicc/agents/<name>.md` file with valid frontmatter is discovered and
   spawnable by name: `pytest tests/unit/test_subagent_pool.py -q` stays green
   and a new case in the same file asserts the declared type is in
   `registry.names()`.
2. The `spawn_subagents` description lists each declared type **with** its
   description text, and the JSON-schema enum contains the same names: asserted
   by `pytest tests/unit/test_subagent_plugin.py -q` extended with a
   description-visibility case.
3. A project definition overrides a user definition of the same name, and both
   override a built-in: `pytest tests/unit/test_subagent_pool.py -q`.
4. An invalid definition (unknown key, missing `description`, unparsable YAML)
   is rejected with the file path and line, and is **not** registered:
   `pytest tests/unit/test_subagent_plugin.py -q`.
5. `SubagentTypeSpec(...).description` exists and defaults to `""` for the
   existing plugin path, so no current plugin breaks:
   `pytest tests/unit/test_subagent_plugin.py -q`.
6. A plugin exporting `SUBAGENT_TYPES` still registers its type:
   `pytest tests/unit/test_subagent_plugin.py -q`.
7. A type named `research` is accepted as a real name and no longer collides
   with the `research` → `researcher` alias
   (`src/agenthicc/subagents/tool.py:70`): a case in
   `tests/unit/test_subagent_pool.py`.
8. `pytest tests/integration/test_subagent_tool_execution.py -q` passes
   unchanged, proving the tool boundary did not shift.
9. A shadowed definition is reported with both sources, and two definitions of
   the same name inside one directory are refused naming both files:
   `pytest tests/unit/test_subagent_plugin.py -q`.
10. A project definition that shadows a built-in without `override: true` is
    refused with the built-in named; with the key set the project definition
    wins and the shadowing is reported:
    `pytest tests/unit/test_subagent_plugin.py -q`.

## 7. Rollout, migration and security

**Migration.** `DEFAULT_REGISTRY` is referenced in 48 places across `src/` and
`tests/`. The migration is staged so no caller has to change in the same
release:

1. Add `description` to `SubagentTypeSpec` with a default, so every existing
   construction site keeps working.
2. Add the loader and make the *callers that construct sessions* build a
   registry from (built-ins ⊕ user ⊕ project) instead of taking the default.
3. Keep `DEFAULT_REGISTRY` exported as a built-ins-only registry for one
   release, with a deprecation note, because `tests/unit/test_subagent_pool.py`
   and `tests/unit/test_subagent_plugin.py` import it directly and plugins
   register into it today.
4. Only then remove the import-time bindings: the three `DEFAULT_REGISTRY`
   default arguments in `pool.py`, and the `None` fallback at
   `tool.py:180`, which is a one-line change and can go last.

**Compatibility.** A plugin that registers into `DEFAULT_REGISTRY` after the
session registry has been built would be invisible to that session. The loader
therefore merges plugin-registered types into the session registry at
construction time and logs a deprecation when a plugin uses the global path.

**Security.** A definition file is data, but it carries a system prompt and a
tool ceiling, and project-scope definitions arrive with a cloned repository.
Three rules:

- Project-scope definitions load only when the project's extensions are trusted,
  using the existing extension-trust machinery
  (`src/agenthicc/cli/commands/trust.py:17-24`). Loading a prompt from an
  untrusted checkout is the same threat as executing a plugin from it, and is
  refused with a visible reason rather than silently skipped.
- Fail closed: an unparsable or invalid definition is not registered and the
  spawn of that name fails with the file path; there is no fallback to a
  broader built-in.
- A definition may narrow tools (`tools:` is intersected with the session's
  available tools, as `allowed_tools` already is) and may never widen them. The
  widening rules themselves are SUBAGENT-05.

**The definition-format key set, and who owns each key.** AGX-09 §5.1 fixes the
frontmatter key list and makes an unknown key a validation error rather than an
ignored field (`prds/agx-09-agent-library-and-switching.md:92`). That rule is
PRD-250's inert-field rule pointing the other way: a field that is accepted but
never read is a defect (`prds/prd-250-code-quality-evaluation-and-improvement-programme.md:244`),
so a key the loader does not recognise must not be quietly absorbed
(`:245`). Four PRDs in this series extend that key list, and each says so in its
own §7. Left in those places alone, the format would be extended independently
four times with no statement of the result. The union is recorded here, once;
the other PRDs cite this table rather than restating it.

| Key | Owner | In AGX-09 §5.1 today | Absent means | What the `:92` rule requires |
|---|---|---|---|---|
| `description` | SUBAGENT-01 §4 goals 2 and 4 | **yes, and required** (`prds/agx-09-agent-library-and-switching.md:78`, `:91`) | required in a definition file; on the runtime record `SubagentTypeSpec.description` defaults to `""` so plugin-registered types keep working (§7 migration step 1) | nothing — the key is already known. What changes is the runtime home and the model-facing projection, not the format |
| `override` | SUBAGENT-01 §4 goal 7 | **no** | `false`: a project definition whose name shadows a built-in is refused, naming the built-in | add it to the key list, the schema and the validator in the same change as SUBAGENT-01. Until then every definition that sets it is refused as an unknown key, which makes the shadowing refusal indistinguishable from a typo |
| `effort` | SUBAGENT-02 §4 goal 2, §7 | **no** | inherit the parent's resolved provider options; an absent key adds no effort entry, exactly as today | same, in the same change as SUBAGENT-02 |
| `context` | SUBAGENT-02 §4 goal 3, §7 | **no** | 8,000 tokens — `ShortTermMemory(max_tokens=8_000)` (`src/agenthicc/subagents/pool.py:960`), observed through the probe wrapper at `:958` | same, in the same change as SUBAGENT-02 |
| `output` | SUBAGENT-03 §4 goal 1, §7 | **no** | no contract: plain-text `SubagentResult.text`, no validation, no parser | same, in the same change as SUBAGENT-03 |
| `permissions` | SUBAGENT-05 §4 goal 1, §7 | **no** | no block: today's effective scope and an unchanged `policy_revision` | same, in the same change as SUBAGENT-05. Nested on purpose, so the next permission field extends a block instead of arriving as another key |

The other eight keys — `name`, `mode`, `model`, `tools`, `max_steps`, `ceiling`,
`color`, `hidden` (`prds/agx-09-agent-library-and-switching.md:77-85`) — stay
exactly as AGX-09 writes them. `mode:` is what makes a definition a subagent type
at all (`mode: subagent`, reused here rather than replaced by an `is_subagent`
flag, §3), and `model:` gains *behaviour* in SUBAGENT-02 §4 goal 1 without
becoming a new key. SUBAGENT-04 §4 goal 10, SUBAGENT-06 §7, SUBAGENT-07,
SUBAGENT-08 and SUBAGENT-09 add none: their subjects are pools, calls, workers
and transcripts, not type records. **After the series the key set is fourteen
keys — AGX-09's nine plus the five above that AGX-09 does not already have.**
SUBAGENT-04 §7 and SUBAGENT-06 §7 had each counted three extending PRDs, because
both were written before SUBAGENT-05 and neither counted `permissions:`; both now
cite this table instead of carrying a count.

The table sits in §7 rather than in a second appendix because the research
report's contract allows nine numbered sections and one appendix, and its §6.7
item 5 fails a document carrying two; defaults and the migration of existing
configuration are §7's own subject in any case.

Three consequences, and they are the reason the table exists at all:

- **One change, not five.** The key list, the schema and the validator are one
  artefact. "Add the key" therefore means "land the key and its reader in the
  same release". A key accepted a release before its reader exists is precisely
  the inert-field defect PRD-250 records for settings
  (`prds/prd-250-code-quality-evaluation-and-improvement-programme.md:244`),
  applied to definitions.
- **Refused beats ignored, and this PRD does not soften it.** Because the
  unknown-key rule makes these errors, a definition using a key whose owning PRD
  has not landed fails at load with the file path — the correct outcome, and the
  reason no key here may be introduced as "accepted and ignored". The one
  existing key that cannot take effect until SUBAGENT-02 lands is `model:`,
  which §5 *warns* about instead of dropping.
- **An unknown value is a different axis from an unknown key.** The unknown-key
  rule governs keys only. A known key with an unusable value fails closed at the
  owning PRD's load check — `model:` and an unrecognised `effort:` (SUBAGENT-02
  §6 criterion 8), `output:` (SUBAGENT-03 §6 criterion 7), `permissions:`
  (SUBAGENT-05 §6 criterion 8) — with two deliberate exceptions that are
  quality-of-execution knobs rather than identity or permission claims: an
  `effort:` the named model does not support warns once and runs at the model's
  default (SUBAGENT-02 §7), and a `context:` larger than the model's resolved
  window is clamped rather than refused (SUBAGENT-02 §6 criterion 5), because an
  oversized budget bounds memory, it does not misreport which model ran.

## 8. Verification

Current suite that must stay green:

```console
$ pytest tests/unit/test_subagent_pool.py -q
$ pytest tests/unit/test_subagent_plugin.py -q
$ pytest tests/unit/test_subagent_pool_runtime_coverage.py -q
$ pytest tests/integration/test_subagent_tool_execution.py -q
$ pytest tests/integration/test_subagent_executor_integration.py -q
```

New cases for this PRD are added to the first two files rather than in a new
module, because they exercise the same registry and plugin surfaces those files
already cover.

## 9. Related and superseded PRDs

- **AGX-09 — extends, does not duplicate.** AGX-09 §5.1 owns the Markdown
  format, the `.agenthicc/agents/` locations, the Library UX and hot reload.
  This PRD consumes that format as-is and adds only the subagent projection. If
  AGX-09 ships first, this PRD is the adapter; if this PRD ships first, it must
  implement AGX-09's format exactly as written there so AGX-09 does not later
  have to migrate files. Two definition formats is the failure mode. The key set
  this series adds to that format is tabulated in §7, because AGX-09's
  unknown-key rule makes each addition a change to AGX-09 and not only to the
  PRD that wants the key.
- **PRD-233 — supersedes for the subagent type record only.** PRD-233 §4.1
  proposes a versioned profile schema with `description`, `role`, prompt source,
  tool allowlists and budgets, and §4.2 a precedence order. For *subagent
  types*, this PRD is the narrower and more specific statement, and PRD-233
  should be amended to point here rather than define a second type record. For
  *primary agent profiles*, policy ceilings and approval semantics, PRD-233
  remains authoritative. The precedence order in PRD-233 §4.2 (managed >
  project > user > built-in) is adopted unchanged; this PRD does not restate
  its security reasoning.
- **PRD-124 — inherits.** PRD-124 Phase 5 introduced the `SUBAGENT_TYPES` plugin
  export (`src/agenthicc/plugins/discovery.py:163`). That path is preserved, not
  removed, and is the compatibility story in §7.

## A. Competitor evidence

| Harness | Evidence | Depth | What it does | Take or reject |
|---|---|---|---|---|
| opencode | `packages/schema/src/agent.ts:23-35` @ `726cf096396d5014c3b1845e231cc01efe0421ee` | code | `Agent.Info` has `description?` and `mode: subagent \| primary \| all` on one typed record | Take both: a description on the record and a mode field instead of a boolean |
| Claude Code | `https://code.claude.com/docs/en/sub-agents` fetched 2026-10-29 | docs-only | Markdown + YAML frontmatter; name from frontmatter, not filename; selection driven by `description`; combined descriptions metered at 15,000 tokens | Take the format and the description-driven selection; take the metering as a reason to keep descriptions short |
| Qoder CLI | `https://docs.qoder.com/cli/subagent` fetched 2026-10-29 | docs-only | Five-tier definition precedence (built-in, user, project, plugin, flag) with shadowing marked in the panel | Take the precedence ladder; the shadowing indicator belongs to AGX-09's UI |
| Grok CLI | `src/utils/settings.ts:109-112` @ `fb97af83f06dca873281d60168430f06c8de6324` | code | A user subagent is exactly `{name, model, instruction}` | Reject as a model: no description and no tool scoping is the weakness this PRD fixes |
| Codex CLI | `codex-rs/protocol/src/agent_path.rs:19-23` @ `9cc14e79882ff8885cd5d33b4adadae014045aef` | code | Agent identity is a validated path tree (`/root`, `/morpheus`), not a flat name | Reject for this area: a path tree is an addressing scheme, and flat names plus `mode` are enough to select a type |
