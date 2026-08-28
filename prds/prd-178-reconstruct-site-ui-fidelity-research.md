---
title: "PRD-178: Evidence-Complete UI and Interaction Research for reconstruct_site"
status: Implemented
version: 1.0.0
created: 2026-08-28
scope: "reconstruct_site research phases, browser observation, UI fidelity, interaction parity, and implementation gating"
related_prds:
  - PRD-100  # code_plan architecture
  - PRD-156  # resumable workflow continuation
  - PRD-159  # CloakBrowser agent tools
  - PRD-160  # Playwright agent tools
  - PRD-163  # cache-stable workflow prompts and generated workflows
  - PRD-169  # tool-call transaction integrity
  - PRD-170  # workflow resume recovery
  - PRD-172  # MCP integration
  - PRD-173  # recoverable workflow errors
  - PRD-175  # runtime AGENTS.md integration
  - PRD-177  # efficient, evidence-driven reconstruct_site
tags:
  - workflows
  - reconstruct-site
  - browser
  - visual-fidelity
  - interaction-parity
  - research
  - screenshots
  - checkpoints
---

# PRD-178 — Evidence-Complete UI and Interaction Research for `reconstruct_site`

## 1. Executive summary

`reconstruct_site` must understand the reference product before it begins to
build the replacement. Its current early phases collect useful route, visual,
interaction, asset, architecture, and design-system information, but a phase
can still finish with a prose summary that does not prove that the reference
was studied comprehensively. That makes later implementation guess at routes,
responsive behavior, hidden states, interaction effects, typography, spacing,
and assets. The result may be a plausible website rather than an observable
equivalent of the target.

This PRD makes the first part of `reconstruct_site` an evidence-complete
research pipeline. The workflow will discover the target's surface area,
observe it through a defined route/viewport/state matrix, trace interactions,
measure the rendered UI, inventory content and assets, and publish a durable
fidelity baseline. A dedicated research-completeness gate will prevent
implementation phases from starting while required observations are missing,
unexplained, or contradicted.

The requirement for an “identical” result is defined as *observable fidelity*
within the declared scope: the same routes and user-visible states, materially
equivalent geometry and styling at the measured viewports, and equivalent
responses to the tested interactions. It does not require copying the target's
source code, private services, implementation framework, or inaccessible
server behavior.

This is a follow-on to PRD-177. It refines the beginning of that workflow and
does not replace its evidence store, one-parent-conversation rule, checkpoint
contract, cache contract, profiles, or controlled validation re-entry.

## 2. Problem statement

### 2.1 A summary is not proof of research

An agent can say that it inspected a site without leaving a route-by-route,
viewport-by-viewport record of what it saw. Later phases then infer missing
details. In particular, a homepage-only inspection can miss secondary routes,
drawers, dialogs, validation states, pagination, authenticated boundaries, and
mobile navigation.

### 2.2 Visual parity is under-specified

“Make it look the same” does not identify what must be observed or compared.
Without structured measurements and screenshots, the workflow has no reliable
baseline for container widths, alignment, spacing rhythm, font metrics, image
cropping, responsive breakpoints, overlays, or scroll behavior.

### 2.3 Interaction parity is easy to lose

A static screenshot cannot show whether a menu opens, a tab changes content, a
form rejects invalid data, a filter updates the URL, or a loading state appears
before data arrives. The reconstruction needs an interaction trace and the
visible result for every important control, not merely an inventory of labels.

### 2.4 Research can be lost on interruption

The workflow already preserves the parent session conversation and uses the
PRD-177 evidence manifest, but the research gate needs explicit coverage and
artifact state. On resume, missing or corrupt evidence must cause the relevant
research to be repeated rather than allowing implementation to proceed on an
unverified assumption.

### 2.5 More exploration must not destroy cacheability

The first phases can generate a large amount of evidence. Appending that data
verbatim to every prompt would reduce provider prompt-cache reuse and make the
TUI slow. The design therefore stores large evidence externally and passes a
stable, compact digest plus deterministic artifact references to downstream
turns.

## 3. Goals

1. Make the initial `reconstruct_site` phases a systematic study of the
   reference website's routes, rendered UI, responsive behavior, states,
   interactions, content, assets, fonts, and icons.
2. Define a machine-readable coverage matrix that proves which required
   surfaces were observed and which were explicitly unavailable or waived.
3. Produce a durable, inspectable fidelity baseline that implementation and
   validation phases can consume without copying large bodies into checkpoints
   or every LLM request.
4. Block implementation until the research baseline satisfies the selected
   profile's completeness policy or the user explicitly approves a recorded
   exception.
5. Preserve one parent `conversation_id`, one session-owned conversation
   memory, append-only journal semantics, checkpoint-aware resume, and bounded
   re-entry across the expanded research pipeline.
6. Preserve the cache contract by keeping stable policy and tool schemas
   stable while isolating dynamic observations in external artifacts and
   compact digests.
7. Make browser-unavailable, blocked, authenticated, dynamic, and flaky target
   behavior explicit and recoverable instead of allowing the agent to invent
   observations.
8. Verify the behavior with deterministic unit, integration, E2E, regression,
   and performance tests.

## 4. Non-goals

- Cloning the target's source code, proprietary backend, database, credentials,
  or private implementation details.
- Circumventing robots, authentication, network policy, paywalls, browser
  policy, workspace policy, or legal restrictions.
- Requiring a browser when the user selected a source that is genuinely
  unavailable to the configured browser. The workflow must report degraded
  coverage and obtain a decision.
- Replacing Playwright, CloakBrowser, MCP, the existing browser-artifact store,
  or the PRD-177 reconstruct evidence store.
- Running concurrent mutable turns against the parent `ConversationStore`.
- Treating a provider prompt-cache hit as guaranteed. The workflow can make
  the request prefix cache-eligible, but the provider decides whether it hits.
