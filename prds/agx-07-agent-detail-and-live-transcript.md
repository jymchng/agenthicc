---
title: "AGX-07: Agent detail, live transcript and actions"
status: Implemented
version: 0.1.0
date: 2026-10-23
repository: jymchng/agenthicc
priority: P1
series: AGX
related_prds: [agx-00, agx-04, agx-05, agx-06, agx-10, agx-13, prd-157, prd-215, prd-216, prd-217, prd-219, prd-227, prd-229, prd-231, prd-244]
tags: [agents, tui, ux, transcript, approvals]
---

# AGX-07 — Agent detail, live transcript and actions


## Implementation status (2026-10-24)

Implemented with the agents hub (AGX-05).

- **Peek (§5.1).** Space shows the exact pending request, or the report line
  and current tool, under the row; `i` replies through PRD-215–217's input
  path, and `/stop` as the whole reply stops the agent.
- **Detail (§5.2–§5.3).** A pinned header (status, title, attempt; project,
  workflow, model, start age, tokens and cost) and the Needs you card — the inbox
  item's canonical target through the export redactor — stay on top while
  Now, Transcript (last 50 events, redacted, `f` follow, `t` all), Changes
  (`git diff --stat` from the base commit including uncommitted work, read off
  the event loop at most every five seconds; `d` the full diff), Attempts and
  Details (non-empty fields only) scroll.
- **Actions (§5.4).** Every key is a key-map action in `agents` /
  `agents.detail`, offered only when its condition holds: approve/reject the
  exact item, reply, resume, retry, stop, archive, pin, rename, labels,
  priority, delete, attach, environment actions, transcript/diff/request.
- **Usage (§5.5).** `UsageLedger.subscribe_completed` delivers the session's
  committed totals once per provider call; the worker writes them with its
  next heartbeat (`BackgroundStore.heartbeat(tokens_in=, tokens_out=,
  cost_usd=)`, a delta update) and at finalization. The ledger folds every
  attempt's journal records, so the totals cover all attempts.
- **Live (§5.6).** The transcript follows the journal through AGX-04's
  watcher, bounded tail reads cached by file identity.

Acceptance: (1) `y`/`n` resolve the exact inbox item whose target is shown;
(2) seeded secrets never reach the transcript (`test_detail_shows_a_redacted_…`,
and the export redactor now masks `token=`/`API_KEY:`/`password =`
assignments); (3) actions that do not apply are neither listed nor accepted;
(4) `r`/`R` call the same supervisor operations as `agents resume`/`retry`;
(5) empty fields are not rendered; (6) the peek reply and `/stop` round trip
(`tests/e2e/test_agents_hub_detail_attach.py`). Usage and the report line are
covered by `tests/unit/test_worker_report_agx07.py`.

## 1. Summary

Opening an agent shows a 20-field list — many fields `—`, several in internal
terms — and nothing about what the agent is doing or what it needs. A session
waiting for approval says "Waiting for approval" without saying for what.
Replace the field dump with a detail view built around four questions: **does it
need me, what is it doing, what has it changed, what can I do** — with a live,
redacted transcript tail and contextual actions, including PRD-219's resume and
retry.

## 2. Problem and evidence

The current details page for a session waiting for approval (AGX-00 §4.5):

```text
Background Session · app-1
╭──────────────────────────── Session Details ────────────────────────────╮
│ Title: Fix flaky scheduler test                                         │
│ Session ID: fix-flaky                                                   │
│ State: waiting_approval                                                 │
│ Dir: app-1                                                              │
│ Workflow: code_plan                                                     │
│ Mode: pending: default/persisted                                        │
│ Requested mode: —                                                       │
│ Mode application: pending · attempt 0                                   │
│ Current phase: —                                                        │
│ Phase history: —                                                        │
│ Provider: —                                                             │
│ Model: —                                                                │
│ Source: cli                                                             │
│ Created: 2026-10-23 09:03:25 CEST                                       │
│ Started: —                                                              │
│ Updated: 2026-10-23 08:53:25 CEST                                       │
│ Completed: —                                                            │
│ Attempt / retries: 0 / 0                                                │
│ Latest activity: Approve bash: pytest tests/unit -k schedule            │
│ Pending action: Waiting for approval                                    │
╰─────────────────────────────────────────────────────────────────────────╯
Esc · Enter attach · i input · x actions · ↑↓ PgUp/PgDn [ ] Home/End  ·  1–20/20
```

