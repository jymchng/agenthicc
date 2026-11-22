---
title: "WFC-11: Automatic trust, provenance and rollback for agent-authored extensions"
status: Proposed
version: 2.0.0
date: 2026-11-22
repository: jymchng/agenthicc
priority: P0
related_prds: [WFC-00, WFC-04, WFC-05, WFC-06, WFC-07, WFC-09, WFC-14, prd-250]
tags: [security, trust, provenance, authoring, rollback]
---

# WFC-11 — Automatic trust, provenance and rollback for authored extensions

> **Decision (2026-11-22, project owner).** Newly created workflows, tools and
> skills are **trusted automatically**. There is no human approval step. This PRD
> defines the one runtime-owned path that does that, and the controls that
> replace the human gate: runner-owned validation, digest-bound trust, call-time
> capability gating, provenance and rollback. Version 1.0 of this PRD specified a
> human review overlay; it is superseded by this version.

## 1. Summary

When an authoring workflow completes, the **session** — not the model, not the
workflow runner — performs one publish step: **validate gate → commit → trust →
reload → record provenance → notify**. It runs without a prompt in interactive
and headless sessions alike. Trust is recorded only for the exact bytes that the
runner-owned verdict approved, and every published artifact can be inspected,
rolled back or removed afterwards. Operators who want a gate can opt in to
`stage_only`.

## 2. Problem (current source)

- Only `~/.agenthicc` is trusted by location. Every other Python extension
  needs a SHA-256 record in `~/.agenthicc/trust.json`, which the repository can
  never write (`plugins/trust.py`: `ExtensionTrust.trust/check/permits/revoke`).
- `load_python_workflows` returns `[]` for an untrusted package and skips a
  directory with the pending marker (`workflows/loader.py:122-140`). Tools and
  commands follow the same rule.
- **The two authoring workflows disagree about trust today.**
  - The tool workflow writes into the live `.agenthicc/tools/` directory and
    never trusts it. The file stays untrusted and does not load until the user
    leaves the session and runs `agenthicc trust`, so a tool an agent creates is
    never usable by that agent in the same session.
  - `create_workflow` already trusts automatically: `finalize_authoring` calls
    `trust.trust(<file>, kind="workflow")` for every `.py` file of the validated
    package inside the runner (`workflows/create_workflow/authoring.py:1187-1216`).
    The only human approval is for *replacing an existing* workflow
    (`create_workflow/runner.py:1071-1090`).
- Trust is bound to a file path and its digest. A runner that trusts inside a
  **worker's worktree** (WFC-04) would record a trust entry for a path that
  disappears with the worktree and never covers the file that lands in the main
  tree. Trust must therefore be recorded by the session, at the main-tree path,
  after the files arrive.
- Nothing records that a file was agent-authored, by which run, or what it
  replaced, so it cannot be diffed or rolled back.
- Validation loads generated code in the agent process:
  `create_workflow/validation.py:276-284` imports package modules through
  `load_python_workflow_modules` and removes them from `sys.modules` afterwards.
  Importing executes top-level code.

## 3. Goals

1. An artifact whose runner-owned verdict passes is committed, trusted and
   loaded with **no approval prompt**, in the same way for tools, skills,
   workflows and commands, interactive or headless.
2. Trust is recorded only by the runtime's publish step, only for the bytes the
   verdict covered; no model-facing tool can create, widen or restore trust.
3. Publication is atomic for a set of artifacts: all publish or none.
4. Every authored artifact has provenance and a restorable previous version, so
   a bad outcome is undone after the fact rather than prevented by a prompt.
5. Authored code never lands under a trusted-by-location root.
6. Validation never executes generated code in the agent process.
7. An operator who wants a gate can choose one (`stage_only`).

## 4. Non-goals

- A human review overlay or approval prompt for trust (removed by the decision
  above).
- Replacing `agenthicc trust`; the publish step writes the **same** record the
  CLI writes, for the same digest.
- Trusting third-party plugins or installed skills (`skills/installer.py`).
- Signing or remote attestation.

## 5. Design

### 5.1 Staging