- Guaranteeing byte-for-byte equality for rendering engines, fonts, network
  data, time-dependent content, random content, or inaccessible states that
  the user did not authorize the workflow to observe.
- Making production infrastructure phases mandatory for a static reference.

## 5. Users and primary journeys

### 5.1 High-fidelity static reconstruction

1. The user selects `reconstruct_site`, supplies a reference URL and target
   directory, and chooses the `static` profile.
2. The workflow asks only the clarifying questions needed to establish scope,
   routes, authentication, allowed domains, and dynamic-content treatment.
3. Research phases discover the target and capture the required route,
   viewport, and visual-state evidence.
4. The user can inspect the evidence package and unresolved questions at the
   research gate.
5. After approval, implementation starts with the evidence package as its
   specification and later validates representative routes against it.

### 5.2 Interactive application reconstruction

The `application` profile additionally requires behavior for navigation,
forms, filters, tabs, overlays, data loading, URL state, and error/empty
conditions. API observations are recorded only within configured network and
credential policy; the workflow may model unavailable APIs with explicit
fixtures or mocks.

### 5.3 Production reconstruction

The `production` profile performs the same research with the strictest
coverage policy and then continues into the infrastructure and operational
phases defined by PRD-177. Research does not assume that a production target
is technically or legally reproducible; unsupported behavior is surfaced as a
decision at the gate.

### 5.4 Interrupted or resumed research

When the user interrupts a browser observation, an LLM turn, or the whole
process, the checkpoint contains the active phase, attempt, coverage-matrix
revision, artifact-manifest revision, parent conversation identity, and the
last completed observation boundary. Resume rehydrates those references and
continues with the first incomplete or stale coverage cell.

### 5.5 Research rejection and re-entry

At the gate, the user or agent can reject the baseline with a concrete reason,
such as “the mobile drawer was not observed.” The workflow marks the affected
coverage cells and downstream artifacts stale, re-enters the earliest valid
research phase, and preserves unaffected evidence. It must not restart the
whole run or silently proceed to bootstrap.

## 6. Definitions and fidelity model

### 6.1 Surface

A surface is a navigable route or a user-reachable overlay/state that can
affect the rendered experience. Examples include `/`, `/pricing`, a mobile
navigation drawer, a sign-in dialog, a filtered result view, and a form error
state.

### 6.2 Observation cell

An observation cell is the smallest required unit in the coverage matrix:

```text
(surface, viewport, visual_state, interaction_cluster, observation_role)
```

`observation_role` is `reference` during research and `implementation` during
later parity validation. A cell is complete only when it has a timestamped
observation receipt, a source URL/state, and all artifacts required by its
policy.

### 6.3 Required, unavailable, and waived

- `required`: the workflow must obtain evidence before the gate can pass.
- `unavailable`: an observation was attempted but blocked by a recorded
  technical or policy limitation. The user must decide whether to narrow the
  scope or approve degraded reconstruction.
- `waived`: the user explicitly excluded the cell with a reason. The waiver is
  part of the baseline and is visible in diagnostics.
- `not_applicable`: the observed controls or surface make a state irrelevant;
  the research phase records the reason instead of silently dropping the
  obligation.
- `complete`: evidence meets the cell's artifact and quality requirements.
- `stale`: evidence was invalidated by a changed prerequisite or re-entry.

The agent cannot convert a missing cell to `complete` by writing a prose
summary. `unavailable` and `waived` are not hidden successes.

### 6.4 Observable identity

The default identity contract compares behavior and rendered output at the
declared matrix, not implementation internals. The default strictness is:

| Dimension | Required observation | Default comparison policy |
|---|---|---|
| Route | Path, query, hash, redirect, title, scroll restoration | Same effective route and user-visible navigation result |
| Geometry | Element boxes, container widths, alignment, spacing, overflow | No unexplained major displacement; configured pixel/relative tolerances |
| Typography | Family, fallback, weight, size, line-height, letter-spacing, casing | Same declared metrics where available; explicit fallback recorded |
| Appearance | Colors, opacity, borders, radii, shadows, gradients, images, icons | Same computed or measured values within a documented tolerance |
| Responsive behavior | Breakpoints, reflow, visibility, overflow, touch target layout | Same state transition and layout regime at each declared viewport |
| Interaction | Action, precondition, visible result, navigation/data effect | Same observable result for each required trace |
| Accessibility surface | Labels, roles, focus order, keyboard access, announcements | No missing required interaction or newly inaccessible control |

The implementation phase may use a different framework, but it cannot use
framework differences as an excuse for an unexplained observable discrepancy.
Tolerance values are stored in the baseline, are deterministic, and can be
overridden only by the user or a documented profile policy.

## 7. Proposed workflow topology

### 7.1 Research-first phase order

PRD-177's authoritative plan remains the source of truth for execution. This
PRD requires its next version to make the following early sequence explicit:

```text
init
  -> recon
  -> visual_research
  -> interaction_analysis
  -> content_assets
  -> responsive_research
  -> architecture
  -> design_system
  -> research_gate
  -> bootstrap
  -> ...implementation and validation...
```

`recon`, `visual_research`, `interaction_analysis`, and `content_assets`
remain the public conceptual phases already documented by PRD-177. The new
`responsive_research` phase isolates breakpoint and viewport evidence from
the later implementation `responsive_pass`. The new `research_gate` phase is
the only transition into implementation. If repository conventions prefer
different internal names, the phase plan must retain stable display names and
persist a versioned alias map; no duplicate runner or backup directory may be
introduced.

### 7.2 Research phase responsibilities

