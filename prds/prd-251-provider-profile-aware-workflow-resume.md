---
title: "PRD-251: Provider-Profile-Aware Workflow Resume"
status: Implemented
version: 1.0.0
date: 2026-10-24
scope: "Workflow recovery across TUI, headless, CLI, and background-session execution"
related_prds: [prd-162, prd-170, prd-186, prd-196, prd-237, prd-248, prd-249]
tags: [workflows, resume, provider-profiles, configuration, recovery]
---

# PRD-251 — Provider-Profile-Aware Workflow Resume

## 1. Executive summary

A user attempting to continue session `b4f837d3-3dd1-437f-81dc-cf4280775418`
received:

```text
Cannot resume 'ea567f93604d4fccb5e4f91257e40792':
provider_profile_mismatch: checkpoint requires provider profile 'opencode_go'
```

The workflow checkpoint is valid enough to be identified, but recovery refuses
to use it because it was recorded with the named connection profile
`opencode_go` while the current session configuration resolves to another
profile (or to no named profile). The workflow has not restarted at its first
phase: it is blocked before the saved run can be rehydrated and dispatched.

This behavior comes from an intentional safety check, but the recovery UX is
incomplete. Agenthicc neither automatically selects the checkpoint's original
profile for an ordinary resume nor provides a first-class, auditable way to
resume the same workflow with a deliberately changed profile. That also
conflicts with PRD-196, which requires users to be able to correct provider
configuration and resume the same durable run.

This PRD defines provider-profile selection as part of workflow-resume
preparation. By default, a resume uses the checkpoint's saved profile. A user
may explicitly choose another profile for that resume. Both paths resolve the
current profile configuration and secrets at runtime, retain the existing
workflow run and phase, and fail safely if the selected profile is unavailable
or invalid. The checkpoint remains available for inspection and retry.

## 2. Evidence and diagnosis

### 2.1 Confirmed source behavior

`WorkflowCheckpoint` stores a non-secret `provider_profile` string.
`WorkflowRecoveryRecord.provider_profile` exposes that checkpoint value.
`WorkflowRecoveryCoordinator.inspect()` passes the caller's current profile to
`_validate_recovery()`. The validator rejects a recoverable checkpoint when
both values are non-empty and differ:

```text
checkpoint.provider_profile != current provider_profile
    → provider_profile_mismatch
```

The profile value is supplied from `session.cfg.execution.profile` by the
foreground TUI and headless resume paths, and from the current session
configuration by background-worker recovery. This is a name comparison; it is
performed before `rehydrate()` returns a workflow handle and before the
workflow's `resume(context)` entry point runs.

The selected display label `openai/deepseek-v4.1-flash` does not establish
profile equivalence. Multiple named profiles can use the OpenAI-compatible
transport and the same model name while targeting different URLs, credentials,
headers, or routing behavior.

### 2.2 Session evidence and limits

The session event projection contains a `workflow_paused_after_error` event
for run `ea567f93604d4fccb5e4f91257e40792`, which confirms that the run was
paused and checkpointed before this resume attempt. The supplied diagnostic
identifies the checkpoint's required profile as `opencode_go`.

The durable checkpoint file itself was not readable in this inspection
environment, so this investigation does not assert what the session's active
profile was at the time of the failed resume, whether the profile was changed
intentionally, or whether the saved named profile still exists in the current
configuration. The implementation must distinguish those cases rather than
guessing from the model label.

### 2.3 Causal sequence

```text
workflow turn fails or process is interrupted
  → failure finalizer persists checkpoint.provider_profile = "opencode_go"
  → user restarts/continues the Agenthicc session
  → session loads its current layered execution configuration
  → recovery inspects checkpoint using current execution.profile
  → profile names differ (including saved name vs empty current name)
  → recovery marks the checkpoint non-recoverable for this attempt
  → /workflow resume or automatic continuation cannot rehydrate it
```

This is a provider-profile selection/recovery policy problem, not evidence of
workflow phase corruption, a bad phase index, or a request to start a new run.
Raising retries or timeouts cannot resolve it. Removing the guard would be
unsafe because a profile can change the endpoint and authentication boundary.

## 3. Problem statement

Workflow checkpoints are bound to a provider profile, but the current resume
path treats the process's present profile as authoritative without determining
whether it is merely the new default or an explicit user choice. As a result:

