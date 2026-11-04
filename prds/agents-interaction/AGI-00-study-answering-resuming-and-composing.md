---
title: "AGI-00: Study — answering an agent, resuming it, and typing to it"
status: Study
version: 1.0.0
date: 2026-11-04
scope: "three reported failures: the agents hub cannot answer an ask_user question; R/r (retry/resume) end in an HTTP provider error; the ask_user overlay's input lacks the composer's / commands, @ mentions and paste"
related_prds: [AGI-01, AGI-02, AGI-03, AGI-04, AGI-05, PRD-100, PRD-192, PRD-219, PRD-227, AGX-05, AGX-07]
tags: [agents-hub, background-sessions, ask_user, resume, composer]
---

# AGI-00: Study — answering, resuming and typing to an agent

## 1. Reported problems

1. In `agenthicc agents`, there is no way to answer an agent in a session
   when it uses the `ask_user` tool.
2. Retry (`R`) and Resume (`r`) cause an HTTP provider error, and the session
   can be neither retried nor resumed.
3. In the `ask_user` overlay, the input panel does not work like the main
   TUI's input panel: `/` commands, `@` mentions and so on do not work.

## 2. Method

- Read each path end to end:
  - the `ask_user` tool;
  - `BackgroundApprovalService`;
  - the interaction inbox;
  - the agents hub;
  - `BackgroundSupervisor.resume`/`retry`;
  - `QuestionsOverlay`, `PromptOverlay` and the composer's capability
    pipeline.
- Read the operator's own background store (`/root/.agenthicc/background`,
  read-only):
  - session records;
  - attempt histories;
  - inbox items;
  - stored worker requests.
- Reproduced each failure in-process against the code on `main` (`d0f7074`).
  The scripts are listed in §6.

## 3. Findings

### 3.1 The hub cannot answer a multi-question `ask_user`

**What the operator has.**
- The store holds 39 `question` inbox items from 12 sessions.
- 25 of them ask **two or more questions**, each with long options. The
  breakdown is 21 with two, 2 with three, 1 with four and 1 with eight.
  Examples:
  - `hero` + `assets` for the `ccresu` goal-flow session;
  - `react_pivot` + `risk` for a YouTube-shorts run.
- The other 14 ask one question.
- 33 of the 39 are `expired`; 1 is `stale`.

**What the hub does with one** (reproduced with the operator's two-question
shape, §6 `hubq.py`):

```text
▸ ◆ needs input   ccresu   Questions: What should the…   ccresu   goal_flow   now
  Answer Derive it from the README   Enter send · Ctrl+J newline · Esc cancel
  multiple questions require a JSON object keyed by question ID
```

- `i` opens a one-line `Answer` field. It shows neither the questions nor
  their options.
- `response_from_text` (`background/inbox.py:109`) accepts plain text only
  when there is exactly one question. For two or more it requires a JSON
  object keyed by question IDs, and the hub never shows those IDs.
- The detail view (`Enter`) lists the questions and options, but not the IDs.
  It also offers no way to pick an option.
- To answer, an operator must type something like
  `{"hero": "Derive it from the README", "assets": "Use the existing docs images"}`
  from memory. In practice there is no way to answer.

**Attaching is not a way to answer.**
- `Enter attach` calls `BackgroundSupervisor.attach_foreground`
  (`supervisor.py:1294`).
- It moves the session through `cancelling` and kills the worker that is
  waiting on the question. The question dies with it.

**The deadline expires the question before anyone sees it.**
- Background questions use `[tools] question_timeout_s`, which defaults to
  300 s (`config.py:83`). For example, item `inbox_1409…` was created at
  `…043.6` and expired at `…343.6`.
- The agent then continues on its own assumption (PRD-192's `timed_out`
  result). When the operator opens the hub, there is nothing left to answer.
- A five-minute deadline suits a foreground TUI, where the overlay is on
  screen. It does not suit a background agent, which by definition nobody is
  watching.

**Stale items linger.**
- All 7 items still marked `pending` (5 questions, 2 approvals) belong to an
  older attempt of a session that has since failed, been deleted or
  completed. For example, `c14af6ba`'s item is for attempt 2, but the session
  failed at attempt 4.