| Phase | Must establish | Must not do |
|---|---|---|
| `init` | Scope, target, profile, policy, auth and dynamic-content decisions | Guess missing user requirements |
| `recon` | Complete reachable route/surface inventory and reachability results | Claim routes from a sitemap or prompt without observation |
| `visual_research` | Baseline screenshots, rendered measurements, visual states, page anatomy | Begin application implementation |
| `interaction_analysis` | Action/state traces, keyboard/focus behavior, URL and data effects | Infer an interaction solely from a label or screenshot |
| `content_assets` | Text/content roles, images, icons, fonts, media dimensions and provenance | Silently replace missing assets with arbitrary substitutes |
| `responsive_research` | Viewport matrix, breakpoints, reflow, visibility, overflow, touch behavior | Treat desktop-only evidence as mobile evidence |
| `architecture` | A target-independent implementation model derived from evidence | Invent unobserved product behavior |
| `design_system` | Normalized tokens, component anatomy, layout rules and exceptions | Start writing production components |
| `research_gate` | Coverage completeness, conflicts, limitations, user decision and baseline revision | Allow bootstrap with unresolved required cells |

The existing `architecture` and `design_system` phases may write planning
artifacts, but they remain research/planning phases until the gate succeeds.
`bootstrap` is the first phase permitted to mutate the target application as
part of implementation.

## 8. Functional requirements

### FR-1 — Establish and persist research scope

The `init` phase MUST collect or confirm:

- reference URL and authorized target domains;
- target directory and workspace scope;
- selected profile (`static`, `application`, `production`, or `custom`);
- route inclusion/exclusion rules and whether discovery may follow external
  links;
- authentication state and how credentials must be supplied, without storing
  secrets in artifacts;
- required viewports or device classes;
- treatment of live, time-dependent, randomized, personalized, and
  API-backed content;
- whether unavailable behavior may be represented by a mock or fixture; and
- the user's definition of acceptable fidelity exceptions.

Missing decisions MUST use the existing question tool and pause for an answer.
The phase MUST store a redacted scope receipt before research begins.

### FR-2 — Discover all in-scope routes and surfaces

`recon` MUST:

1. Start at the reference URL and follow only permitted links and navigation
   actions.
2. Record canonical URL, path, query/hash behavior, title, page purpose,
   referring control, authentication requirement, and reachability status.
3. Inspect header, footer, navigation, menus, breadcrumbs, search, filters,
   pagination, tabs, cards, tables, forms, dialogs, drawers, tooltips, and
   other controls that expose a distinct surface or state.
4. Detect route candidates from rendered links and observed navigation, while
   marking unvisited candidates as `discovered_not_observed`.
5. Record redirects, 404/403/500 responses, loading and empty route states when
   they are reachable under policy.
6. Deduplicate aliases without deleting evidence for the alias.

The route inventory MUST include a coverage status for every candidate and a
reason for every excluded or unavailable candidate.

### FR-3 — Define a deterministic viewport and device matrix

The default matrix MUST include at least:

| Class | Viewport |
|---|---:|
| Mobile | 390 × 844 CSS pixels |
| Tablet | 768 × 1024 CSS pixels |
| Desktop | 1440 × 900 CSS pixels |

The user may add or remove viewports within the selected profile policy. The
workflow MUST record viewport width, height, device scale factor, user-agent
class, orientation, reduced-motion preference, color scheme, and touch/pointer
assumptions. A viewport may be skipped only with a recorded unavailable or
waived status.

### FR-4 — Capture visual states, not only default pages

For every required surface and viewport, the workflow MUST classify and, when
reachable, observe the following state groups:

- initial load, stable loaded state, and scroll positions for long pages;
- hover, focus-visible, active, pressed, disabled, selected, expanded, and
  checked states for applicable controls;
- loading/skeleton, empty, validation-error, server-error, offline, and
  permission/authentication states when reachable or explicitly configured;
- open/closed states for menus, popovers, tooltips, dialogs, drawers, and
  accordions;
- pagination, sorting, filtering, tabs, carousels, and other content-changing
  states;
- sticky headers, fixed action bars, lazy-loaded regions, and viewport-edge
  behavior; and
- browser back/forward and refresh behavior for stateful routes.

State applicability MUST be derived from observed controls and the declared
scope. A state that is not applicable is recorded as `not_applicable` with
the reason; it is not silently omitted.

### FR-5 — Capture screenshots with stable identity

When a browser is available, each required reference visual cell MUST have a
screenshot or an explicit structured failure. The workflow MUST use the
existing Playwright or CloakBrowser artifact contract and record:

- artifact ID and content hash;
- sanitized URL, route/surface ID, viewport, device scale, and scroll/state
  identifier;
- reference versus implementation role;
- browser backend and capture timestamp;
- whether fonts, images, and network data were fully loaded;
- redaction status for sensitive regions; and
- complete, degraded, unavailable, or stale status.

Repeated capture of the same `(surface, viewport, state, role, source
revision)` MUST be idempotent where the bytes are unchanged. Screenshots MUST
not contain credentials or unredacted secrets. If the browser is unavailable,
the workflow MUST not fabricate screenshots or mark the cell complete.

### FR-6 — Record measurable visual observations

For each representative page and state, the research output MUST include
structured measurements or a declared measurement limitation for:

- viewport and page bounds;
- main containers, columns, grids, cards, controls, and overlay rectangles;
- margins, padding, gaps, alignment, max-widths, min-heights, and overflow;
- font family and fallback, size, weight, line-height, letter-spacing, and
  text-transform for each typography role;
- background/foreground colors, opacity, borders, radius, shadows, gradients,
  and separators;
- image and icon source, dimensions, aspect ratio, crop/fit behavior, and
  loading placeholder; and
- fixed, sticky, scroll, clipping, and z-index behavior observable in the
  target.

Measurements MAY be normalized to tokens and repeated component roles, but
the baseline MUST preserve representative raw observations and their source
cell. The agent MUST distinguish measured values from visual estimates.

