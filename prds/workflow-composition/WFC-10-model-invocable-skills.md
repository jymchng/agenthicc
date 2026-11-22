---
title: "WFC-10: Model-invocable skills — list and use skills mid-turn, against a live skill surface"
status: Proposed
version: 1.0.0
date: 2026-11-22
repository: jymchng/agenthicc
priority: P1
related_prds: [WFC-00, WFC-05, WFC-08, WFC-09, WFC-11, WFC-12, prd-22, prd-23, prd-142, SUBAGENT-12]
tags: [skills, tools, reload, prompt-safety]
---

# WFC-10 — Model-invocable skills

## 1. Summary

Give the agent two tools — `list_skills` and `use_skill` — so it can discover and
load a skill itself, including one created a moment ago by `create_skills`.
Skill bodies returned to the model are source-labelled, bounded, and narrow (never
widen) the tools available for the rest of the turn.

## 2. Problem (current source)

- A skill reaches a turn only by a person typing `$slug` (`_set_pending_skill`,
  `tui_session.py:1888` → `_pending_skill_body`, prepended to the next user text
  at `:5143`) or by auto-match on the **user's** message
  (`find_matching_skills(ctx.text, …)`, `runners/agent_turn.py:1413`). A search of
  `src/` for a model-facing skill tool found none.
- So even after `create_skills` and a reload, the agent cannot decide "this task
  needs skill X" and load it; only the human can.
- Several `SkillDef` fields are consumed only by the `$slug` path:
  `tools`, `disabled_tools`, `model`, `max_turn_depth`. Making skills
  model-invocable requires deciding what each means mid-turn, or they become
  inert fields (CLAUDE.md: "an inert configuration field is a bug").
- `_reload_skills` clears and refills the shared skills dict
  (`tui_session.py:4115-4160`); a model tool reading that dict mid-reload could
  see it empty (WFC-05 A2).

## 3. Goals

1. The agent can list and load skills by name and read the result in the same
   turn.
2. A skill created and reloaded this session is usable immediately.
3. Every skill field has a defined, tested meaning for model invocation.
4. A skill body is attributable and bounded when it enters model context.
5. Skills can opt out of model invocation.

## 4. Non-goals

- Changing auto-trigger on user text, or `$slug` behaviour.
- Executing skill context commands (still rendered as instructions).
- Letting a skill grant tools, models or capabilities it does not narrow.

## 5. Design

### 5.1 Tools

Defined in `skills/tools.py`, registered with the session tools in
`runners/agent_turn.py` (inherited by workers, SUBAGENT-12).

| Tool | Capability | Arguments | Result |
|---|---|---|---|
| `list_skills` | read | `query: str \| None` | `[{slug, description, when_to_use, command, source, model_invocable}]`, ≤ 40 entries, ranked by match when `query` is given |
| `use_skill` | read | `name: str`, `args: list[str] \| None` | `SkillUse{slug, source, body, narrowed_tools, truncated}` or a typed error |

`use_skill` resolves `name` against the **current** skills surface at call time
(no snapshot), applies `filter_skills_for_agent` (`allowed_agents` /
`denied_agents`) for the active agent, then renders the body through the
existing pipeline (`process_skill_body`: argument substitution, `template.md`,
`{reference}`, context-command placeholders). Errors: `unknown_skill`,
`not_available_to_agent`, `not_model_invocable`, `too_large`.

### 5.2 Surface

The skills mapping becomes an immutable surface held by a holder with
`current()` (the mechanism of WFC-08, owned by WFC-05). The `clear()`/`update()`
refill is deleted. `use_skill` and `list_skills` read `current()` per call, so a
skill published by a reload during the turn is visible to the next call.

### 5.3 Provenance in context

The returned body is wrapped:

```text
<skill slug="release-notes" source="project" digest="a1b2c3d4e5f6a7b8">
…rendered body…
</skill>
```