- Every one of them is stale, yet each still counts as "needs you".
- `BackgroundInteractionInbox.reconcile()` marks such items `stale`, but it
  runs only from `agenthicc agents inbox` and `/inbox`, never when the hub
  opens.

**A background direct turn cannot ask at all.**
- The foreground TUI gives every turn the session tools: `ask_user`, the
  memory tools and the browser tools (`tui_session._make_session_tools`,
  `tui_session.py:4560`).
- The background worker's direct turn (`worker._run_direct_turn`,
  `worker.py:1097`) passes only `project_plugins.all_tools`. A background
  agent without a workflow therefore has no `ask_user`.
- `BackgroundApprovalService.request_input` exists to serve exactly that call
  (`worker.py:896`).

### 3.2 Resume and retry drop the session's launch configuration

**What the operator has.**
- Attempt histories show the failures after `R`/`r`:
  - `07c4a784` attempts 5, 7 and 8 failed with `HTTP 401 … Invalid
    'Authorization' header or token; provider=openai;
    model=deepseekdeepseek-v4.1-flash`;
  - `b91a3646` attempt 13 failed the same way.
- Both sessions were launched with
  `--set-secret execution.default_headers.Authorization=CMD_API_KEY`, and
  `b91a3646` also with `--set execution.provider=openai`. These values are
  recorded in `requests/<id>.json`.

**Root cause** (`supervisor.py:1274-1292`, reproduced in §6
`resume_repro.py`).
- `resume()` calls `handoff(session_id, intent, workflow_name, cwd,
  mode_name)` and nothing else.
- `handoff()` builds a new `BackgroundRequest` from its own defaults:
  - `config_path=None`;
  - `set_overrides=()`;
  - `set_secret_overrides=()`;
  - `dangerously_skip_permissions=False`.
- It then **overwrites** the stored request with that request and launches
  the worker from it.
- The resumed worker therefore runs with the base configuration: no custom
  provider, no base URL, no Authorization header. The provider answers 401.
- Reproduced output:

  ```text
  config_path: None | set_overrides: () | set_secret_overrides: ()
  ```

  The original request had all three set.

**Retry is resume.**
- `retry()` is `return self.resume(session_id, mode_name=mode_name)`.
- The hub describes `R` as "Retry the agent from scratch" (`tui/actions.py:281`)
  and `retry_async` says "Retry from scratch" (`agents_service.py:216`). Both
  keys run the same code.

**Secret references are environment lookups.**
- `--set-secret KEY=ENV_VAR` stores `{"env": "ENV_VAR"}` (`config.py:2281`),
  and the value is read when the worker starts.
- Preserving the reference is therefore not enough on its own: the process
  that relaunches the worker must have the variable.
- A missing variable already raises `Missing secret environment variable
  'CMD_API_KEY'` (`config.py:653`). That is the right error, but today it can
  never surface, because the reference is dropped first.

**A second, older resume failure.**
- `e4222609` attempts 24 and 25 failed with lauren-ai's "provider rejected
  the assistant history because its required reasoning_content was not
  replayed".
- The journal stores the whole message dict and folds it back unchanged
  (checked: an assistant message with `reasoning_content` survives append,
  reopen and fold). The failure predates the current lauren-ai, so it is
  probably already fixed upstream.
- AGI-04 pins it with a regression test rather than a separate PRD.

### 3.3 The `ask_user` input is a 70-line buffer, not the composer

**Root cause** (`tui/workspace/overlays/prompt.py`).
- `QuestionsOverlay`'s "Other — type your answer" mode uses
  `PromptOverlay._handle_prompt_key`. It handles only `Key.CHAR`, Backspace,
  Left/Right and Home/End.
- Everything else is dropped:
  - `Key.AT`, the reader's encoding of `@`;
  - `Key.PASTE`, bracketed paste;
  - newline;
  - Delete, word movement and history.
- Reproduced (§6 `qo.py`): typing `see @src` and pasting `/main.py and more`
  leaves the buffer as `'see src/'`. The `@` and the entire paste are lost
  silently, and `/` is a plain character.