A candidate must be invisible to every loader until the publish step runs. Two
mechanisms provide that, and the store (`plugins/authored.py`) fronts both with
one interface:

| Kind | Candidate lives | Invisible because | Commit | Discard |
|---|---|---|---|---|
| tool, skill, command | `.agenthicc/.staging/<run_id>/<kind>/<name>/…`, inside `WorkspaceView` | no loader scans `.staging` | move into the live directory atomically, then trust (code kinds) | delete the staging set |
| workflow | its canonical package under the existing authoring lease | the pending marker (`PENDING_MARKER_NAME`) makes the loader skip it | `finalize_authoring` (recheck fingerprint, trust, remove marker) | `restore_authoring` (remove the candidate, restore the replaced original and its trust records) |

```python
class AuthoredArtifactStore(Protocol):
    def commit(self, artifact: PublishedArtifact, *, source_root: Path | None) -> PublishedArtifact: ...
    def discard(self, artifact: PublishedArtifact) -> None: ...
```

`source_root` is set when a **worker** produced the candidate inside its
worktree (WFC-04): the store imports the files into the main workspace,
re-verifies the digest, and commits them there. Generation into either form uses
the WFC-14 scoped writer.

### 5.2 The publish step

```python
ArtifactKind = Literal["tool", "skill", "workflow", "command"]
ArtifactState = Literal["staged", "loaded", "load_failed", "rejected"]

@dataclass(frozen=True, slots=True)
class PublishedArtifact:
    kind: ArtifactKind
    name: str
    files: tuple[str, ...]            # workspace-relative, sorted
    digest: str                       # SHA-256 over the sorted (path, bytes) set
    state: ArtifactState
    declared_capabilities: tuple[str, ...]   # ToolCapability names; empty for skills
    network_hosts: tuple[str, ...]
    dependencies: tuple[str, ...]
    advisories: tuple[str, ...]       # advisory finding codes (WFC-14 warnings)
    validation: Literal["passed", "static_only"]   # static_only = no sandbox available
    supersedes: str | None            # digest of the version it replaces
```

When a `WorkflowReturn` carrying staged artifacts is popped (WFC-01), under
policy `auto` the session runs, in order:

1. **Gate.** Every artifact's `ValidationReport` has `errors == 0` and its current
   content digest equals the digest the verdict covered. Otherwise nothing is
   committed or trusted; the artifacts are discarded and the return says why.
2. **Serialise.** Take the session publish lock, so parallel workers' artifacts
   commit one at a time and a name collision between two of them fails the
   second with `name_conflict`.
3. **Commit.** `store.commit` for each artifact (§5.1) — the only place
   `ExtensionTrust.trust(path, kind=…)` is called for authored code. All or
   nothing: if moving, trusting or reloading any artifact fails, files already
   moved are restored and trust records are restored with
   `ExtensionTrust.restore_records`.
4. **Reload.** `ReloadService.reload` for the affected surfaces (WFC-05);
   artifact state becomes `loaded`, or `load_failed` with the diagnostic.
5. **Record.** Write the provenance entry (`approved_by: "policy"`) and append a
   journal record for each state change.
6. **Notify.** One non-blocking transcript block and a line in `/authored list`:
   names, kinds, short digests, declared capabilities and hosts, advisory
   findings, `static_only` if applicable, and the undo command
   (`/authored rollback <name>`).

Then the return is delivered to the parent (WFC-01 §5.4) with each artifact's
final state. This moves `create_workflow`'s existing trust call from the runner
to this single point (WFC-06 §5.2); after this PRD the only call sites of
`ExtensionTrust.trust` for authored code are inside `store.commit`.

### 5.3 Policy

`security.authored_extension_policy = "auto" | "stage_only"`, default **`auto`**.
There is no per-kind or per-mode variation: Safe, Yolo and headless behave the
same, and Plan mode cannot author at all because the authoring workflows need a
write ceiling (WFC-01 §5.5).