### FR-7 — Trace interactions and their outcomes

For every important interaction cluster, the workflow MUST record a trace with:

```json
{
  "trace_id": "interaction-...",
  "surface_id": "...",
  "viewport_id": "mobile",
  "precondition": "menu is closed",
  "action": {"kind": "click", "target": "primary-nav"},
  "sequence": ["click primary-nav", "click pricing"],
  "visible_outcome": "drawer opens, then pricing route renders",
  "navigation_effect": {"path": "/pricing", "query": "", "hash": ""},
  "data_effect": "none|request|local-state|storage|unknown",
  "focus_effect": "focus moves to first drawer link",
  "screenshots": ["artifact-id-before", "artifact-id-after"],
  "status": "complete"
}
```

Traces MUST cover pointer, keyboard, touch, and form submission behavior when
applicable. The workflow MUST record the result of valid and invalid form
inputs, escape/backdrop dismissal, focus return, browser navigation, URL/query
changes, and loading/error transitions. API request details are limited to
authorized, redacted metadata; secrets and private response bodies are never
stored in the research package.

### FR-8 — Model responsive behavior explicitly

`responsive_research` MUST compare the same surfaces and applicable states
across the viewport matrix and record:

- observed breakpoint intervals and the evidence for each interval;
- layout reflow, stacking, order, alignment, and density changes;
- controls that disappear, collapse, become drawers, or change interaction
  method;
- text wrapping, truncation, line clamping, image cropping, and overflow;
- touch-target sizing and gesture/scroll behavior; and
- fixed/sticky elements and viewport-safe-area behavior.

The output MUST identify invariants, breakpoint-specific rules, and exceptions.
The later implementation phase MUST consume these rules instead of inventing
mobile behavior from a desktop screenshot.

### FR-9 — Inventory content, media, fonts, and icons

`content_assets` MUST publish an inventory for visible and interaction-driving
content, including role, source/provenance, dimensions, format, alt/label
semantics, loading behavior, and whether it is static, generated, or
time-dependent. It MUST identify font files or CSS declarations, fallback
chains, icon source and sizing, and repeated content structures.

Unavailable assets MUST be labelled with a reason and a replacement policy.
The agent MUST not silently substitute a different asset and call it
identical.

### FR-10 — Normalize evidence into a fidelity baseline

Before the gate, the workflow MUST derive a versioned baseline containing:

- scope and profile;
- route/surface inventory;
- viewport and environment matrix;
- visual-state coverage matrix;
- interaction traces and state-transition graph;
- measured layout/style observations and normalized tokens;
- content/asset/font/icon inventory;
- screenshot manifest and comparison metadata;
- known dynamic, unavailable, and waived behavior;
- contradictions and unresolved questions;
- comparison tolerances and fidelity exceptions; and
- links to all source artifacts and phase receipts.

The baseline MUST be deterministic for the same fixture observations after
volatile timestamps and IDs are normalized. It MUST include a content hash and
manifest revision.

### FR-11 — Enforce a research-completeness gate

`research_gate` MUST compute completeness from the selected profile and the
coverage matrix. It MUST fail or remain waiting when any required cell is
missing, stale, contradictory, or lacks the required artifacts. The agent
must transition only through an explicit tool call, such as:

- `approve_research_baseline(summary, baseline_artifact_id)`;
- `reject_research_baseline(findings, target_phase)`; or
- `approve_degraded_research(exception_ids, rationale)`.

Approval MUST verify that the artifact ID is the current baseline, that all
required cells have acceptable statuses, and that the user has explicitly
accepted every degraded/waived exception. A prose response or an arbitrary
phase result MUST NOT start `bootstrap`.

The gate's report MUST show counts for complete, missing, unavailable, waived,
stale, and contradictory cells and list the exact cells preventing approval.

### FR-12 — Give implementation phases an evidence-derived context

After approval, `bootstrap` and later phases MUST receive a compact digest
containing the current baseline revision, profile, route count, coverage
counts, token summary, open issues, and artifact IDs. They MUST be able to
read the full artifacts through authorized tools when detail is needed.

Prompts MUST instruct implementation agents to treat the baseline as the
source of observed truth, cite the relevant artifact/cell when making a
decision, and record any deviation as a validation issue. The workflow MUST
not paste the complete screenshot metadata, HTML, or research corpus into
every turn.

### FR-13 — Preserve the cache contract

The research-first workflow MUST:

- keep the stable system prompt, workflow policy, and capability-filtered tool
  schemas deterministic within a cache epoch;
- keep phase-specific observations, question answers, route IDs, artifact
  revisions, and current-state digests in dynamic context;
- avoid embedding timestamps, random IDs, current token counts, or mutable
  evidence bodies in stable prompt text;
- reuse the same compiled stable tool bundle for compatible research phases;
- invalidate the bundle only for a genuine capability, policy, configuration,
  or browser-backend change; and
- record cache eligibility and fingerprints without claiming provider cache
  hits.

### FR-14 — Preserve conversation, memory, journal, and checkpoint continuity

All parent research phases, retries, gate decisions, resume operations, and
controlled re-entry MUST use the same workflow-scoped `conversation_id` and
session-owned conversation memory. The workflow MUST append observations and
summaries in order; it MUST not create a fresh parent conversation per phase
or replay the whole research corpus as new user messages.

Checkpoints MUST include at least:

```text
workflow name and plan version
active profile and phase/attempt
parent conversation_id
phase journal position or receipt ID
evidence manifest path and revision
fidelity baseline artifact ID/hash, if published
coverage matrix revision and incomplete-cell IDs
stale artifact IDs and re-entry history
browser backend/capability status
compact digest and recovery diagnostics
```

