---
title: "PRD-260: Scoped Generation and Reviewed Publication for make_agenthicc_tool"
status: Proposed
version: 0.1.0
created: 2026-11-01
scope: "make_agenthicc_tool phase permissions, generated artifact staging, overwrite handling, and activation UX"
related_prds:
  - PRD-24 # Tool plugin discovery
  - PRD-27 # Tool plugin security
  - PRD-76 # Tool capability gate
  - PRD-147 # Workflow-native extension authoring
  - PRD-152 # Agent-executable create_* authoring
  - PRD-155 # Consolidated Safe, Plan, and Yolo modes
  - PRD-168 # Mode-aware parent-workspace access
  - PRD-250 # Code quality and extension trust hardening
  - PRD-259 # Deterministic make_agenthicc_tool validation

tags:
  - workflows
  - tool-plugins
  - security
  - permissions
  - publication
---

# PRD-260 — Scoped Generation and Reviewed Publication for `make_agenthicc_tool`

## 1. Summary

Make `make_agenthicc_tool` generate an isolated candidate with the minimum
necessary permissions, validate and present that candidate for review, and only
then publish it into `.agenthicc/tools/`. The workflow must not elevate a Safe
or Plan session to Yolo, must not grant the generation agent the full session
tool bundle when it only needs to author one file, and must not overwrite an
existing project tool without explicit approval.

Publishing a candidate and trusting it are separate user decisions. A
successfully published project plugin remains untrusted until the user reviews
its exact bytes and records trust through the existing `agenthicc trust add`
flow. Static validation does not remove that boundary.

## 2. Evidence-backed current state

`MakeToolRunner._generate()` calls the inherited
`CodePlanRunner.run_phase(..., mode="Yolo")`; the declarative phase metadata
also sets `mode_override="Yolo"`. In `CodePlanRunner._run_turn()`, a phase
override is activated in the session `ModeManager`, and `_base_tools(mode)`
filters against that override. `phase_mode()` resolves the requested mode
rather than restricting it to the caller's mode. As a result, invoking this
workflow while the user selected Safe or Plan runs generation with Yolo's
unrestricted capability and approval policy, then restores the previous mode.

The generation prompt asks the model to write directly to
`.agenthicc/tools/<tool_name>.py`. The phase receives the normal session tool
bundle, including any applicable filesystem, execution, network, Git, MCP, and
memory tools. The prompt constrains intended behavior, but does not enforce a
file-level write scope.

`confirm_generation_complete()` accepts any path containing the text
`.agenthicc/tools/`; it does not require the planned filename, resolve the path,
reject symlinks, or check for an existing target. The workflow writes directly
into the recursively scanned plugin directory before validation. A failed,
rejected, interrupted, or malicious generation can therefore leave executable
source in the discovery tree, and a same-name tool can be replaced before the
user has reviewed the new bytes.

The canonical loader correctly skips untrusted project extensions before
import. The current finalize prompt nevertheless says to reload the tool and
does not tell the user to review and explicitly trust the generated file first.

## 3. Problem

A workflow request to create one plugin is not consent to bypass the selected
mode, use unrelated tools, modify arbitrary workspace files, or replace an
existing executable extension. Prompt text is not an authorization boundary.
Direct writing into the active plugin directory also conflates candidate
generation with publication. Even a structurally valid candidate needs human
review before it becomes an active extension, and a separate digest-bound trust
decision before the loader imports it.

## 4. Goals

1. Keep the user's effective mode, approval requirements, and workspace ceiling
authoritative throughout the workflow.
2. Give generation only a minimal, file-scoped authoring surface; no implicit
   access to execution, network, Git-write, arbitrary MCP, or unrelated tools.
3. Write candidates outside the plugin loader's discovery tree.
4. Derive the candidate and publication paths from validated workflow state,
   not from an arbitrary model-supplied path.
5. Make publication explicit, atomic, and non-destructive by default.
6. Keep human review, publication approval, and extension trust as distinct
   boundaries.
7. Preserve staged candidates safely across interruption and resume.

## 5. Non-goals

- Changing the global semantics of mode overrides for every workflow; this PRD
  removes the `make_agenthicc_tool` elevation and requires its operations to
  obey the current session policy.
- Treating workflow invocation itself as approval to write, overwrite, execute,
  install dependencies, or trust generated code.
- Importing a generated module during validation or publication.
- Automatically recording extension trust after publication.
- Replacing the existing workspace path policy, capability gate, approval
  service, extension loader, or digest-bound trust store.
