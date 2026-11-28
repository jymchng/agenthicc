# UI/UX Pro Max design-intelligence workflow

`ui_ux_pro_max` is a native Agenthicc workflow for product discovery, evidence-
informed design decisions, implementation, and acceptance. It extends the
existing workflow runner and checkpoint contracts; it does not create a second
workflow engine. Every phase has a small transition tool, and only a successful
transition call advances the outer workflow state.

## Capability audit and provenance

The upstream reference was inspected in its actual source repository, rather
than inferred from its README. Upstream code and data are optional inputs to
Agenthicc: they are not vendored, downloaded, or executed automatically. The
skill must be available in Agenthicc's loaded skill catalog before the workflow
can use it. When absent, inaccessible, or failing, the workflow records that
limitation and uses repository evidence; it never labels fallback advice as
UI/UX Pro Max output.

| Verified capability | Upstream implementation or data | Purpose | Agenthicc phase | Artifact and validation |
|---|---|---|---|---|
| Dataset-backed search | `scripts/core.py` defines CSV datasets and schemas; datasets include product, style, color, chart, landing, UX, typography, icons, motion/GSAP, React, web, and Google Fonts guidance | Search focused design domains and return dataset-backed recommendations | `DESIGN_INTELLIGENCE` or `TARGETED_DESIGN_RESEARCH` | `design-research-report.md`; retain query, dataset/domain, finding, provenance, confidence, and limitation; transition tools record supplied details without checking them |
| Search CLI and stack selection | `scripts/search.py` implements domain search, stack selection, and design-system operations | Run supported searches and tailor advice to an actual supported stack | `DESIGN_INTELLIGENCE`, `STACK_ADAPTATION` | Research report and `implementation-guide.md`; inspect the installed parser/help and execution result before claiming a capability |
| Master design-system generation | `search.py` combines product/style/color/landing/typography results with `ui-reasoning.csv` | Generate a coherent master system instead of isolated recommendations | `DESIGN_SYSTEM_SYNTHESIS` | `design-system.md`; submit direction, rationale, tokens, layout, components, states, responsiveness, accessibility, motion, anti-patterns, and artifact reference |
| Design reasoning and validation signals | `data/ui-reasoning.csv` includes recommendation/rule, anti-pattern, severity, reasoning, and confidence fields | Explain why a recommendation fits and identify risk/confidence | `DESIGN_INTELLIGENCE`, `DESIGN_SYSTEM_SYNTHESIS`, `DESIGN_SYSTEM_REVIEW` | Preserve provenance and confidence; review for contradictions and applicability to the actual platform |
| Persist/apply master system and page overrides | `SKILL.md` documents master-system persistence, page-specific overrides, and overwrite controls; CLI exposes persistence/output/page/force options | Reuse a system across pages while allowing justified local overrides | `DESIGN_SYSTEM_SYNTHESIS`, `EXPERIENCE_ARCHITECTURE` | Agenthicc records artifact references only. Skill persistence is not invoked automatically; writing project files still follows normal workspace permissions and approval policy |
| Research dials | `SKILL.md` and CLI expose variance, motion, and density controls | Tune visual output to the requested product and experience | `DESIGN_SYSTEM_SYNTHESIS` | Design-system artifact explains selected values and rationale |
| Process and operating guidance | Upstream `SKILL.md` documents query selection, CLI invocation, dataset discovery, stack choices, and reuse rules | Guide the agent in using executable capabilities accurately | All research/design phases | Report inspected skill/version/path and limitations; never invent domains, flags, results, or source references |
| TUI-specific design corpus/renderer | No upstream TUI-specific dataset or terminal-rendering implementation was verified in the inspected sources | Avoid applying browser assumptions to terminal interfaces | `STACK_ADAPTATION`, `VISUAL_UX_AUDIT` | Translate design decisions into terminal colors, hierarchy, focus, keyboard, density, resizing, and fallbacks; disclose unavailable visual tooling |