- What needs approving (tool, target, arguments) is not shown, and there is no
  approve or reject key on this page.
- The transcript is not visible; `_activity_lines` reads the tail of
  `conversation.jsonl` but keeps only the newest text event.
- Worktree-backed agents record `worktree_id`, `branch` and `base_commit`, but
  their changes are not shown.
- Tokens and cost are not recorded for background sessions at all.
- Resume and retry exist only as `jobs resume`/`jobs retry` CLI commands
  (PRD-219's control gap).

## 3. How others handle detail

- **Claude Code agent view**: Space opens a *peek panel* without leaving the
  list — the exact question or latest output, how long a blocked session has
  waited (`waiting 3m`), and a reply input (a reply of `/stop` stops the
  session). Enter or → attaches; attaching to a stopped session shows the tail
  of its transcript, rendered like the live session, while its process
  restarts.
- **Claude Code**: Enter on a `/tasks` row opens the subagent's transcript; you
  can type into the transcript to resume a subagent; a background subagent's
  permission prompt appears in the main session naming the subagent, and Esc
  denies that one call without stopping it.
- **Codex**: the approval overlay shows the source thread's label, and `o`
  opens that thread before you approve, reject or answer; `codex apply` applies
  a cloud task's diff to the working tree.
- **Grok Build**: the Agent Dashboard lists actions you pick with the arrow keys
  and Enter (*third-party cheat sheet*); `/workflow pause|resume|stop` controls
  background runs.

## 4. Goals and non-goals

**Goals**

1. A waiting agent's request is shown in full (redacted) and can be answered in
   place.
2. The transcript tail is visible and follows live output.
3. Worktree agents show what they changed.
4. Every lifecycle action valid for the agent is one key away and listed.
5. Tokens and cost are shown when known.

**Non-goals**

- Editing an agent's history or replaying turns from the detail view.
- Applying worktree changes to another branch (PRD-231 covers review and
  hand-off).

## 5. Design

### 5.1 Peek panel

Space on a list row opens a panel below it (or in the right-hand pane at ≥ 140
columns) without leaving the list:

```text
 ◆ needs approval · waiting 3m
   bash  pytest tests/unit -k schedule -x   in ~/projects/app-1 (Safe)
   y approve   n reject   Enter open   ❯ reply…
```

- It shows the agent's exact pending question or approval, or its latest
  report line and current tool when it is working.
- The reply input sends input through PRD-215–217's path; `/stop` as the whole
  reply stops the agent.
- Space again or Esc closes it; Enter opens the full detail view (§5.2).

Most interactions — answer a question, approve a command, glance at progress —
end in the peek panel without a screen change.

### 5.2 Layout

Full screen below 140 columns; a right-hand pane beside the list at ≥ 140.

```text
 ◆ needs approval   Fix flaky scheduler test                               attempt 2
 app-1 · code_plan · claude-sonnet · started 12m ago · 8m running · 14.2k tokens · $0.18

 Needs you ──────────────────────────────────────────────────────────────────────────
   bash  pytest tests/unit -k schedule -x
   in    ~/projects/app-1  (Safe mode)
   why   "Re-running the scheduler tests to confirm the flake is fixed."
   y approve   n reject   o open in transcript

 Now ────────────────────────────────────────────────────────────────────────────────
   Phase 3 of 4 · verify    (plan ✓ → implement ✓ → verify ● → summarize)

 Transcript (following) ────────────────────────────────────────────────────── f t
   assistant  I changed the retry window in scheduler/clock.py and will re-run…
   ✓ edit     scheduler/clock.py  (+12 −4)
   ✓ bash     pytest tests/unit/test_scheduler.py  · 41 passed
   ◆ bash     pytest tests/unit -k schedule -x  · waiting for approval

 Changes ────────────────────────────────────────────────────────────────────────── d
   2 files changed, +18 −6   (branch agx/fix-flaky, base 1a2b3c4)

 ▸ Details   ▸ Attempts (2)
 Enter attach  r resume  c stop  i input  p pin  e rename  l labels  ? help   Esc back
```

### 5.3 Sections

| Section | Shown when | Content |
|---|---|---|
| **Needs you** | `waiting_approval`, `waiting_input`, `worker lost` | Approval: tool, command or target, working directory and mode, the agent's stated reason when present; keys `y`/`n`/`o`. Input: the question and an inline answer field (PRD-215–217 delivery path). Worker lost: what happened and `r resume` |
| **Now** | Active | Phase progress from `phase_history`/`current_phase`; goal progress for `goal_flow`; the current tool call |
| **Transcript** | Always | The last 50 events from `conversation.jsonl`, redacted with the export redactor: user and assistant text, tool calls as one line each with status and a short result; `f` toggles follow; `t` opens the full transcript (PRD-244's on-demand history) |
| **Changes** | Worktree agents | `git diff --stat` from `base_commit` including uncommitted work, run off the event loop; `d` opens the full diff in a pager |
| **Attempts** | More than one attempt | Timeline of attempts with outcome, failure category and error excerpt |
| **Details** | Collapsed by default | Only non-empty fields, with plain labels; mode application and dispatch state live here |

### 5.4 Actions

Keys come from the keymap (AGX-13); only valid actions are shown:

| Key | Action | Valid when |
|---|---|---|
| Enter | Attach in the foreground (existing canonical path) | Not deleted |
| `y` / `n` | Approve / reject the exact pending request (PRD-227) | Needs approval |
| `i` | Send input (PRD-215–217) | Accepts input |
| `r` | Resume in the background | Resumable (PRD-219) |
| `R` | Retry from scratch | Failed or cancelled |
| `c` | Stop | Active |
| `p`, `e`, `l`, `P` | Pin, rename, labels, priority | Always |
| `A` | Archive | Finished |
| Ctrl+X | Delete (PRD-208 progress) | Always |
| `x` | Worktree environment actions (PRD-229) | Worker with a trusted manifest |
| `o`, `d`, `t` | Open transcript at the request, diff, full transcript | As above |

### 5.5 Usage and cost

Workers add `tokens_in`, `tokens_out` and `cost_usd` to the session record after
each provider step (as delta updates, AGX-04), using PRD-157's canonical usage
accounting. The list (AGX-06) and detail view read them from the index.

### 5.6 Live behaviour

The transcript and "Now" sections follow the journal through AGX-04's change
notification (or the session service stream when running). Reads are bounded to
the last 256 KiB of the journal, parsed off the event loop, and cached by
`(inode, size)` as `_activity_lines` does today.

## 6. Budgets and acceptance criteria

| Measure | Budget |
|---|---:|
| Open detail from the list | p95 ≤ 50 ms |
| New journal event → visible in a following transcript | p95 ≤ 250 ms |
| Diff summary for a worktree agent | ≤ 300 ms, never on the event loop |

1. For a session waiting for approval, the tool, target and arguments shown
   match the inbox item exactly, and `y`/`n` resolve that exact item.
2. Secrets present in the journal never appear in the transcript section (the
   existing redaction tests run against the detail renderer).
3. Only actions valid for the agent's state are listed and accepted.
4. `r` and `R` produce the same durable records as `agents resume`/`agents retry`.
5. Fields that are empty are not rendered; the Details section shows the rest.
6. Space on a list row opens the peek panel within 50 ms; a reply sent from it
   is delivered exactly as the detail view's `i` action delivers input, and
   `/stop` as the whole reply stops the agent.

## 7. Test plan

- Golden renders for each status and for 80/120/160 columns.
- Approval round trip: render, approve, verify inbox state and worker resumption.
- Follow mode with a writer appending events; redaction of seeded secrets.
- Worktree fixture with committed and uncommitted changes for the diff summary.

## 8. Risks

- **Large journals.** Bounded tail reads and on-demand full history keep cost
  independent of journal size.
- **Leaking sensitive tool arguments.** The approval card renders the inbox
  item's canonical request (the same target and operation the approval is bound
  to) through the export redactor, never raw provider arguments.

## Sources

- Claude Code agent view (peek panel, attach): <https://code.claude.com/docs/en/agent-view>
- Claude Code subagents (`/tasks`, transcripts, approvals): <https://code.claude.com/docs/en/sub-agents>
- Codex subagents (approval overlay `o`): <https://learn.chatgpt.com/docs/agent-configuration/subagents>
- Codex CLI reference (`codex apply`): <https://developers.openai.com/codex/cli/reference>
- Grok Build modes and commands (`/workflow`): <https://docs.x.ai/build/modes-and-commands>
- Grok Build cheat sheet (*third-party*): <https://www.scriptbyai.com/grok-build-cheat-sheet/>