Large artifact bodies remain in the authorized evidence store. There is no
arbitrary one-megabyte checkpoint ceiling; filesystem, serialization, and
provider limits remain real operational errors and must be reported
recoverably.

### FR-15 — Make failure and degraded evidence explicit

Transient browser, network, provider, tool, and artifact errors MUST be
represented as structured recoverable failures with the affected cell, retry
count, and next action. A browser timeout MUST not be converted into a
successful observation. Repeated failures may result in `unavailable`, but
only after the configured retry policy and with a gate decision required.

If Playwright or CloakBrowser is unavailable, the workflow MUST report the
specific missing capability and continue independent non-browser research when
possible. `/tools reload` or a capability change MUST not corrupt completed
evidence or cause unrelated tools to disappear.

### FR-16 — Support profile-specific coverage

The profile policy MUST determine the required matrix, not merely the number
of later implementation phases:

| Profile | Research minimum |
|---|---|
| `static` | All in-scope routes, visual states, viewports, assets, and navigation that are reachable without application data |
| `application` | Static minimum plus stateful controls, forms, loading/empty/error behavior, URL/data effects, and authorized API observations |
| `production` | Application minimum plus operational entry points, deployment-visible environment assumptions, and production-specific routes/states in scope |
| `custom` | User-declared cells, with `init`, `research_gate`, and `final_validation` required |

Omitted profile phases and cells MUST be recorded with actionable reasons.

### FR-17 — Invalidate dependent evidence on re-entry

If visual or interaction validation requests a research target, the runner
MUST validate the target against the authoritative plan, mark the target and
downstream baseline/implementation artifacts stale, preserve unrelated route
evidence, and consume the bounded re-entry budget. An unknown target MUST
return a structured tool error and leave the current phase active.

Changing scope, route inclusion, viewport policy, or a source revision MUST
invalidate the baseline and affected coverage cells before implementation can
continue.

### FR-18 — Instruct agents to research rather than improvise

Research-phase system/dynamic prompts and transition-tool descriptions MUST
tell agents:

- research is evidence collection, not implementation;
- inspect every in-scope route and relevant state rather than only the home
  page;
- use the browser tools and record their artifact IDs immediately;
- measure and label observations versus estimates;
- ask the user when scope or behavior is ambiguous;
- never claim an unobserved route/state is complete;
- submit structured receipts through tools; and
- leave a clear unresolved-questions list for the gate.

The prompt text itself must remain cache-stable where it is policy, while
current route and artifact data must remain dynamic.

## 9. Evidence and artifact contract

### 9.1 Required artifact kinds

The PRD-177 evidence store MUST support these additional typed kinds:

```text
research_scope
route_surface_inventory
viewport_environment_matrix
visual_state_inventory
visual_measurements
interaction_trace_catalog
interaction_state_graph
responsive_behavior_matrix
content_asset_inventory
font_icon_inventory
fidelity_baseline
research_coverage_report
research_gate_receipt
```

Existing `route_inventory`, `visual_spec`, `interaction_inventory`,
`asset_inventory`, screenshot, phase-receipt, and validation kinds remain
valid and may be referenced by the new aggregate artifacts.

### 9.2 Common artifact record

Every record MUST preserve the PRD-177 fields and add source-cell linkage:

```json
{
  "artifact_id": "sha256-or-uuid",
  "kind": "visual_measurements",
  "relative_path": ".agenthicc/reconstruct_site/run/research/visual-measurements.json",
  "media_type": "application/json",
  "sha256": "...",
  "byte_count": 1234,
  "phase": "visual_research",
  "attempt": 1,
  "source_cells": ["home|desktop|loaded|primary-navigation|reference"],
  "status": "complete",
  "created_at": "...",
  "source": "workflow|browser|agent_write|command"
}
```

The manifest MUST use relative authorized paths, atomic revision updates, and
hash verification. It MUST redact credentials, authorization headers,
cookies, local-storage tokens, and sensitive form values before persistence.

### 9.3 Coverage record

Each matrix cell MUST be serializable independently so a single interrupted
capture can resume without repeating the whole route:

```json
{
  "cell_id": "home|mobile|loaded|primary-navigation|reference",
  "surface_id": "home",
  "viewport_id": "mobile",
  "visual_state": "loaded",
  "interaction_cluster": "primary-navigation",
  "role": "reference",
  "status": "complete",
  "observation_receipt_id": "receipt-...",
  "artifact_ids": ["screenshot-...", "measurement-..."],
  "limitations": [],
  "observed_at": "..."
}
```

## 10. Dataflow

The required dataflow is:

```text
User scope and answers
        |
        v
init -> redacted research_scope artifact
        |
        v
recon -> route/surface inventory -> required coverage cells
        |                                  |
        +------------------+---------------+
                           v
                visual / interaction / responsive observation
                 |              |                |
                 v              v                v
          screenshots +   action/state traces   viewport rules
          measurements
                 \              |                /
                  v             v               v
                 durable evidence manifest revisions
                           |
                           v
                  content/assets + design model
                           |
                           v
               normalized fidelity baseline + coverage report
                           |
              missing? ----+---- complete/degraded decision
                |                         |
                v                         v
        retry/ask/re-enter      research_gate tool call
                                            |
                              approved baseline artifact reference
                                            |
                                            v
                          bootstrap and implementation phases
                                            |
                                            v
                 implementation screenshots/traces and validation
```

Conversation and persistence flow alongside the evidence flow:

```text
Each parent phase turn
  -> same conversation_id
  -> same session ConversationStore / memory
  -> append-only journal entry
  -> phase tool receipt
  -> evidence manifest transaction
  -> compact checkpoint reference + digest

resume
  -> rehydrate conversation and typed checkpoint
  -> verify manifest revision and hashes
  -> find first incomplete/stale coverage cell
  -> continue the same phase/attempt boundary
```