The source of truth for the upstream behavior is the [skill instructions](https://github.com/nextlevelbuilder/ui-ux-pro-max-skill/blob/main/.claude/skills/ui-ux-pro-max/SKILL.md), [search implementation](https://github.com/nextlevelbuilder/ui-ux-pro-max-skill/blob/main/src/ui-ux-pro-max/scripts/search.py), [dataset-backed search core](https://github.com/nextlevelbuilder/ui-ux-pro-max-skill/blob/main/src/ui-ux-pro-max/scripts/core.py), and [UI reasoning dataset](https://github.com/nextlevelbuilder/ui-ux-pro-max-skill/blob/main/.claude/skills/ui-ux-pro-max/data/ui-reasoning.csv). Those links describe the inspected upstream repository, not a guarantee that every installed skill version has the same capabilities.

No local skill installation was found in the skill directories inspected during this task, and the workflow tests use an empty loaded skill catalog. Therefore the implementation verifies the integration and honest-fallback contract but does not claim that upstream scripts or datasets were executed in this environment.

## Adaptive routes

Product discovery inspects the repository before asking questions and classifies
the request. It submits a product brief with users, journeys, surfaces,
requirements, constraints, accessibility expectations, acceptance criteria,
and explicit assumptions. Three bounded routes are supported:

```text
broad:
PRODUCT_DISCOVERY → DESIGN_INTELLIGENCE → DESIGN_SYSTEM_SYNTHESIS
→ DESIGN_SYSTEM_REVIEW → STACK_ADAPTATION → EXPERIENCE_ARCHITECTURE
→ DESIGN_IMPLEMENTATION → VISUAL_UX_AUDIT → DESIGN_REMEDIATION (bounded)
→ DESIGN_ACCEPTANCE → DELIVERY

targeted:
PRODUCT_DISCOVERY → TARGETED_DESIGN_RESEARCH → STACK_ADAPTATION
→ EXPERIENCE_ARCHITECTURE → DESIGN_IMPLEMENTATION → VISUAL_UX_AUDIT
→ DESIGN_REMEDIATION (bounded) → DESIGN_ACCEPTANCE → DELIVERY

accessibility_audit:
PRODUCT_DISCOVERY → TARGETED_DESIGN_RESEARCH → STACK_ADAPTATION
→ VISUAL_UX_AUDIT → DESIGN_ACCEPTANCE → DELIVERY
```

The route is selected from discovery output and persisted as
`route_profile`. The same profile determines the workflow checkpoint topology,
so resume uses the matching phase list and branch edges. An invalid stored
profile is normalized to the broad topology; checkpoint decoding separately
validates typed context. Design-system review can return `APPROVED`, `REVISE`,
or `BLOCKED`. Revisions return to synthesis only within the configured attempt
budget. Audit findings enter remediation only when that route includes the
phase and the bounded remediation budget remains. Accessibility-only work does
not regenerate a design system or force implementation.

### The route-profile contract

`complete_product_discovery` names one key: `details["scope"]`, with the three
canonical values `broad`, `targeted`, and `accessibility_audit`. Anything else —
an unknown word, an empty string, or no key at all — is treated as `broad`.

Three further keys are accepted as input spellings, checked in this order after
`scope`, so a model that names the field differently still routes correctly:
`classification`, `route_profile`, and `profile`. An explicit `scope` always
wins over an alias. Only the first non-empty value is considered.

Spelling is not significant. Values are case-folded, trimmed, and have spaces
and hyphens rewritten before lookup, and a bounded alias table maps the common
variants onto the canonical vocabulary:

| Accepted spelling | Canonical profile |
| --- | --- |
| `broad`, `broad_design`, `comprehensive`, `default`, `full`, `Broad`, `BROAD`, ` broad `, `broad-design` | `broad` |
| `targeted`, `targeted_design_research`, `targeted_research`, `focused` | `targeted` |
| `accessibility`, `accessibility_audit`, `a11y` | `accessibility_audit` |

Only canonical values are ever persisted: a checkpoint written by the workflow
carries one of the three keys above, so a stored profile always resolves to a
phase tuple. A checkpoint that carries a legacy or unreadable value — including
the empty string written by earlier versions — is restored as `broad` instead of
failing to decode, and only a wrong *type* (for example a list) is rejected.

A fallback is never silent. When a value has to be defaulted the run records why
in `route_profile_fallback_source` and `route_profile_fallback_detail`
(`checkpoint` or `product_discovery`, whichever saw the bad value first), and the
`/workflow status` view renders one bounded line:

```text
Workflow  ui_ux_pro_max: route profile fell back to broad (source=product_discovery, value='unknown')
```

The invariant is enforced rather than assumed: the transition decision and the
phase list used for indexing are both derived from the same normalised profile,
so a phase the active profile does not schedule is impossible. If a topology
mismatch is ever produced anyway, the workflow raises a typed
`PhaseTopologyError` naming the workflow, the phase, the active profile, and the
phases that profile does schedule. The TUI classifies it by type as a
`configuration` failure and the row reads:

```text
PhaseTopologyError: ui_ux_pro_max: phase 'targeted_design_research' is not part
of route profile 'broad'; scheduled phases: product_discovery,
design_intelligence, design_system_synthesis, ... (+7 more)
```

The phase list is bounded, so the row stays one readable error.

A bare `ValueError: tuple.index(x): x not in tuple` never reaches a transcript.

## Phase outputs

The agents write full Markdown reports or implementation files with the
ordinary workspace tools. Every phase transition tool has a small calling
contract: a required `summary` and optional `details` JSON object. The tool only
records what the agent supplies; it does not inspect files, validate evidence,
check completeness, or decide whether the phase output is correct. The runner
still enforces workflow controls such as approval, retry budgets, and final
acceptance status. This keeps tool calls simple while retaining structured
phase data in checkpoints when supplied.

| Phase | Suggested `details` |
|---|---|
| `PRODUCT_DISCOVERY` | Product brief and chosen route (`broad`, `targeted`, or `accessibility_audit`) |
| `DESIGN_INTELLIGENCE` / `TARGETED_DESIGN_RESEARCH` | Queries, datasets checked, findings, provenance, applicable/rejected recommendations, conflicts, confidence, limitations, report reference |
| `DESIGN_SYSTEM_SYNTHESIS` | Coherent master design system with rationale, tokens, layout, component/interaction rules, responsive/accessibility/motion rules, anti-patterns, and reference |
| `DESIGN_SYSTEM_REVIEW` | `APPROVED`, `REVISE`, or `BLOCKED` decision with optional findings |
| `STACK_ADAPTATION` | Detected stack, evidence, token mappings, component and responsive strategy, accessibility/performance constraints, validation commands, limitations |
| `EXPERIENCE_ARCHITECTURE` | Surface inventory, state matrix, component dependencies, local overrides, acceptance mapping, implementation plan |
| `DESIGN_IMPLEMENTATION` | Files and states changed, implementation summary, deviations with reasons, remaining work |
| `VISUAL_UX_AUDIT` | Structured findings, commands/results, inspection status and limitations |
| `DESIGN_REMEDIATION` | Finding IDs addressed, changes, verification, unresolved items |
| `DESIGN_ACCEPTANCE` | Criterion statuses and evidence or limitations; these are recorded as supplied and are not checked by the transition tool |
| `DELIVERY` | Summary, changed files, audit/test outcomes, unverified requirements, and limitations |

`PASS` should be backed by evidence. Unavailable screenshot/browser/terminal
inspection should be reported as unverified or limited, never inferred from a
successful build. The delivery phase may remain blocked when required criteria
failed or remain unverified; this workflow decision is separate from transition
tool input handling.

## Configuration and invocation

Run from a project workspace:

```bash
uv run agenthicc workflows run ui_ux_pro_max --intent "Improve the mobile checkout"
```

In an interactive session, select it with `/workflow ui_ux_pro_max` and submit
the request normally. The workflow uses the current session's conversation,
workspace scope, tools, mode, approvals, and security policy. It does not
install packages, clone the skill, perform unrestricted web searches, or
automatically persist generated skill output.

Per-phase model overrides and bounded loop budgets are configurable:

```toml
[workflows.ui_ux_pro_max]
design_intelligence_model = "reasoning-model"
visual_ux_audit_model = "reasoning-model"
max_design_review_attempts = 2 # bounded to 1..5
max_remediation_cycles = 2     # bounded to 0..5
max_phase_turns = 24           # bounded to 1..40
```

Model override keys are `<phase>_model` for `product_discovery`,
`design_intelligence`, `targeted_design_research`,
`design_system_synthesis`, `design_system_review`, `stack_adaptation`,
`experience_architecture`, `design_implementation`, `visual_ux_audit`,
`design_remediation`, `design_acceptance`, and `delivery`. Empty values use the
session model. Phase transitions are tool-call gated. If an agent turn ends
without its phase transition tool, the runner continues the same phase with the
shared conversation and session memory for up to three bounded attempts,
reminding the agent to finish without repeating completed work. If all attempts
end without a transition, the phase remains the resume cursor and the workflow
reports the expected transition tool so it can be resumed. The typed checkpoint
stores bounded phase state and artifact references, not full research reports
or source files; the existing workflow checkpoint/resume mechanism remains the
source of durable execution state.

## Validation

The workflow has unit tests for transition recording, profile selection, bounded loops, and checkpoint encoding; integration tests for approval behavior; and mocked-provider end-to-end tests for a broad design run and a focused accessibility audit. The E2E suite also verifies honest handling when the skill or visual-inspection capability is unavailable.

```bash
uv run pytest tests/unit/test_ui_ux_pro_max.py tests/unit/test_workflow_registry.py -q
uv run pytest tests/integration/test_ui_ux_pro_max_integration.py -q
uv run pytest tests/e2e/test_ui_ux_pro_max_e2e.py -q
```