**What the composer has instead.**
- The main composer is `UnifiedInputSession` with the capability pipeline in
  `tui/input/capabilities.py`:
  - triggers (`/` command and skill picker, `@` mention picker, `#`, `!`)
    through `TriggerManager`;
  - condensed paste;
  - `Ctrl+J` newline;
  - history;
  - cursor and word movement.
- On submit, `@` mentions are resolved into a context prefix by
  `mentions.injector.build_context_prefix` (`agent_turn.py:1391`).

**Why the overlay cannot simply reuse it.**
- `OverlayHost` holds one overlay (`tui/workspace/overlay.py:46`). Opening
  the trigger picker over the questions overlay would call
  `QuestionsOverlay.on_unmount()`, and coming back would call `on_mount()`,
  which clears the typed answer.
- The pickers are overlays, and overlays do not nest.

**There are three editors.**
- The composer (`UnifiedInputSession`), `PromptOverlay` (`InputBuffer`) and
  the hub's `LineEditor` are separate implementations.
- The hub's Answer field (`LineEditor`) handles paste and `Ctrl+J`, but has
  no `@` or `/`.

### 3.4 Incidental: a 19.6 GB background event log

**The size.**
- `/root/.agenthicc/background/events.jsonl` is 19,585,053,236 bytes:
  1,795,908 `updated` events averaging 10.9 KB.
- Older `updated` events carry the whole 72-field record on every heartbeat,
  including a 16 KB `intent` and a 5 KB `attempt_history`.
- Recent events are small diffs (184 bytes, mostly `last_active`), so the
  writer was fixed. Nothing ever compacts the old log, though.

**The effect.**
- Any reader that rebuilds the index replays all 19.6 GB. In this study a
  copied store could not settle the hub within its timeout.

**Out of scope.** It does not cause the three reported problems. It is
recorded for a separate store-compaction PRD.

## 4. Routing

| Problem | Root cause | PRD |
|---|---|---|
| 1 | The answer field needs hand-typed JSON for 2+ questions; no options or IDs are shown | [AGI-01](AGI-01-answer-questions-in-the-agents-hub.md) |
| 1 | Questions expire after 300 s while unattended; no late answer; stale items linger | [AGI-02](AGI-02-background-questions-wait-for-the-operator.md) |
| 1 | Background direct turns are not given `ask_user` | [AGI-03](AGI-03-background-turns-get-the-session-tools.md) |
| 2 | Resume and retry rebuild the worker request without its launch configuration | [AGI-04](AGI-04-resume-and-retry-keep-the-launch-configuration.md) |
| 3 | The overlay uses a bare buffer; overlays do not nest, so pickers cannot open | [AGI-05](AGI-05-one-composer-for-every-answer.md) |

Order:
1. AGI-04, which is small and unblocks every failed session.
2. AGI-05 and AGI-01 together. AGI-01's form uses AGI-05's composer for its
   free-text answers.
3. AGI-02, then AGI-03. AGI-03 makes background direct agents ask, which is
   only useful once AGI-01 and AGI-02 let the operator answer.

## 5. Out of scope

- Compacting the background event log (§3.4).
- Foreground sessions seen from the hub. They appear only as saved History;
  the live session answers in its own TUI.

## 6. Reproductions

Each script ran against `main` (`d0f7074`) and is described here so it can be
re-created as a test.

- **`hubq.py`.** A `BackgroundStore` in a temporary directory, with one
  `waiting_input` goal-flow session and a two-question inbox item shaped like
  the operator's. It opens `AgentsHub` on Needs you and sends `i`, a paste of
  `Derive it from the README`, then Enter. The answer is rejected with
  "multiple questions require a JSON object keyed by question ID", and
  `inbox.accepted_answer()` is `None`.
- **`resume_repro.py`.** A failed session whose stored request has
  `config_path`, `set_overrides=["execution.provider=openai"]` and
  `set_secret_overrides=["execution.default_headers.Authorization=CMD_API_KEY"]`.
  `BackgroundSupervisor.resume()` runs with `_launch` captured, and the
  launched request has all three empty.
- **`qo.py`.** `QuestionsOverlay` for one question. Down and Enter select
  "Other". It then types `see `, `Key.AT`, `src`, a `Key.PASTE` of
  `/main.py and more`, and `/`. The buffer is `'see src/'`.