The full conversation remains available for continuity. Large evidence is
loaded by reference only when needed. The baseline is the handoff between
research and implementation; it is not a second conversation or a replacement
for the journal.

## 11. Agent, tool, and phase contracts

### 11.1 Research transition tools

Each research phase MUST expose typed transition tools that validate required
fields, persist evidence, and advance only after a successful transaction.
The tools SHOULD include the following logical operations:

- `submit_route_surface_inventory(routes, coverage_plan, summary)`;
- `record_reference_screenshot(cell_id, browser_artifact_id, metadata)`;
- `submit_visual_observations(cell_id, measurements, state, artifact_ids)`;
- `submit_interaction_trace(trace, artifact_ids)`;
- `submit_responsive_observations(surface_id, observations, artifact_ids)`;
- `submit_content_asset_inventory(entries, artifact_ids)`;
- `submit_architecture_model(model, source_artifact_ids)`;
- `submit_design_system(tokens, component_rules, source_artifact_ids)`;
- `approve_research_baseline(summary, baseline_artifact_id)`; and
- `reject_research_baseline(findings, target_phase)`.

Names may follow current repository conventions, but every operation MUST
have a structured result, stable schema, explicit error fields, and a receipt
ID. A successful tool call MUST be idempotent for the same source revision and
content hash.

### 11.2 Browser observation boundary

Browser tools remain responsible for browser execution, network policy,
timeouts, screenshots, and browser artifacts. The reconstruct workflow is
responsible for associating those artifacts with route/state/viewport cells.
The workflow MUST not reach around `NetworkGuard`, `WorkspaceView`, browser
policies, or the configured Playwright/CloakBrowser integration.

### 11.3 Phase transition boundary

Only a validated transition tool can move a phase to its next state. Agent
prose, a successful screenshot, a file write, or exhaustion of turns cannot
advance the research gate. Rejection, retry, interruption, provider failure,
and resume must retain the current phase and its durable partial receipts.

## 12. Non-functional requirements

### 12.1 Determinism and reproducibility

- Tests MUST use a local fixture site with deterministic routes, fonts,
  images, API responses, delays, and interaction states.
- Coverage-cell IDs and normalized baseline hashes MUST be stable when volatile
  values are removed.
- Browser capture settings, wait conditions, viewport, and environment MUST be
  recorded.
- Re-running a completed observation MUST not duplicate identical artifacts.

### 12.2 Performance and cost

- The workflow MUST deduplicate repeated routes, states, screenshots, and
  measurements before invoking another LLM turn.
- Large evidence MUST remain external to conversation messages and ordinary
  checkpoints.
- The research digest included in a phase turn SHOULD remain bounded by an
  implementation-configured budget and contain references rather than bodies.
- Browser waits MUST have explicit timeouts and cancellation propagation.
- A static profile MUST not run application/production research or
  infrastructure phases that are outside its declared scope.
- Metrics MUST report route count, cell count, browser calls, LLM turns,
  repeated captures, artifact bytes, and gate duration.

No requirement in this PRD introduces an arbitrary global checkpoint-size
limit. Oversized serialization or provider requests remain explicit errors
with recovery guidance.

### 12.3 Resilience

- Every capture and artifact write MUST be retryable and idempotent.
- A crash between artifact write and phase transition MUST be recoverable by
  reconciling the manifest and receipt transaction.
- A crash during a browser action MUST leave the cell incomplete or
  unavailable, never complete without evidence.
- Missing/corrupt artifacts MUST be diagnosed and re-collected.
- Interrupted waits MUST honor the existing unified cancellation contract.

### 12.4 Security and privacy

- Network and workspace access MUST remain governed by configured policy,
  including allowed domains, localhost behavior, and browser capability policy.
- Authentication must use the existing secret/input mechanisms. Secrets MUST
  never be written to prompts, screenshots, logs, manifests, checkpoints, or
  test fixtures.
- Screenshot redaction MUST happen before durable publication where sensitive
  fields can be identified.
- Artifact paths MUST be confined to the authorized workspace and protected
  against traversal and symlink escapes.
- The workflow MUST not expand permissions because a target is difficult to
  inspect.

### 12.5 Accessibility of the reconstructed result

The research baseline MUST retain labels, roles, keyboard paths, focus order,
focus-visible styling, dialog semantics, and relevant announcements. Later
accessibility validation MUST compare the reconstructed interaction surface
against these observations, not only run a generic linting tool.

## 13. Acceptance criteria

### AC-1 — No implementation before research approval

Given a new `reconstruct_site` run, when any required route/viewport/state
cell is missing, then `bootstrap` cannot start and the gate reports the exact
blocking cells.

### AC-2 — Complete route discovery

Given the deterministic multi-route fixture site, when research completes,
then every in-scope route, redirect, navigation surface, and reachable overlay
appears in the route/surface inventory with a coverage status and evidence
receipt.

### AC-3 — Responsive evidence

Given a site with desktop navigation and a mobile drawer, when the three
default viewports are researched, then the baseline contains screenshots and
responsive observations for both navigation regimes, including the drawer's
open/close behavior.

### AC-4 — Interaction trace parity input

Given a form with valid, invalid, loading, and server-error outcomes, when
interaction research runs, then the baseline contains traces for each outcome,
the visible state, focus result, and any URL/data effect.

### AC-5 — Measurement provenance

Given a measured card and heading, when the baseline is published, then each
normalized token or rule links back to raw measurements and a source
route/viewport/state cell; estimates are labelled as estimates.

### AC-6 — Browser failure is not success

Given a browser timeout or unavailable browser backend, when a required cell is
attempted, then the cell is `unavailable` or `incomplete`, no screenshot is
fabricated, and the gate requires retry, scope change, or explicit degraded
approval.