| Policy | Behaviour |
|---|---|
| `auto` | The §5.2 publish step runs as soon as the run completes. No prompt. |
| `stage_only` | The publish step stops after the gate and leaves artifacts `staged`. A person publishes with `/authored approve <run-id>` (TUI) or `agenthicc authored approve <run-id>` (CLI), which run steps 2–6 unchanged, or discards with `reject`. No overlay exists; this is the only path in the series that involves a person in trust. |

### 5.4 The trust boundary after this change

What still holds, and is tested:

- **Only the runtime trusts.** No registered tool references
  `ExtensionTrust.trust`, `revoke` or `restore_records`; a static test walks
  every tool module. Trust is recorded only inside `store.commit` and by the
  existing `agenthicc trust` CLI.
- **Only validated bytes are trusted.** The gate in §5.2 rechecks the digest at
  commit; a change after validation fails closed.
- **Never in a trusted-by-location root.** `~/.agenthicc` is trusted by location
  and skips the digest check. Authored artifacts are written only under the
  project `.agenthicc/…`; no authoring workflow can address `~/.agenthicc`
  (user-global placement is a non-goal; a person can move a file there).
- **Authors replace only what they authored.** A run may replace an artifact the
  manifest records as authored. Replacing a file the manifest does not know keeps
  `create_workflow`'s existing replacement approval (an overwrite guard, not a
  trust gate), and tools and skills fail `not_authored`.
- **No shadowing.** An authored artifact may not take a built-in tool, workflow,
  command or skill name (`name_conflict`).
- **Trust is not permission.** A trusted tool still runs through the capability
  gate and the approval broker on every call under Safe, Plan and Yolo; an
  authored workflow's phases run under their capability ceilings; entering a
  workflow is an ordinary approval-brokered action (WFC-02). Declared
  capabilities are checked against imports by the deterministic validator
  (WFC-07 §5.4).
- **Direct edits are not trusted.** If a trusted file later changes — including
  an agent editing it with the general file tools — the next load reports
  `untrusted_modified` and skips it. Only an authoring run (an update) or
  `agenthicc trust` re-trusts it. The file tools treat
  `.agenthicc/{tools,workflows,commands}/` as protected paths and ask the normal
  approval with the explanation "this path runs as code" — defence in depth over
  the digest check.

What does **not** hold, by decision:

- Code that passes the validator runs in the agent process with the user's
  privileges once it is called. Static checks cannot prove behaviour.
- An agent steered by hostile content it read (a web page, a repository file)
  can author and trust code without a person seeing it.

The mitigations are the ones above plus the notice (§5.2 step 6), provenance
and rollback (§5.6), and the `stage_only` option for operators whose threat
model needs a gate. The risk is documented, not hidden.

### 5.5 Validation without in-process execution

Validation that must import generated code (the create_workflow smoke import,
the "would this tool register?" check; harness lifetime rule in WFC-14 §5.5)
runs in a subprocess under the existing sandbox (`tools/sandbox.py`; Bubblewrap
on Linux), with no network, a read-only workspace, a wall-clock bound and a
bounded output. Where no sandbox is available the check is AST-only, the
artifact is marked `validation="static_only"`, and the notice says so.

### 5.6 Provenance and rollback

`.agenthicc/authored.json` records, per live artifact:
`kind, name, files, digest, run_id, session_id, authored_at, model,
approved_by ("policy" | "user"), supersedes`. Previous versions are kept under
`.agenthicc/authored/versions/<name>/<digest>/` (retention: 5 per artifact).
The file is provenance, not trust: forging it can confuse rollback but cannot
make anything load.

Commands (registered in `commands/builtins.py`): `/authored list`,
`/authored diff <name>`, `/authored rollback <name> [digest]`,
`/authored remove <name>`, and under `stage_only` `/authored approve|reject
<run-id>`. Rollback restores the prior files, revokes the newer trust record,
restores the older record, then reloads. The CLI gains
`agenthicc authored list|approve|reject <run-id>`.

### 5.7 Drift

A hand edit after publication is not an authoring run and is not auto-trusted
(§5.4). The next load reports `untrusted_modified` with the new digest; the
operator re-runs authoring as an update, or runs `agenthicc trust`.

## 6. Requirements

- **FR-1** `PublishedArtifact.digest` is computed identically by the gate, the
  trust record and the manifest (one function, one test vector).