- Publishing to user-global `~/.agenthicc/tools/`; this workflow targets the
  current project's tool extension directory only.

## 6. User-facing lifecycle

```text
ANALYZE
  -> preflight mode, workspace, and target collision
GENERATE
  -> use a dedicated writer restricted to this run's staging artifact
VALIDATE
  -> run PRD-259's non-executing structural validator
REVIEW
  -> show exact candidate/diff and deterministic report; obtain explicit
     publication approval
PUBLISH
  -> atomically promote the approved candidate to the canonical project path
FINALIZE
  -> report the published but still-untrusted tool and exact trust/reload steps
```

The workflow may retain its existing typed runner and phase infrastructure,
but it must represent staging, review approval, and publication explicitly in
its state/checkpoint model. If the user declines publication, the candidate is
not placed under `.agenthicc/tools/` and is reported as unpublished.

## 7. Functional requirements

### FR-1 — No implicit Yolo elevation

Remove the hard-coded `mode="Yolo"` and `mode_override="Yolo"` from this
workflow. Every tool call remains subject to the user's current mode, capability
gate, approval service, workspace scope, and any parent/session policy. A mode
change made by the user during a phase must take effect at the next tool call;
the workflow must not restore a stale snapshot over a newer user choice.

Before generation, perform a permission preflight:

- In Safe, continue only through the normal per-operation approval path.
- In Plan, do not silently enable writes; explain that generation requires the
  user to switch to a mode that permits the requested write, then stop or wait
  for an explicit user-driven mode change according to the session contract.
- In Yolo, continue under the user's explicit selection, but retain the narrow
  tool and path scope below.
- In headless runs, use the configured effective mode and fail closed when the
  required write is not authorized; do not infer permission from workflow name.

The generator must never change the mode itself to obtain permission.

### FR-2 — Minimal generation tool surface

The GENERATE phase receives only the tools required to write the candidate,
inspect the relevant plan, and submit its phase transition. Its writer must use
the shared workspace authorization boundary and must be bound to the current
run's candidate path. It must reject:

- paths outside the dedicated staging root;
- absolute paths, `..` traversal, symlinked components, and path changes after
  authorization;
- writes to `.agenthicc/tools/` or any other active extension directory;
- writes to files other than this run's candidate artifact.

The phase must not receive general shell/process execution, network, Git-write,
MCP, or unrelated plugin tools. Reads, if needed, remain workspace-scoped and
capability-gated. Validation is static per PRD-259 and does not need an
interpreter or import tool.

Use an explicit phase tool allowlist or a session-owned dedicated authoring
writer; do not rely on prompt instructions to restrict the ordinary session
bundle. The selected path and current workspace scope remain runner-owned.

### FR-3 — Candidate staging and identity

Derive one canonical candidate identity from the run ID and normalized tool
name. Store it under a dedicated staging directory outside
`.agenthicc/tools/`, for example `.agenthicc/.staging/tools/<run_id>/<tool_name>.py`.
The precise directory name may follow an existing shared authoring convention,
but the active tool loader must not discover staged candidates.

Do not accept a model-supplied `file_path` as authority. The completion tool
signals only that generation has finished and supplies a bounded summary; the
runner reads and validates the candidate at the path it derived. Any symlink,
missing file, unexpected path, or target identity change fails the handoff.

Stage writes must be atomic or recoverable: an interrupted write must not be
mistaken for a complete candidate. The checkpoint records the candidate path,
run identity, and source digest after a successful write, but does not treat
those fields as validation or publication approval.

### FR-4 — Collision and overwrite policy

Before generation, inspect the canonical publication target through the
workspace boundary. If it does not exist, proceed to staging. If it exists,
never overwrite it implicitly. Present the existing target identity and require
an explicit user decision to choose another valid name or approve replacement.

When replacement is approved:

- preserve the original until the new candidate has passed validation and the
  user has reviewed the exact diff;
- publish atomically so readers see either the complete old file or the
  complete new file;
- do not follow a symlinked target or parent component;
- preserve a recoverable copy or transaction record sufficient to restore the
  previous file if publication fails; and
- do not preserve its trust record for changed bytes. The existing digest-bound
  trust service must require trust again for the replacement.

If overwrite is declined, do not write the target and do not report success.

### FR-5 — Review and publication approval

After PRD-259 validation passes, show the user the candidate source or a
bounded, complete review surface and the deterministic report. Require an
explicit publication decision before promotion. A model-generated approval
summary, successful validation report, original request, or trust record for an
older digest is not publication approval.