### AC-7 — Durable evidence and resume

Given an interruption after some but not all mobile captures, when the run is
resumed with the same session, then completed cells and artifacts are reused,
the first incomplete cell is selected, and the parent conversation ID is
unchanged.

### AC-8 — Corrupt evidence recovery

Given a changed or deleted evidence artifact, when the run resumes, then hash
verification marks its cell stale and the workflow re-collects it before gate
approval.

### AC-9 — Gate approval is tool-controlled

Given a complete baseline, when the agent emits an approval sentence without
calling the gate tool, then the phase remains active. Only a valid approval
tool call advances to `bootstrap`.

### AC-10 — Explicit degraded approval

Given one cell is unavailable because the target requires unauthorized
authentication, when the user approves a degraded scope with a rationale, then
the baseline records the exception, the gate receipt records the approver
decision, and implementation prompts expose the limitation.

### AC-11 — Cache-stable requests

Given two research turns in the same cache epoch, when only route observations
and artifact revisions change, then the stable system prompt and compiled
capability-filtered tool schema remain byte-equivalent while the dynamic
digest changes.

### AC-12 — No research corpus duplication

Given a large screenshot and route inventory corpus, when a downstream phase
starts, then its prompt contains the compact baseline digest and references,
not duplicate full artifact bodies, and the checkpoint contains references and
small summaries rather than the corpus.

### AC-13 — Interaction re-entry is bounded

Given a later interaction validation failure targeting `interaction_analysis`,
when re-entry is requested, then the target and dependent baseline artifacts
become stale, unrelated visual evidence remains valid, and an exhausted
re-entry budget prevents another cycle with a recoverable diagnostic.

### AC-14 — Invalid target is rejected

Given an unknown research re-entry target, when the rejection tool is called,
then it returns a structured validation error and the workflow remains in its
current phase without silently selecting visual research.

### AC-15 — Profile minimization

Given a `static` profile, when the research and implementation graph is
resolved, then application-only and production-only cells/phases are omitted
with reasons, while all static required coverage remains enforced.

### AC-16 — Reproducible evidence

Given the same fixture, capture configuration, and normalized content, when
the evidence pipeline runs twice, then the normalized coverage and baseline
hashes match apart from documented volatile fields.

### AC-17 — Security boundary

Given an artifact containing an authorization header, cookie, or password
field, when the receipt and screenshot are persisted, then the sensitive value
is redacted and no secret appears in the manifest, checkpoint, journal, or
logs.

### AC-18 — End-to-end fidelity handoff

Given the fixture site's baseline is approved, when implementation runs, then
the first implementation phase can resolve the baseline and route/cell
references, and final validation can compare implementation captures against
the corresponding reference cells.

## 14. Testing strategy

### 14.1 Unit tests

Add isolated tests for:

- scope/profile normalization and required-cell generation;
- route/surface deduplication, aliasing, exclusions, and reachability status;
- viewport and visual-state matrix expansion;
- stable cell IDs and normalized baseline hashing;
- measurement normalization and estimate provenance;
- interaction trace schema validation and state-graph construction;
- screenshot metadata validation, redaction, and idempotency keys;
- artifact manifest revisioning, atomic writes, hash verification, and stale
  propagation;
- gate completeness calculation, contradictory cells, unavailable/waived
  policy, and approval validation;
- phase-plan edges, retry/re-entry targets, profile omissions, and invalid
  target errors;
- compact digest generation and cache-epoch fingerprint behavior; and
- checkpoint serialization/rehydration without copying large artifact bodies.

Include malformed JSON, missing fields, path traversal, symlink, hash mismatch,
duplicate receipt, stale revision, invalid viewport, unknown state, and
oversized-artifact error cases.

### 14.2 Integration tests

Use temporary workspaces and fake browser/provider implementations to verify:

- route discovery feeds the coverage matrix;
- screenshots are linked to Playwright/CloakBrowser artifacts without
  bypassing policy;
- visual, interaction, responsive, and asset receipts publish atomically;
- an interrupted capture resumes at the first incomplete cell;
- manifest corruption causes targeted re-collection;
- the gate blocks and then advances after valid approval;
- degraded approval records exceptions without suppressing diagnostics;
- re-entry stales only dependent artifacts and respects the budget;
- the same parent memory and `conversation_id` are used across all research
  phases and retries;
- stable tool bundles are reused while dynamic digests evolve;
- static/application/production/custom profile requirements resolve correctly;
- provider, browser, MCP, and tool failures become structured recoverable
  errors; and
- checkpoint and evidence changes survive process restart.

### 14.3 End-to-end tests

Run a local fixture website through the real workflow boundary and cover:

1. fresh static research, gate approval, implementation handoff, and final
   validation;
2. application research with navigation, drawer, tabs, filter, form, loading,
   empty, and error states;
3. mobile/tablet/desktop capture and responsive comparison;
4. browser unavailable and explicit degraded approval;
5. interruption during each research phase followed by resume;
6. rejection at the gate followed by targeted re-entry;
7. artifact deletion/corruption followed by recovery;
8. custom profile with explicit exclusions; and
9. secret redaction in a complete persisted evidence package.

E2E assertions MUST inspect both user-visible workflow behavior and the
manifest/baseline/checkpoint contents. They MUST not rely on a live third-party
website or an external provider.

### 14.4 Performance and regression tests

Measure a fixture with at least ten routes, three viewports, and multiple
interaction states. Assert that:

- repeated identical captures are deduplicated;
- checkpoint size is dominated by references and remains independent of full
  screenshot bytes;
- downstream prompts contain bounded digests rather than the full evidence;
- resuming after 50% completion does not repeat completed cells;
- phase/tool compilation is reused within a cache epoch; and
- adding a research artifact does not change the stable system/tool prefix.