- **FR-2** The commit rechecks the digest the verdict covered; publishing after
  the staged bytes change is impossible.
- **FR-3** One publish step per authoring run, not per file.
- **FR-4** The notice, `/authored` output and journal contain no file bodies
  beyond the diff of changed lines.
- **FR-5** Skills never receive a trust record (they are text) but are always
  committed through the same step and listed in the manifest.
- **FR-6** Every state transition appends a record to the session journal for
  audit.
- **FR-7** No authoring workflow can write under `~/.agenthicc`; a test attempts
  it through the scoped writer and the store.

## 7. Acceptance criteria

| # | Criterion |
|---|---|
| A1 | A scripted agent runs `create_tools`; with default config and **no prompt of any kind**, the tool is trusted (`ExtensionTrust.permits`), loaded, and the resume turn can call it. |
| A2 | The same holds for `create_workflow` and `create_skills`, interactive and headless; headless emits one `authored_published` JSON line per run. |
| A3 | A set where one artifact's verdict has an error commits and trusts **nothing** and discards the set. |
| A4 | A failure while moving the second of three artifacts restores the first, restores trust records, and publishes nothing. |
| A5 | Changing a staged file after validation makes the gate fail; nothing is trusted. |
| A6 | Two parallel workers publishing the same tool name: the first commits, the second fails `name_conflict`, and neither run corrupts the other. |
| A7 | Policy `stage_only`: artifacts stay `staged` and unloaded; `/authored approve <run-id>` publishes through the same steps; `reject` discards with no trust record. |
| A8 | The static no-trust-in-tools test fails if any tool module imports or calls the trust mutators. |
| A9 | A static scan of `src/` finds `ExtensionTrust.trust` / `trust.trust` called only from `store.commit` (including `finalize_authoring`) and the `agenthicc trust` CLI; the `create_workflow` runner no longer calls it. |
| A10 | Editing a published tool file by hand yields `untrusted_modified` on the next reload and the tool is not on the surface. |
| A11 | `/authored rollback` restores the previous digest and a reload shows the previous behaviour. |
| A12 | No authoring path can write or trust a file under `~/.agenthicc` (FR-7). |
| A13 | A published tool declaring `execute` still triggers the normal approval prompt on each call in Safe mode (trust does not bypass the gate). |
| A14 | With Bubblewrap absent the artifact shows `static_only`; with it present, generated code is never imported in the agent process (test asserts the module is absent from `sys.modules`). |

## 8. Test plan

- Unit: digest vectors, state machine, policy matrix, protected-path matching,
  the no-`~/.agenthicc` rule.
- Integration: real `ExtensionTrust` against a `tmp_path` store, atomic publish
  with injected failure at each step, parallel-worker collision, rollback, drift
  (`tests/integration/test_authored_publish.py`).
- Security: the static tool-module scan and the call-site scan; capability-gate
  behaviour for a published tool; sandbox smoke on Linux CI (`bubblewrap`
  already required).
- Command tests for picker visibility and execution (`/authored …`).

## 9. Documentation and cleanup

`docs/guides/security.md` — state plainly that authored extensions are trusted
automatically, what still holds (§5.4), what does not, and how to choose
`stage_only`; `plugins.md`, `commands.md`, `llms-full.txt`; `agenthicc trust`
docs cross-link, noting that the publish step writes the same record. The
runner-side `trust.trust` call in `create_workflow`'s finalization path is
removed from the runner (it remains inside `finalize_authoring`, reachable only
from `store.commit`).

## 10. Risks

| Risk | Mitigation |
|---|---|
| An agent steered by hostile input authors and trusts harmful code with no person in the loop. | Accepted by decision. Bounded by the runner-owned verdict and sandboxed import validation, digest-bound trust, the call-time capability gate and approvals, no writes under `~/.agenthicc`, the notice, provenance and one-command rollback, and the `stage_only` opt-in. |
| A skill is a persistent prompt-injection vector. | Provenance, listing, `/authored remove`, advisory lint in the notice, and model-invoked bodies are source-labelled (WFC-10). |
| Validator false negatives let unsafe code through. | Findings are conservative and stable-coded; the sandboxed would-load preview; capability checks at call time are independent of the validator. |
| Sandbox unavailable on a platform. | `static_only` is recorded and shown; `stage_only` remains available. |
| Silent publication surprises the operator. | A non-blocking notice on every publish and `/authored list`; headless emits a JSON line. |

