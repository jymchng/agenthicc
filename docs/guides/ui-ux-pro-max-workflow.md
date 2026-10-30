# UI/UX Pro Max design-intelligence workflow

`ui_ux_pro_max` is a native Agenthicc workflow for product discovery, evidence-
informed design decisions, implementation, and acceptance. It extends the
existing workflow runner and checkpoint contracts; it does not create a second
workflow engine. Every phase has a typed transition tool, and only a successful
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
| Dataset-backed search | `scripts/core.py` defines CSV datasets and schemas; datasets include product, style, color, chart, landing, UX, typography, icons, motion/GSAP, React, web, and Google Fonts guidance | Search focused design domains and return dataset-backed recommendations | `DESIGN_INTELLIGENCE` or `TARGETED_DESIGN_RESEARCH` | `design-research-report.md`; retain query, dataset/domain, finding, provenance, confidence, and limitation; validate submitted summary and bounded fields |
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

## Phase outputs

The agents write full Markdown reports or implementation files with the
ordinary workspace tools. Transition tools capture compact typed summaries and
references, not report bodies. They do not manufacture artifacts or check that
an agent-written artifact exists. This keeps checkpoint state small and leaves
artifact creation with the agent that performed the work.

| Phase | Required output |
|---|---|
| `PRODUCT_DISCOVERY` | Product brief and chosen route (`broad`, `targeted`, or `accessibility_audit`) |
| `DESIGN_INTELLIGENCE` / `TARGETED_DESIGN_RESEARCH` | Queries, datasets checked, findings, provenance, applicable/rejected recommendations, conflicts, confidence, limitations, report reference |
| `DESIGN_SYSTEM_SYNTHESIS` | Coherent master design system with rationale, tokens, layout, component/interaction rules, responsive/accessibility/motion rules, anti-patterns, and reference |
| `DESIGN_SYSTEM_REVIEW` | Explicit `APPROVED`, `REVISE`, or `BLOCKED` decision with actionable findings |
| `STACK_ADAPTATION` | Detected stack, evidence, token mappings, component and responsive strategy, accessibility/performance constraints, validation commands, limitations |
| `EXPERIENCE_ARCHITECTURE` | Surface inventory, state matrix, component dependencies, local overrides, acceptance mapping, implementation plan; plan approval where requested by the active policy |
| `DESIGN_IMPLEMENTATION` | Files and states changed, implementation summary, deviations with reasons, remaining work |
| `VISUAL_UX_AUDIT` | Structured severity/evidence/expected-vs-actual findings, commands/results, inspection status and limitations |
| `DESIGN_REMEDIATION` | Finding IDs addressed, changes, verification, unresolved items |
| `DESIGN_ACCEPTANCE` | Every criterion classified `PASS`, `FAIL`, `UNVERIFIED`, or `NOT_APPLICABLE`, with evidence for claims of success |
| `DELIVERY` | Summary, changed files, audit/test outcomes, unverified requirements, and limitations |

`PASS` requires evidence. Unavailable screenshot/browser/terminal inspection
must be reported as unverified or limited, never inferred from a successful
build. The final workflow is blocked rather than declaring success when a
critical criterion fails or remains unverified.

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

The workflow has unit tests for phase contracts, profile selection, tool
validation, bounded loops, and checkpoint encoding; integration tests for
approval behavior; and mocked-provider end-to-end tests for a broad design run
and a focused accessibility audit. The E2E suite also verifies honest handling
when the skill or visual-inspection capability is unavailable.

```bash
uv run pytest tests/unit/test_ui_ux_pro_max.py tests/unit/test_workflow_registry.py -q
uv run pytest tests/integration/test_ui_ux_pro_max_integration.py -q
uv run pytest tests/e2e/test_ui_ux_pro_max_e2e.py -q
```