If validation fails, the candidate may be repaired only in staging. If review
is declined, the workflow leaves it unpublished and gives the user a safe way to
inspect or discard the staged artifact. No candidate is imported during review.

### FR-6 — Trust-aware activation instructions

After publication, report separately:

1. the canonical file path and structural validation result;
2. that the published code has not been imported or semantically proven;
3. that project extensions remain untrusted until reviewed and explicitly
   trusted; and
4. the exact commands to review/trust the single file and then reload, e.g.:

   ```text
   agenthicc trust list
   agenthicc trust add .agenthicc/tools/<tool_name>.py
   /tools reload
   ```

The workflow must not invoke `agenthicc trust add`, write to the trust store,
mark the file trusted, or promise that `/tools reload` will load an untrusted
file.

### FR-7 — Resume and cleanup

Checkpoint the current lifecycle state, candidate identity, digest, collision
decision, validation result reference, and publication approval. Resume must
revalidate the current candidate bytes and must not repeat publication if the
same run already committed it. A changed candidate invalidates validation and
publication approval. Failed or abandoned runs may retain a clearly identified
staged candidate for user recovery, but staging files must never be loaded as
plugins.

## 8. Acceptance criteria

1. Running `make_agenthicc_tool` from Safe does not set the active mode to Yolo;
   write approval follows the normal Safe approval path.
2. Running it from Plan never exposes an unapproved write path or silently
   changes mode; the user receives an actionable stop/wait message.
3. A user-selected Yolo mode still cannot make the generation writer write
   outside this run's candidate path.
4. The GENERATE tool schema contains no general command, network, Git-write,
   MCP, or unrelated plugin tool.
5. A model-supplied path such as `/tmp/.agenthicc/tools/x.py`, a path containing
   `../`, an alternate filename, and a symlinked path cannot complete generation.
6. An incomplete or interrupted candidate is not considered generated and is
   not visible to plugin discovery.
7. An existing target remains byte-identical unless explicit overwrite approval
   is recorded; rejected, failed, and interrupted runs cannot replace it.
8. Publication is atomic and a failure preserves a recoverable original.
9. Publication does not add extension trust; the loader skips the result until
   the user trusts its exact digest.
10. Editing a candidate after validation or review invalidates both the report
    and publication approval.
11. Resume after publication reports the existing committed artifact without
    publishing it a second time; resume before publication rechecks path, digest,
    and approval state.
12. The final message gives the actual trust/reload sequence and does not claim
    that the plugin is already loaded or trusted.

## 9. Test plan

- Mode-policy tests for Safe, Plan, and Yolo, including a user mode change during
  the generation phase and a headless run without an approval broker.
- Tool-bundle tests proving only the candidate writer, necessary scoped reads,
  and phase transition are visible to generation.
- Path tests for absolute paths, traversal, alternate filenames, symlinked
  parents/targets, race-time replacement, and out-of-workspace paths.
- Collision tests for no-overwrite default, explicit replacement approval,
  declined replacement, atomic promotion failure, and digest trust invalidation.
- Lifecycle tests proving no stage file is discovered, no candidate is imported,
  and no trust store is mutated by any phase.
- Resume/interruption tests before stage write, after write, after validation,
  after review approval, and after atomic publication.
- Documentation tests or prompt assertions for the separate publication,
  review, trust, and reload steps.

## 10. Rollout and migration

1. Remove the make-tool-specific Yolo override and establish the supported
   phase-scoped tool surface.
2. Add staging and collision preflight before exposing a writer.
3. Integrate the deterministic validator from PRD-259.
4. Add user review and atomic publication as explicit lifecycle boundaries.
5. Update checkpoint codec, finalization copy, docs, and end-to-end coverage.

Do not keep a direct-write fallback, trust generated files implicitly, or add a
second plugin loader. Existing user files are not migrated or overwritten.

## 11. Security and operational considerations

- A workflow's capability and path scope is an authorization control, not a
  prompt convention. Mode policy, workspace access, and approval must be checked
  at the operation boundary.
- Trust is not a sandbox. The post-publication trust action permits import of
  the exact reviewed bytes but does not isolate their runtime behavior.
- The workflow must not copy secrets into generated source or stage files. Any
  candidate diff and failure report shown to the user must remain bounded and
  redact known secret material.
- Atomic publication and digest checks must use the same canonical target
  identity and resist symlink or time-of-check/time-of-use substitution.