1. A config default change can strand a valid checkpoint even when its saved
   profile is still configured and usable.
2. An intentional provider/profile change, such as switching away from a
   failing endpoint, is rejected even when the user is trying to follow
   PRD-196's configuration-correction flow.
3. An empty current profile is reported as a mismatch with a named saved
   profile, but the error does not clearly explain that empty means “use the
   legacy execution settings” rather than “restore the original profile.”
4. The displayed provider/model can look unchanged even though profile-specific
   endpoint and authentication settings differ.
5. The error reports the required saved profile but gives no supported resume
   command or recovery choice.

## 4. Goals

1. Resume an eligible workflow with the same session ID, run ID, context,
   conversation, phase, receipts, and artifacts.
2. Use the checkpoint's named profile by default when resuming, even if the
   current project/user default has since changed.
3. Support an explicit, validated profile override for recovery after a model,
   endpoint, or provider failure.
4. Resolve current secret references and credentials at resume time; never
   persist credential values in workflow checkpoints or recovery diagnostics.
5. Apply one resolved profile consistently to all provider calls made by the
   resumed workflow, including phase agents, subagents, tools, retries, and
   background/headless execution.
6. Keep unavailable, malformed, or incompatible configurations fail-closed
   while preserving the checkpoint for later recovery.
7. Give actionable diagnostics that distinguish automatic inheritance,
   explicit override, missing profile, and invalid profile configuration.
8. Keep legacy checkpoints without a saved profile resumable under the
   existing effective configuration.

## 5. Non-goals

- Persisting API keys, authorization header values, cookies, or resolved secret
  material in checkpoints.
- Silently falling back to a different provider when the saved profile is
  unavailable or fails authentication.
- Treating matching provider/model strings as proof that two profiles are
  equivalent.
- Changing workflow topology, phase cursor, or context-identity validation.
- Starting a replacement workflow when a saved run cannot be resumed.
- Allowing a profile to bypass mode, approval, sandbox, workspace, or tool
  capability restrictions.
- Changing a user's global/project provider default merely to resume one run.

## 6. Proposed behavior

### 6.1 Resume profile selection policy

Introduce one shared, typed resume-profile resolution step used by TUI,
headless, CLI, and background-worker recovery. It runs after the checkpoint is
identified but before a provider agent is constructed or any model request is
made.

| Checkpoint | Resume request | Effective profile |
|---|---|---|
| Has named profile `P` | No explicit resume override | Resolve and use saved profile `P` |
| Has named profile `P` | Explicit override `Q` | Resolve and use `Q`; record that this run was deliberately switched |
| Has named profile `P` | Explicit override selects legacy/no named profile | Use current legacy execution settings only after explicit selection and strict validation |
| Has no profile (legacy checkpoint) | No explicit override | Use current effective profile/configuration, preserving existing behavior |
| Has named profile `P` | `P` is missing or invalid, no override | Do not call a provider; preserve checkpoint and offer an explicit override/configuration repair |
| Any checkpoint | Explicit override is missing or invalid | Do not call a provider; preserve checkpoint and report the validation failure |

Only a resume-scoped choice counts as explicit. TUI and background workflow
commands accept `/workflow resume [run-id] --profile <name|none>`. An
invocation-level `--set execution.profile=<name>` is also explicit where its
CLI provenance is available. `none` explicitly selects the legacy execution
settings. A config file's newly changed default alone does not override a
checkpoint's saved profile.

### 6.2 Execution and checkpoint semantics

1. Resolve the selected profile using the current layered configuration and
   current environment. Do not reuse secret values captured when the
   checkpoint was written.
2. Build or reconfigure the provider runtime from that resolved profile before
   dispatching the resumed phase. A recovery inspection must not make a
   provider request.
3. Rehydrate and execute the exact saved workflow run. Do not call
   `runner.run(original_intent)`, create a new run ID, replay INIT, or discard
   completed phase output.
4. Persist the selected profile binding in the claimed run's `resuming`
   checkpoint before dispatching provider work. Keep the original profile name
   as provenance and record profile switches in bounded, non-secret history.
   This makes the choice durable even if the process exits before the first
   resumed phase boundary.
5. If that checkpoint write fails, restore the in-memory paused state and
   profile metadata, release the claim, and do not dispatch or publish a
   successful profile-selection event. Once the `resuming` checkpoint is
   written, recovery uses the persisted effective binding rather than silently
   reverting to another profile.