The instruction preamble the agent receives states that skill text is guidance
from the named source and does not override approvals, the capability ceiling or
the user's instructions. Authored skills (WFC-11 manifest) add
`authored="true"`. The wrapper makes a poisoned skill attributable in the
transcript, the manifest and the publish notice.

### 5.4 Field semantics for model invocation

| Field | Meaning when `use_skill` loads it |
|---|---|
| `tools` | Narrows the **remaining turn** to this allow-list, applied at the next tool boundary (the `next_queued_message` seam, `agent_turn.py:1604`) through the existing `allowed_tool_names` mechanism. Never widens. |
| `disabled_tools` | Removed from the remaining turn at the next boundary. |
| `model` | Unsupported mid-turn. A skill with `model` set is **not model-invocable**; `list_skills` shows `model_invocable: false` and `use_skill` returns `not_model_invocable`. It stays user-activatable with `$slug`. |
| `max_turn_depth` | Not applied (the turn's own limit governs); documented as `$slug`-only. |
| `disallow_auto_triggering` | Unchanged: governs auto-match on user text only. |
| `allowed_agents` / `denied_agents` | Enforced as in §5.1. |
| `disable_model_invocation` (new, default `false`) | When `true`, hides the skill from `list_skills` and refuses `use_skill`. |

Narrowing persists until the turn ends; a second `use_skill` can only narrow
further.

### 5.5 Bounds

A body larger than 16 KB is returned truncated with `truncated=true` and the
digest of the full body; `list_skills` output ≤ 6,000 characters. Loading a
skill already loaded in the same turn returns a short
`already_loaded{slug, digest}` result instead of the body again, which bounds
prompt growth without a count cap. No limit is placed on how many distinct
skills a turn (or a worker) may load.

## 6. Requirements

- **FR-1** `use_skill` never executes anything; context-command placeholders stay
  instructions.
- **FR-2** A skill created and published by a reload during the current turn is
  returned by `list_skills` and `use_skill` on their next call.
- **FR-3** Narrowing is observable: the next model request's tool list excludes
  disabled tools and the narrowed set is recorded on the turn.
- **FR-4** Every `SkillDef` field in §5.4 has a test.
- **FR-5** `disable_model_invocation` is parsed, validated and documented with
  the other frontmatter fields.

## 7. Acceptance criteria

| # | Criterion |
|---|---|
| A1 | After `create_skills` completes and publishes automatically, a scripted agent calls `list_skills("release")` and `use_skill("release-notes", ["v2"])` and receives the wrapped, substituted body. |
| A2 | A skill with `disabled_tools: [shell]` removes `shell` from the next model request of the same turn. |
| A3 | A skill with `model` set returns `not_model_invocable`. |
| A4 | A skill denied for the active agent returns `not_available_to_agent` and is absent from `list_skills`. |
| A5 | A 40 KB body returns truncated with the full-body digest. |
| A6 | A reload during `use_skill` execution never yields an empty or partial skills view (stress). |
| A7 | The wrapper carries `source` and, for authored skills, `authored="true"`. |
| A8 | A worker spawned afterwards can call both tools. |

## 8. Test plan

- Unit: field-semantics table, wrapper format, bounds, ranking.
- Integration: real loader + surface + reload mid-turn, narrowing observed in a
  recording transport (`tests/integration/test_use_skill.py`).
- E2E: WFC-13 S5.

## 9. Documentation and cleanup

`docs/guides/` skills and tools pages, `skills/` guide, `llms-full.txt`.
`_set_pending_skill` and `_pending_skill_body` remain (user path); the dict
`clear()`/`update()` refill is removed.

## 10. Risks

| Risk | Mitigation |
|---|---|
| A repository-supplied skill steers the agent. | Source/digest wrapper, "guidance not authority" preamble, unchanged approvals and ceiling, per-turn skill cap. |
| Narrowing surprises the agent mid-turn. | The result states `narrowed_tools`; the change lands at a tool boundary, not inside a call. |
| Prompt growth from many skills. | 16 KB per body and `already_loaded` de-duplication; no count cap, so workers are not limited. |