Record baseline metrics so later changes can detect route fan-out, browser-call,
artifact-byte, or LLM-turn regressions.

## 15. Migration and compatibility

1. Keep the public workflow name `reconstruct_site`, existing profile names,
   browser tool contracts, and PRD-177 artifact paths compatible.
2. Bump the authoritative phase-plan version for the new topology. New runs
   use the new research gate; an in-progress older plan resumes against its
   persisted plan version rather than unexpectedly changing phase order.
3. Existing PRD-177 route, visual, interaction, asset, screenshot, and phase
   artifacts remain readable. A migration may wrap them in new coverage and
   baseline records, but must not claim cells complete without enough source
   evidence.
4. If an old checkpoint has no coverage matrix or baseline reference, resume
   must enter a compatibility research-reconciliation step before
   `bootstrap`; it must not discard the old context or fabricate a baseline.
5. Existing static/application/production runs retain their scope semantics.
   The new research policy may require additional observations only for a new
   phase-plan version or when the user changes scope.
6. Do not add backup, duplicate, or legacy workflow directories. The canonical
   implementation remains under `src/agenthicc/workflows/reconstruct_site/`.
7. Update `docs/guides/workflows.md`, the reconstruct workflow findings,
   storage documentation, public symbol inventories, and the PRD index in the
   same change.

## 16. Observability and diagnostics

The TUI and headless runner MUST expose:

- current research phase and attempt;
- route/surface and cell currently being observed;
- completed/required/unavailable/waived/stale cell counts;
- browser backend and capability status;
- evidence manifest and baseline revisions;
- retries, blocked reasons, unresolved questions, and gate decision;
- re-entry source, target, reason, and remaining budget; and
- research duration, browser calls, artifact bytes, and LLM turn counts.

Exploratory browser/tool calls may be consolidated in the TUI, but the
underlying receipts and errors remain individually queryable for debugging.
Diagnostics MUST redact secrets and avoid dumping full page bodies or prompt
contents by default.

## 17. Rollout plan

### Phase 1 — Schemas and plan

Add typed coverage, observation, trace, baseline, and gate contracts. Extend
the authoritative reconstruct phase plan and checkpoint codec. Add unit tests
before changing execution.

### Phase 2 — Research execution

Implement deterministic route/state/viewport exploration, screenshot linkage,
measurements, and receipts. Add integration tests for transactional artifact
publication and recovery.

### Phase 3 — Gate and implementation handoff

Implement completeness evaluation, explicit approval/rejection/degraded
decisions, compact baseline digests, and implementation-phase access. Add E2E
tests for the full fixture journey.

### Phase 4 — Validation and migration

Connect later visual/interaction validation to baseline cells, implement
targeted staleness/re-entry, support old PRD-177 artifacts/checkpoints, and
run performance/regression tests.

### Phase 5 — Documentation and release gate

Update guides, storage/reference docs, public symbol inventories, PRD index,
diagnostics, and CI commands. Run the unit/integration/E2E/performance matrix
and relevant static checks.

## 18. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Exhaustive research is slow or expensive | Deduplicate cells, use profile-specific scope, persist after each cell, and keep compact digests |
| Dynamic target changes during research | Record source revision/time assumptions, detect conflicts, and require a refreshed baseline |
| Browser automation cannot reach a state | Record unavailable evidence and require a user decision; never fabricate |
| Pixel comparisons are brittle across environments | Record browser/font/environment metadata and use explicit, versioned tolerances |
| More artifacts enlarge storage | Content-addressed files, deduplication, retention policy, and manifest references |
| Large evidence enters prompts/checkpoints | Enforce external artifact references and test prompt/checkpoint sizes |
| Research phase drift returns | Generate dispatch/progress/re-entry metadata from the authoritative phase plan |
| Sensitive data leaks through screenshots or traces | Redaction before persistence, policy-bound tools, secret scanning tests |
| Gate blocks valid but unusual sites | Support explicit per-cell `not_applicable`/degraded decisions with user-visible rationale |

## 19. Open decisions

The implementation owner must resolve and document these before coding:

1. Whether the default visual tolerance should be a strict 1 CSS pixel,
   one-percent relative geometry, or a component-specific policy.
2. Which DOM measurement APIs can be exposed through the existing browser
   tools without expanding their security boundary.
3. The maximum default route/state matrix size before asking the user to
   narrow scope, while preserving the requirement that omitted cells are
   visible and intentional.
4. The retention and cleanup policy for large screenshot/evidence packages.
5. Whether the gate is agent-approved, user-approved, or requires both for the
   `production` profile.
6. How volatile, personalized, and time-dependent target content is frozen or
   represented in deterministic fixture and production runs.

Each decision must be represented in the baseline/configuration and covered
by tests; it must not be hidden in prompt wording.

## 20. Definition of done

- The authoritative reconstruct phase plan contains an evidence-complete
  research sequence and an explicit implementation gate.
- Required route, viewport, visual-state, and interaction coverage is typed,
  persisted, hash-verified, and resumable.
- Screenshots, measurements, traces, assets, and the fidelity baseline are
  linked to source cells and available to later implementation/validation
  phases.
- Missing, unavailable, stale, contradictory, and waived observations are
  distinct and visible; no unobserved behavior is presented as fact.
- `bootstrap` cannot start without valid gate approval or an explicit recorded
  degraded decision.
- Parent conversation, memory, journal, checkpoints, cache eligibility,
  browser policy, workspace policy, and re-entry contracts remain intact.
- Existing PRD-177 runs and artifacts remain resumable/readable under their
  persisted plan version.
- Unit, integration, E2E, regression, and performance tests cover every
  acceptance criterion and pass in deterministic CI.
- Documentation and public symbol inventories are updated, and no duplicate
  or backup workflow implementation exists.
