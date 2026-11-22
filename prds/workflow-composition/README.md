# Workflow composition and agent self-extension (WFC series)

The PRDs that let an agent **enter a workflow and return from it**, **spawn
subagents that run a workflow**, and **extend itself** — create workflows,
tools and skills, have them reloaded, and use them in the same session — under
a trust model where a human decides what code runs. Start with
[WFC-00](WFC-00-programme-and-study.md): it holds the current-source findings,
the architecture, the dependency table and the delivery order.

| PRD | Title | Outcome | Priority |
|---|---|---|---|
| [WFC-00](WFC-00-programme-and-study.md) | Programme and study | all (evidence, order, cross-cutting rules) | P0 |
| [WFC-01](WFC-01-workflow-call-stack.md) | A workflow call stack — enter a workflow, then return to the one you left (implemented) | 1 | P0 |
| [WFC-02](WFC-02-model-facing-workflow-tools.md) | Model-facing workflow tools and `WorkflowInvocation` (implemented) | 1 | P0 |
| [WFC-03](WFC-03-durable-stack-events-and-resume.md) | A durable stack — kernel events, checkpoint links, exactly-once return | 1, 6 | P1 |
| [WFC-04](WFC-04-workflow-running-subagents.md) | Subagents that run a workflow | 2 | P1 |
| [WFC-05](WFC-05-session-reload-service.md) | One session reload service for tools, skills, workflows and commands (implemented) | 3, 4, 5 | P0 |
| [WFC-06](WFC-06-create-workflow-activate-complete-reload.md) | `create_workflow`: activate, complete, trust, reload, use (implemented) | 3 | P1 |
| [WFC-07](WFC-07-create-tools-workflow.md) | `create_tools` — rename `make_agenthicc_tool`; one or several tools per run | 4 | P0 |
| [WFC-08](WFC-08-live-tool-surface.md) | A live tool surface — new tools callable on the next request | 4 | P0 |
| [WFC-09](WFC-09-create-skills-workflow.md) | `create_skills` — author one or several skills | 5 | P1 |
| [WFC-10](WFC-10-model-invocable-skills.md) | Model-invocable skills — `list_skills` / `use_skill` on a live surface | 5 | P1 |
| [WFC-11](WFC-11-authored-extension-trust-and-provenance.md) | Automatic trust, provenance and rollback for agent-authored extensions | 3, 4, 5, 6 | P0 |
| [WFC-12](WFC-12-capability-catalog-and-operator-visibility.md) | A capability catalog for the agent; operator visibility | 6 | P1 |
| [WFC-13](WFC-13-end-to-end-acceptance-suite.md) | End-to-end acceptance suite | all | P0 |
| [WFC-14](WFC-14-shared-authoring-contract.md) | A shared authoring contract — runner-owned validation, scoped generation, bounded repair | 3, 4, 5 | P0 |

The six outcomes (from WFC-00 §1): **1** enter-and-return; **2** subagents run a
workflow; **3** `create_workflow` then reload; **4** `create_tools` then reload
and use; **5** `create_skills` then reload and use; **6** the enhancements these
need to be safe and legible.

## Order

Full dependency table and rationale: [WFC-00 §5](WFC-00-programme-and-study.md).

| Step | PRDs | Unlocks |
|---|---|---|
| 0 | Three spikes (tool-list swap, `dict[str, str]` tool schema, sandboxed import harness) | design branches decided |
| 1 | WFC-14 ‖ WFC-05 ‖ WFC-13 harness | foundations |
| 2 | WFC-11 ‖ WFC-10 | publish/trust/provenance; agent-usable skills |
| 3 | WFC-01 (+ pop → publish connection) | return to the previous workflow |
| 4 | WFC-02 | **outcome 1** — enter a workflow, return |
| 5 | WFC-06 | **outcome 3** — `create_workflow`, reload, use |
| 6 | WFC-08, then WFC-07 | **outcome 4** — `create_tools`, use immediately |
| 7 | WFC-09 | **outcome 5** — `create_skills`, use |
| 8 | WFC-04 ‖ WFC-03 | **outcome 2** — subagents run a workflow; durable stack |
| 9 | WFC-12, WFC-13 close-out | series exit |

## Relationship to existing PRDs

- **The series is self-contained.** Validation, generation scoping, the repair
  budget, publication and trust for all three authoring workflows are specified
  in WFC-14, WFC-07, WFC-09 and WFC-11; no other proposal has to land first.
  WFC-14 generalises what `create_workflow` already does (runner-owned verdict,
  scoped writers, a repair counter) so `create_tools` and `create_skills` get it
  too.
- **PRD-114 / PRD-213** keep their meaning for the *base* workflow selection.
  WFC-01 supersedes the single sticky override they describe.
- **SUBAGENT series** is unchanged. WFC-04 adds a task field and a worker path
  and **no** limit, consistent with SUBAGENT-12 and the removal of subagent
  limits.
- **TUIR-04** supplies the loop-lag sampler WFC-05 and WFC-13 reuse.

## Implementing the series

[`IMPLEMENTATION-PROMPT.md`](IMPLEMENTATION-PROMPT.md) is the instruction for an
AI agent that implements all fourteen PRDs on one branch: commit regularly, push
at each milestone, then open one pull request to `main`, watch CI and merge.

## Status

WFC-05 is implemented. The remaining PRDs keep their proposed status until their acceptance criteria pass. When a PRD ships, add an
"Implementation status" section with per-criterion evidence, as the SUBAGENT
and TUIR series do, and update the table above.
