---
title: "WFC-09: create_skills — a workflow that authors one or several skills"
status: Proposed
version: 1.0.0
date: 2026-11-22
repository: jymchng/agenthicc
priority: P1
related_prds: [WFC-00, WFC-01, WFC-02, WFC-05, WFC-10, WFC-11, WFC-14, prd-22, prd-23, prd-142, prd-147]
tags: [skills, workflows, authoring]
---

# WFC-09 — `create_skills`

## 1. Summary

Add a built-in workflow, **`create_skills`**, that designs, writes, validates and
stages one or more skills (`.agenthicc/skills/<slug>/SKILL.md` plus optional
support files). It returns a typed `WorkflowReturn` of staged skill artifacts;
the session publishes them (WFC-11), reloads skills and their `$slug` commands
(WFC-05), and the agent can use them straight away (WFC-10).

## 2. Problem (current source)

- Skills are discovered from `.agenthicc/skills/` and `~/.agenthicc/skills/`
  by `discover_skills_with_diagnostics` (`skills/loader.py:581`), parsed by the
  **private** `_parse_skill_detailed` (`:279`) into `SkillDef` (`:88`) with
  frontmatter fields `name, description, tags, suggested_topics, tools,
  disabled_tools, max_turn_depth, model, aliases, allowed_agents,
  denied_agents, disallow_auto_triggering`.
- The only authoring help is prompt-only bootstrap skills (`create-workflow`,
  `create-tools`, `create-commands`, `skills/bootstrap.py`); there is none for
  skills, and a skill written by hand today has no validation beyond
  discovery diagnostics at the next load.
- A malformed skill is discovered late and quietly (a `SkillDiagnostic`), and a
  name or alias that collides with a command is reported only after
  `_reload_skills` has already torn down and rebuilt the skill commands
  (`tui_session.py:4115-4160`).
- The skills runtime renders `!`command`` placeholders as *instructions* for the
  agent, never executes them (`skills/runner.py`), so skill text is not code —
  but it is a persistent prompt, which makes authored skills a prompt-injection
  vector once they are published automatically (WFC-11).

## 3. Goals

1. An agent (or a person) can create one or several coherent skills in one run,
   with deterministic validation, published automatically when it passes.
2. Validation uses the **same parser** that loads skills, so "validated" means
   "will load".
3. Skills are usable in the same session as soon as they are published and
   reloaded.
4. Authored skills carry provenance and can be listed, diffed and removed
   (WFC-11).

## 4. Non-goals

- Installing third-party skills (`skills/installer.py`).
- Executing skill context commands.
- A general prompt-quality evaluator; checks here are structural and policy.

## 5. Design

### 5.1 Public validation API

Promote the parser to a public, typed function in `skills/loader.py`:

```python
@dataclass(frozen=True, slots=True)
class SkillValidation:
    skill: SkillDef | None
    diagnostics: tuple[SkillDiagnostic, ...]
    @property
    def ok(self) -> bool: ...      # skill is not None and no error-severity diagnostic

def validate_skill_dir(path: Path, *, source: str) -> SkillValidation: ...
```

`_parse_skill_detailed` stays the implementation; `_parse_skill` and discovery
call the same code. A `llms-full.txt` entry is added (public symbol).

### 5.2 Phases

`design → write → validate → finalize`.

| Phase | Behaviour |
|---|---|
| design | The agent reads existing skills (`list` via the live surface), decides the set, and calls `submit_skills_plan(skills: list[SkillPlanInput])`. Each plan: `slug`, `name`, `description`, `when_to_use` (becomes `suggested_topics`), `tools`/`disabled_tools` narrowing, `argument_hints`, `update: bool`. |
| write | Writes each skill to `.agenthicc/.staging/<run_id>/skill/<slug>/SKILL.md` (+ `template.md`, `reference.md` if planned) through the WFC-14 scoped writer: runner-derived root, `.md` suffix only, ≤ 12 skills, ≤ 7 files each; the runner scans the root for the manifest. |
| validate | The deterministic checks (§5.3) run in the runner and produce a WFC-14 `ValidationReport`; the agent may explain and request repair but, by WFC-14 `decide`, cannot turn a report with an error into a pass. |
| finalize | Builds the return. |

The run is bounded by WFC-14's persisted `RepairBudget` (§5.4 there).

### 5.3 Deterministic checks

For every skill and for the set:

1. `validate_skill_dir(...)` returns `ok` with no error diagnostics.
2. `slug` matches `[a-z0-9][a-z0-9-]{0,63}`, unique in the set, and does not
   collide with a built-in or existing live skill unless `update: true`.
3. Its `$slug` command and every alias do not collide with any registered
   command or alias (the check `_reload_skills` currently does after the fact,
   moved earlier through `ReloadService.preview({"skills","commands"})`).
