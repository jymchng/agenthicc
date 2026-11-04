---
title: "AGI-04: Resume and retry relaunch with the session's own launch configuration"
status: Implemented with amendments (2026-11-04; AGI wave 1, pending merge)
version: 1.0.0
date: 2026-11-04
scope: "what BackgroundSupervisor.resume/retry launch: the durable launch specification (config path, --set overrides, --set-secret references, profile, permissions), a preflight that refuses a launch that cannot authenticate, distinct retry semantics, and provider diagnostics that name the model correctly"
related_prds: [AGI-00, PRD-205, PRD-211, PRD-219, AGX-05]
tags: [background-sessions, resume, retry, configuration, secrets]
---

# AGI-04: Resume and retry keep the launch configuration

## Implementation status (2026-11-04)

Delivered in AGI wave 1 (branch `agi-wave-1-resume-launch-spec`).

- **The launch record.** `LaunchSpec` (in `agenthicc.background`) holds:
  - `config_path`;
  - `set_overrides`;
  - `set_secret_overrides`, as references only;
  - `dangerously_skip_permissions`.

  `submit(..., launch=…)` and `handoff(..., launch=…)` replace the four
  scattered parameters at every caller: the CLI, the TUI `/background`
  handoff, the hub, goal runs, worktree workers and schedules. They record it
  once, in `launch/<id>.json` (0600). `handoff(launch=None)` reuses the
  record.
- **Resume reuses it.** `resume()` launches with the recorded configuration.
  `retry()` starts a new session from the failed one's request, with the same
  launch configuration, mode and profile, linked by
  `retried_from`/`retried_as`. `R` is offered only for failed sessions.
- **Preflight.** `preflight_launch()` runs before any state change. It raises
  `LaunchPreflightError`, naming the variable and its `--set-secret` flag, for
  an unset secret variable, a missing config file or an override that does
  not parse. The CLI exits with status 2, and the hub shows the message.
- **The CLI can replace it.** `agents resume|retry ID` uses the global
  `--config`/`--set`/`--set-secret`/`--dangerously-skip-permissions`, when
  given, as the new record.
- **Model names.** Provider diagnostics keep `/` and `@`, so the report now
  says `model=deepseek/deepseek-v4.1-flash`.

Amendments:

- **Profile and mode stay where they are.** They are already persisted
  per session (`requested_profile_name`, mode application), so `LaunchSpec`
  does not duplicate them.
- **A launch file, not a record field.** The record is a file beside the
  request (`launch/<id>.json`), not a session-record field with
  `launch_spec_version`. This keeps the 72-field heartbeat record, which
  AGI-00 §3.4 found had grown the event log to 19.6 GB, from carrying it.
- **An unknown configuration resumes with defaults.** A session with no
  launch record and no request file resumes without flags. An old request
  that an earlier resume overwrote with defaults cannot be told apart from a
  launch without flags, so refusing to resume would only break resumes that
  work today. The documented remedy is to resume with the flags.
- **Preflight checks syntax, not the whole configuration.** The project's
  configuration is discovered from the worker's own directory, which the
  supervisor process does not run in. Preflight therefore validates override
  syntax and the config file's existence, not a full `load_config`.
- **The end-to-end test checks configuration, not a transport.** Criterion
  3 is tested through the worker's own configuration path: the resumed
  request is parsed by `WorkerRequest`, `load_config` runs, and the
  Authorization header resolves to the variable's value. It does not use a
  recording transport.

Tests: `tests/unit/test_launch_spec.py` (10). They were mutation-checked: a
resume that launches with defaults fails four of them.

## 1. Problem

AGI-00 §3.2.

**Resume launches with the base configuration.**
- `BackgroundSupervisor.resume()` calls `handoff(session_id, intent,
  workflow_name, cwd, mode_name)` (`supervisor.py:1274`).
- `handoff()` builds a new `BackgroundRequest` whose launch fields come from
  its own parameter defaults:
  - `config_path=None`;
  - `set_overrides=()`;
  - `set_secret_overrides=()`;
  - `dangerously_skip_permissions=False`.
- It overwrites `requests/<id>.json` with that request and launches it.

**The operator's sessions show the result.**
- They were started with
  `--set-secret execution.default_headers.Authorization=CMD_API_KEY`, and
  some also with `--set execution.provider=openai`.
- On `R`/`r` they relaunch with no Authorization header. The provider
  answers `HTTP 401 Invalid 'Authorization' header or token`: `07c4a784`
  attempts 5, 7 and 8, and `b91a3646` attempt 13.
- Reproduced in AGI-00 §6 (`resume_repro.py`).

**Retry and resume are the same operation.**
- `retry()` is `return self.resume(...)`.
- The hub nevertheless describes `R` as "Retry the agent from scratch", and
  `retry_async` says "Retry from scratch".

**The diagnostic misnames the model.**
- `_safe_provider_identifier` keeps only `[A-Za-z0-9_.:-]`
  (`agent_turn.py:206`), so `deepseek/deepseek-v4.1-flash` is reported as
  `deepseekdeepseek-v4.1-flash`. The operator is pointed at a model that does
  not exist.

**Failures surface late.**
- Nothing checks a relaunch before it runs. The failure appears only as a
  new failed attempt minutes later, and `retry_count` climbs: 10 retries on
  `b91a3646`, 10 on `e4222609`.

## 2. Goals