6. Do not mutate global, user, or project configuration as a side effect of
   profile inheritance or an override scoped to one resume.

### 6.3 Profile identity and configuration drift

A profile name alone is insufficient to explain the effective connection.
Add a bounded non-secret profile binding to checkpoint/recovery metadata. It
should identify at least the selected profile name, provider family, normalized
endpoint identity, and model selection. If a fingerprint is used, it must be
computed from a canonical representation that excludes resolved secrets and
secret values. Secret reference names may be represented only if doing so is
safe and useful; raw values must never be included.

Credential rotation must not invalidate a checkpoint solely because a secret
value changed. A changed endpoint/provider binding under the same profile name
must be visible in diagnostics and treated according to the resume policy:
inherit the current definition for the saved name by default, but disclose the
non-secret drift; an explicit resume override is required when the connection
identity changes in a way that could redirect the request. This behavior must
be deterministic and covered by tests.

### 6.4 Diagnostics and recovery UX

Replace the generic mismatch-only message with a bounded explanation, for
example:

```text
Workflow 'goal_flow' is saved with provider profile 'opencode_go', but that
profile is unavailable in the current configuration. No provider request was
sent and the checkpoint is preserved. Restore the profile or explicitly
resume with another profile.
```

For an explicit profile mismatch, report both profile names and explain that
the run ID and saved workflow state are retained. Provide the supported
resume-scoped override syntax. The startup preview identifies the saved
profile or notes that a legacy checkpoint uses current settings. Diagnostics
must not print endpoint credentials, headers, environment values, full
checkpoint context, or provider payloads.

Keep stable machine-readable codes, at minimum:

| Code | Meaning |
|---|---|
| `provider_profile_unavailable` | Saved profile name is absent from current configuration and no override was selected |
| `provider_profile_invalid` | Selected profile exists but fails strict validation or secret resolution |
| `provider_profile_override_required` | A potentially unsafe profile binding change needs explicit user intent |
| `provider_profile_override_invalid` | The explicit replacement profile is invalid |
| `provider_profile_mismatch` | Compatibility alias for older consumers; no longer the only user-facing distinction |

### 6.5 Scope and ownership

The shared resume-profile decision must be consumed by:

- `--resume` and `--continue` foreground startup;
- `/workflow resume [run-id]` in the TUI;
- headless workflow resume;
- detached/background worker recovery and queued recovery inputs; and
- any resume selection or preview UI that reports whether a checkpoint is
  recoverable.

All consumers must use the same resolver and must not independently implement
profile precedence. A recovery preview must show the selected profile name
and whether it is inherited or overridden, without displaying secrets.

## 7. Compatibility and migration

- Existing profile-less checkpoints remain readable and use the current
  configuration as before.
- Existing checkpoints containing `provider_profile` remain readable; no
  destructive migration is allowed.
- If a new non-secret binding field is added, old checkpoints receive a
  defined “binding unknown” interpretation rather than being rejected.
- Profile inheritance applies only to resuming a saved workflow. Fresh turns
  and fresh workflow runs continue to use the current configured default.
- Existing explicit provider configuration remains supported. The new
  resume-specific override must not alter unrelated subsequent turns unless
  the user separately changes normal configuration.
- The original checkpoint remains inspectable and resettable after any
  profile-resolution failure.

## 8. Security, privacy, and reliability

1. Never serialize API keys, authorization values, resolved secrets, request
   headers, or full provider configuration into workflow state.
2. Resolve secrets from the current environment/secret store at the last
   responsible moment and redact exceptions before persistence or display.
3. Do not contact the saved or replacement endpoint during inspection,
   preview, or profile validation unless the user explicitly invokes a
   separate connectivity test.
4. A failed profile resolution must leave the original run ID, checkpoint,
   conversation journal, phase state, and artifacts untouched.
5. Profile selection must not weaken workspace identity, workflow fingerprint,
   topology, journal-integrity, ownership-claim, or approval validation.
6. Claiming and resuming remain single-owner operations. The profile decision
   must be bound to the claimed run revision to prevent a concurrent process
   from changing it between preview and execution.
7. Switching profiles must not duplicate a previously committed phase or
   repeat non-idempotent tool effects.

## 9. Acceptance criteria