## Amendment — 2026-11-22

The Linux import spike passed with Bubblewrap. The existing Bubblewrap
process isolation and bounded execution live in
`src/agenthicc/worktrees/environment.py`. `tools/sandbox.py` owns workspace
path policy. The worktree command entry point mounts its worktree writable,
so the import harness needs a read-only mount with its own limited runtime
paths and a cleared environment. The spike verified an import, denial of a
write, a private network namespace with only loopback, a 4 KiB output limit,
and wall-clock termination. Keep the AST-only fallback and `static_only`
notice for platforms without the subprocess sandbox. Evidence is in
`tests/integration/test_authored_import_spike.py`.

## Amendment — 2026-11-22

The trust store authorizes one resolved file at a time, so authored trust
records keep their original file SHA-256 and also carry the complete artifact
digest. Each file hash is checked against the verified byte set before the
record is written. The project provenance file is a view. Approval tickets,
rollback identities and recovery snapshots live in the operator-owned
directory beside the trust store.

Publication uses atomic replacement into the live directory and retains staging
until the commit is durable. This works when a worker stages files on another
filesystem. A pending journal entry hides the provisional files from other
loaders. The next store operation can recover a process that exits during
installation. Tests cover a process exit, real reload rollback, and a modified
archived version.

The live tree also has operator-initiated trust actions in the Agents Hub and
the Markdown agent library. The hub requires explicit confirmation and the
library creates the definition submitted by the operator. The static scan
accounts for these existing operator actions. Authored publication uses the
store and canonical finalizer. The last runner-side finalizer call moves in
WFC-06.

## Implementation evidence

| Criterion | Test or delivery dependency |
|---|---|
| A1 | `test_session_publisher_loads_validated_tools_and_emits_one_notice` exercises the real loaders. The `create_tools` workflow and resume-turn case land in WFC-07 and WFC-08. |
| A2 | `test_canonical_workflow_approval_removes_marker_and_reloads` and `test_skill_discovery_hides_provisional_set_without_recording_python_trust`. The workflow-driven TUI and headless cases land with WFC-01, WFC-06 and WFC-09. |
| A3 | `test_failed_runner_verdict_discards_entire_set` |
| A4 | `test_moving_second_file_failure_restores_first_and_trust`, `test_load_failure_keeps_the_prior_surface_and_reverts_the_set`, `test_workflow_reload_failure_restores_the_pending_lease` |
| A5 | `test_changed_staged_bytes_fail_closed`, `test_authored_trust_rechecks_bytes_and_records_the_set_digest` |
| A6 | `test_two_publishers_allow_only_one_owner_of_the_same_name` |
| A7 | `test_authored_picker_and_operator_commands_restore_previous_behavior`, `test_authored_cli_lists_approves_and_rejects_staged_runs` |
| A8 | `test_model_tool_modules_do_not_reference_extension_trust_mutators` |
| A9 | `test_extension_trust_writes_are_owned_by_runtime_and_explicit_operator_actions`. The legacy runner call remains until WFC-06. |
| A10 | `test_reload_removes_the_only_tool_if_its_trusted_bytes_changed` |
| A11 | `test_authored_picker_and_operator_commands_restore_previous_behavior`, `test_rollback_refuses_changed_archive_content` |
| A12 | `test_store_refuses_authoring_below_the_user_root`, `test_scoped_writer_cannot_author_under_the_user_root` |
| A13 | `test_published_execute_tool_requires_a_safe_approval_for_each_call` |
| A14 | `test_without_bubblewrap_validation_is_static_and_does_not_import`, `test_validation_executes_only_inside_the_read_only_network_namespace`, and the publication receipt check in `test_published_execute_tool_requires_a_safe_approval_for_each_call`. |