4. `description` ≤ 300 characters, non-empty; `suggested_topics` ≤ 12 words, no
   empty entries — so auto-triggering stays meaningful.
5. `tools` / `disabled_tools` name tools that exist on the current surface, and a
   skill may only *narrow* the tool set (CLAUDE.md "child scopes only restrict").
6. No `model` override unless the plan states why (it makes the skill
   user-activation only, WFC-10 §5.4).
7. Body size ≤ 32 KB; support files ≤ 5, each ≤ 64 KB; no symlinks; no path
   escapes.
8. **Advisory lint**, shown in the publish notice and recorded in the manifest
   rather than failing the run: text that
   asks the agent to ignore or disable approvals, sandboxing or the capability
   ceiling, to exfiltrate data, or to fetch and run remote content.

### 5.4 Placement and publication

Skills are always project-scoped: `.agenthicc/skills/<slug>/`. Authoring never
writes `~/.agenthicc/skills/` (outside the workspace, and trusted by location —
WFC-11 §5.4); a person can move a skill there by hand. Skills are text, so they
receive no trust record, but they go through the same automatic publish step as
every other kind (WFC-11 §5.2): gate, commit, reload, provenance entry
(`approved_by: "policy"`) and notice, with no prompt.

### 5.5 Return and activation

```python
WorkflowReturn(
    status="completed",
    summary="Created 2 skills: release-notes, changelog-audit",
    outputs={"skills": "release-notes,changelog-audit"},
    artifacts=(PublishedArtifact(kind="skill", name="release-notes", …),),
)
```

On pop the session runs the WFC-11 §5.2 publish step with
`reload({"skills","commands"})` (skill commands register through the command
registry, so both surfaces), then delivers. The resume
turn lists each skill with its `$slug` command, its `when_to_use`, and whether
it is model-invocable (WFC-10).

### 5.6 Invocation declaration

`CreateSkills.invocation = WorkflowInvocation(agent_callable=True,
subagent_eligible=True, publishes=("skill",), when_to_use="Create reusable
skills (instructions the agent can load on demand) for this project",
returns="Names, slugs and commands of each skill created")`.

## 6. Requirements

- **FR-1** A skill passes `validate_skill_dir` at validate time if and only if it
  loads at the next reload (property test over generated skills).
- **FR-2** A set with any failing skill publishes none.
- **FR-3** Updating an existing skill keeps the previous version for rollback
  (WFC-11 §5.6).
- **FR-4** The workflow never writes outside the staging root; the model chooses
  no destination.
- **FR-5** The bootstrap docs skill set gains nothing new; the prompt-only
  authoring skills for tools and workflows are removed by WFC-06/07.

## 7. Acceptance criteria

| # | Criterion |
|---|---|
| A1 | A scripted run creates two skills; with no prompt, `ctx.skills` contains both and `/` completion lists their `$slug` commands. |
| A2 | A skill whose alias collides with `/help` fails validation before any file leaves staging. |
| A3 | A skill that widens tools (names a tool the parent set excludes, or sets a non-narrowing field) fails. |
| A4 | A malformed frontmatter fails with the loader's own diagnostic text. |
| A5 | An advisory-lint hit appears in the publish notice and the manifest and does not fail the run. |
| A6 | Under policy `stage_only`, `/authored reject <run-id>` leaves no file in `.agenthicc/skills/` and no manifest entry. |
| A7 | Property test: 200 generated skills, `validate_skill_dir(...).ok` equals "discovered by `discover_skills_with_diagnostics` without error". |

## 8. Test plan

- Unit: plan validation, slug/alias rules, lint table, size bounds
  (`tests/unit/test_create_skills_validation.py`).
- Integration: staged write → preview → publish → reload with the real loader
  and command registry (`tests/integration/test_create_skills.py`).
- E2E: WFC-13 S5.

## 9. Documentation and cleanup

`docs/guides/` skills documentation (`plugins.md`, `commands.md`),
`skills/` repository guide, `README.md` section on skills, `llms-full.txt`
(`validate_skill_dir`, `SkillValidation`, the workflow's public symbols).
Registry: add `create_skills` to `_BUILTIN_WORKFLOW_DESCRIPTORS`.

## 10. Risks

| Risk | Mitigation |
|---|---|
| Persistent prompt injection through an authored skill. | Provenance, `/authored remove` and `rollback`, source-labelled bodies (WFC-10), advisory lint in the notice, and the opt-in `stage_only` policy. |
| Over-broad `suggested_topics` makes a skill fire on everything. | Word and count limits in §5.3. |
| Parser divergence between validate and load. | One parser; FR-1 property test. |