1. A checkpoint saved with `opencode_go` resumes with that profile by default
   even when the current project/user default has changed, provided the saved
   profile still resolves and validates.
2. A current empty profile does not get mistaken for an explicit request to
   abandon the checkpoint's named profile.
3. If the saved profile is absent, recovery sends no provider request, retains
   the checkpoint, and explains how to restore it or choose an override.
4. A user can explicitly resume the same run with another valid profile after
   a provider/model/endpoint failure; the run ID, session ID, phase, typed
   context, conversation cursor, and completed artifacts remain unchanged.
5. The explicit override is applied uniformly to all provider work in the
   resumed workflow and is visible as a non-secret profile decision.
6. An invalid override fails before provider execution and leaves the
   checkpoint available for another attempt.
7. Secret values are freshly resolved at resume time; rotating a credential
   does not require rewriting the checkpoint and does not expose the secret.
8. Same-name profile endpoint/provider drift is surfaced using non-secret
   metadata; credentials are never part of the drift fingerprint.
9. Legacy checkpoints with no profile remain resumable under the current
   effective configuration.
10. Fresh runs continue to use current configuration and are unaffected by
    saved workflow profiles in the session.
11. TUI recovery previews and errors show the effective profile selection and
    its source (`saved`, `explicit override`, or `current config` for a legacy
    checkpoint), but never show secrets.
12. TUI, headless, `--continue`, `--resume`, `/workflow resume`, and detached
    background recovery all apply the same profile precedence.
13. Profile resolution does not bypass existing workflow, topology,
    conversation-integrity, workspace, or session-ownership validation.
14. A provider failure immediately after switching profiles preserves a
    recoverable checkpoint and does not restart the workflow from its first
    phase.

## 10. Test plan

### Unit tests

- Saved profile equals current default: inherit and resolve saved profile.
- Saved profile differs from config default: inherit saved profile unless the
  resume request carries an explicit override.
- Empty/default profile versus named saved profile is handled by explicit
  provenance, not raw string inequality.
- Explicit alternate profile, explicit legacy settings, missing profile,
  malformed profile, and secret-resolution failure.
- Same-name endpoint/provider changes produce deterministic non-secret drift
  diagnostics; secret rotation does not affect the binding fingerprint.
- Legacy checkpoint without profile retains existing resolution behavior.
- No secret values appear in checkpoint JSON, recovery records, logs, events,
  and error strings.

### Integration tests

- Disk-backed checkpoint saved with profile A is resumed with profile A after
  the configured default changes to B.
- Explicit override resumes the exact same run and starts at its saved phase.
- Missing/invalid saved profile and invalid override make zero provider calls
  and preserve checkpoint bytes/revision and workflow artifacts.
- Provider object construction receives the resolved profile before the first
  resumed agent turn; workflow, phase, and subagent calls share it.
- Profile selection remains correct through TUI, headless, background worker,
  session continuation, and workflow-resume command paths.
- A crash between profile selection and a durable resumed phase boundary does
  not silently select another profile on restart.

### End-to-end tests

- Create and pause a workflow under profile A; restart with profile B as the
  default; continue and verify the same workflow run resumes under A.
- Repeat with an explicit profile B override; verify the same run and phase
  continue under B and that the override is reported.
- Remove profile A; verify a clear actionable recovery state, no provider
  request, and successful recovery after an explicit valid override.
- Exercise the workflow after a transient/permanent provider error and verify
  no phase replay, duplicate tool effect, or loss of completed artifacts.

All tests must use mocked provider transports and temporary stores; no live
provider credentials or external calls are required.

## 11. Rollout and observability

1. Add the shared typed profile-resolution policy and compatibility handling
   before enabling it in each resume entry point.
2. Emit bounded recovery metadata describing profile source and outcome, with
   no endpoint secrets or request headers.
3. Keep the legacy mismatch code readable by existing clients while migrating
   new UI to the more specific error codes.
4. Add tests for all entry points before changing the default precedence.
5. Document resume profile inheritance and override syntax in the provider
   profile and workflow recovery guides.

## 12. Definition of done

The PRD is implemented when all acceptance criteria pass; every resume path
uses the same profile resolver; old and new checkpoints remain readable; no
secret values enter durable state; provider/profile errors are actionable; and
the same workflow run can resume at its saved phase under either its inherited
profile or an explicitly selected valid replacement.