1. **A durable launch specification.**
   - New `LaunchSpec` (frozen) in `background/model.py` with these fields:
     - `config_path`;
     - `set_overrides`;
     - `set_secret_overrides`, as `KEY=ENV_VAR` references only, never
       values;
     - `dangerously_skip_permissions`;
     - `requested_profile_name`;
     - `mode_name`.
   - It is written once, when the session is created (`run --background`,
     `/background`, hub dispatch), into the session record. A
     `launch_spec_version` field makes it migratable.
   - `handoff()` takes `launch: LaunchSpec | None`. `None` means "the
     session's recorded spec". Explicit fields replace recorded ones only
     when the caller passes them, as the foreground `/background` handoff
     does today.
   - The scattered `config_path`/`set_overrides`/… parameters are deleted.
2. **Resume never overwrites with defaults.** `resume()` and `retry()` launch
   from the recorded spec. `requests/<id>.json` is derived from it, never the
   other way round.
3. **Preflight before launch.**
   - Before any worker is spawned, `resume`/`retry` build the effective
     configuration in the launching process: `load_config` with the spec's
     path, overrides and secret references, resolved against the process
     environment.
   - A missing secret variable raises `LaunchPreflightError` and changes
     nothing: no attempt, no `retry_count`, no status change. The message
     names the variable and the flag that needs it:

     ```text
     Resume needs CMD_API_KEY in this environment
     (from --set-secret execution.default_headers.Authorization=CMD_API_KEY).
     Export it and press r again.
     ```

   - An invalid override or a missing config file fails the same way.
   - The hub shows the message as the operation's notice. The CLI prints it
     and exits 2.
4. **Retry and resume mean different things.**
   - **Resume (`r`)** continues the same session from its durable state: the
     journal turn recovery and the workflow checkpoint.
   - **Retry (`R`)** starts a **new** background session with the same
     `LaunchSpec` and the original intent, linked by `retried_from` (and the
     failed session's `retried_as`). The failed session stays in History as
     it is. "From scratch" then means exactly what it says, without
     discarding anything.
   - `R` is offered only for `failed` sessions, and `r` for failed,
     cancelled and orphaned ones (unchanged).
5. **The CLI can supply a spec.** `agenthicc agents resume|retry ID`
   honours the global `--config`, `--set` and `--set-secret` flags as
   explicit replacements of the recorded spec, and records the result. Today
   `_mutate` passes only the session ID.
6. **Diagnostics name the real model.** `_safe_provider_identifier` keeps
   `/` and `@`, which provider-qualified model IDs use, and still drops
   whitespace, quotes and control characters.
7. **Resume replays provider reasoning.** A resumed conversation sends back
   each assistant message's `reasoning_content` exactly as journaled. The
   journal already keeps it (AGI-00 §3.2); this pins it against the
   `e4222609` failure.

## 3. Non-goals

- Storing secret values. Only the `{"env": NAME}` references are persisted,
  as today.
- Choosing a different model or profile on resume. That is an explicit
  `agents resume --profile`, which is out of scope.

## 4. Acceptance criteria

1. **Overrides survive resume.** A failed session created with
   `config_path`, `set_overrides=("execution.provider=openai",)` and
   `set_secret_overrides=("execution.default_headers.Authorization=CMD_API_KEY",)`,
   when resumed and when retried, launches a request carrying all three.
   This is the AGI-00 reproduction turned into a test.
2. **Missing secret.** With `CMD_API_KEY` unset, `r` and `R` in the hub show
   the preflight message and launch nothing. The session's `attempt`,
   `retry_count` and status are unchanged.
3. **End to end.** With `CMD_API_KEY` set, resuming launches a worker whose
   provider request carries `Authorization: <value of CMD_API_KEY>`. This is
   an integration test with a recording transport.
4. **Retry makes a new session.** `R` on a failed session creates a new
   session with the same intent and launch spec, `retried_from` pointing at
   the original. The original's journal, attempts and status are unchanged.
5. **Model label.** A 401 for `deepseek/deepseek-v4.1-flash` is reported
   with `model=deepseek/deepseek-v4.1-flash`.
6. **Reasoning replay.** Resuming a journal whose assistant message has
   `reasoning_content` sends it back in the first resumed provider request.
7. **No dropped fields.** No call to `handoff()` passes launch fields
   individually; `rg "set_secret_overrides=" src/agenthicc/background`
   shows only `LaunchSpec`.

## 5. Rollout and security

- **Migration.** Sessions created before this change have no recorded
  `LaunchSpec`. On first resume, the spec is built once from
  `requests/<id>.json`, the last request written by a non-resume path, and
  then recorded.
- **Unrecoverable sessions.** A session whose request file was already
  overwritten by an earlier resume cannot recover its overrides. The hub
  says so instead of launching with defaults: "launch configuration
  unknown; resume with the flags you launched it with:
  `agenthicc --set … --set-secret … agents resume ID`".
- **Secrets.** Only environment-variable names are stored, never values,
  and preflight resolves them in memory only.
  `agenthicc agents status ID --json` prints the references, never resolved
  values.
- **New session on retry.** Retry creates a new session, so it obeys the
  queue's capacity and owner limits like any submission.

## 6. Verification

- `uv run pytest tests/integration/test_background_sessions.py
  tests/unit/test_background_sessions.py -q`
- New: `tests/unit/test_launch_spec.py` (criteria 1, 2, 7),
  `tests/integration/test_resume_authenticates.py` (criterion 3, recording
  transport), and `tests/unit/test_provider_identifier.py` (criterion 5).
