---
title: "TUX-07: One dialog component for approvals, plans and questions"
status: Proposed
version: 1.0.0
date: 2026-11-05
scope: "a shared Dialog frame for every decision overlay (tool approval, workspace access, plan review, ask_user questions, confirmations, pagers): layout, width, wording, keys, and approval content that explains the action (command, diff, target, risk)"
related_prds: [TUX-00, TUX-01, TUX-04, TUX-05, AGI-01, AGI-05, PRD-78, PRD-100, AGX-08, AGX-13]
tags: [tui, overlays, approvals, dialogs, ergonomics]
---

# TUX-07: One dialog component for approvals, plans and questions

## 1. Problem

From TUX-00: F3. See `screens/ov-approval.png`, `ov-plan.png` and `ov-questions.png`.

The three decision overlays each draw their own frame.

### 1.1 Tool approval

`tui/workspace/overlays/approval.py:80–175`:

- **The header** is `⚠  Tool Approval Required  [execute]` in bold yellow, over a dim rule clipped at 66 columns.
- **The tool** is named by its **internal name** (`run_bash`), followed by up to three `key: value` pairs cut at 80 characters.
- **A file write** shows the first 80 characters of `content`, not a diff.
- **A command** is cut at 80 characters, so its tail, often the dangerous part (`&& rm -rf …`), is hidden.
- **Nothing says why approval is needed**: which mode rule or capability asked.
- **The options** are `[y] Allow once`, `[a] Allow all execute this turn`, `[A] Allow all execute this session` and `[n] Deny`, selected in reverse video with `▶`.

### 1.2 Plan review

`overlays/plan_approval.py`:

- a cyan header with an emoji that renders as tofu (F4);
- a fixed 66-column frame;
- a large empty viewport for a short plan;
- options "Approve - Safe" (hyphen) beside "Reject — add feedback" (em dash).

### 1.3 Questions

`overlays/questions.py`:

- a third header style and emoji;
- a "240s remaining" countdown, which AGI-02 made obsolete for background turns;
- its own option rendering.

### 1.4 Shared problems

- Every frame is `min(cols, 66)` (`approval.py:100`, `plan_approval.py:191`, `questions.py:358`), so on a 200-column terminal a diff or plan is squeezed into a third of the screen.
- Keys differ between dialogs: Esc denies in approval, steps back in questions, and rejects in plan review.

**What the others do.** Claude Code's approval names the action in words, shows the command in full and the diff for edits, and offers "Yes / Yes, and don't ask again for <scope> / No, and tell the agent what to do differently". OpenCode and Qoder show a diff before approving an edit.

## 2. Goals

### 2.1 `Dialog`: one frame

`tui/workspace/dialog.py: Dialog` is a renderable plus key handling, composed by every decision overlay.

```text
 ╭─ Run a command ──────────────────────────────────────────────── Safe ─╮
 │                                                                       │
 │  $ cd objective-create-a && npm ci && npm test                        │
 │                                                                       │
 │  Safe mode asks before running commands. in ~/youtube-shorts/         │
 │                                                                       │
 │ ▌ 1  Yes                                                              │
 │   2  Yes, and allow commands for the rest of this turn                │
 │   3  Yes, and allow `npm` commands this session                       │
 │   4  No, and tell the agent what to do instead                        │
 │                                                                       │
 │  ↑↓ choose · enter confirm · esc no · ctrl+t full details             │
 ╰───────────────────────────────────────────────────────────────────────╯
```

**Parts.**

- **Title.** A verb phrase on the top border ("Run a command", "Edit 2 files", "Approve the plan", "The agent asks"). It never contains a tool's internal name.
- **Badge.** The mode badge on the right of the top border.
  - When a subagent or background worker asks, the badge is the requester instead: `reviewer #2 · o open transcript` (AGX-08).
- **Body.** It scrolls when it is taller than the available height, with `↑ N more` and `↓ N more` markers. Its content depends on the kind of request (2.2).
- **Reason.** One muted line saying why the dialog is shown: the mode rule, the capability, or the workspace boundary that triggered it.
- **Options.**
  - They are numbered 1–9. A number key selects and confirms.
  - The selected row has the `accent` bar and `accent_bg` (TUX-01), not reverse video.
  - A destructive choice ("No", "Reject") is drawn in `danger` only when selected.
- **Key legend.** One muted row at the bottom.

**Size.**

- The width is `clamp(cols − 4, 60, 120)`, centred when `cols > 124`.
- The height is up to `rows − 6`. Plans and diffs use it, and short bodies shrink the dialog to fit, with no empty viewport.

**Keys, the same in every dialog.**

- `↑↓` or `j/k` choose; Enter confirms; `1–9` choose and confirm.
- Esc takes the **safe exit**: deny for approval; "not now, keep reviewing" for plan review (the plan stays pending); back, then cancel, for questions (as AGI-05 already does).
- `Ctrl+T` shows the full arguments or diff in TUX-09's transcript view.
- `Tab` adds a note to the choice (2.3).
- The old letter hotkeys (`y/a/A/n`) remain as aliases in the keymap, since users have learned them, and `/shortcuts` lists them.

**Screen readers.** The title, body summary and options are announced as sentences when the dialog opens (AGX-13). There is no box drawing.

### 2.2 Approval content explains the action

| Request kind | Body |
|---|---|
| execute (`run_bash`, `run_command`, `run_tests`, `run_python`) | The **whole** command, syntax-highlighted, wrapped (never cut), and the working directory. A `cd X && …` prefix is shown as "in X". |
| write (`write_file`, `patch_file`, `apply_diff`, `batch_write`) | A unified diff against the current file, with TUX-01 diff tokens, plus totals `+12 −3` per file. A new file shows its first 40 lines, marked "new file". |
| delete or move | The list of paths with their sizes, and "N files, M directories". |
| network | The method, host and path, and which allow-list rule it fails. |
| workspace access (scope approvals) | The target paths, the workspace root, and the operation (unchanged from today, restyled). |
| MCP or plugin tool | `server · tool`, its description's first line, and the arguments as a key/value table with values wrapped, not cut. |

The rules that limit what is shown are unchanged:

- Secrets stay redacted by the same projection the transcript uses.
- Large content is bounded by the existing approval preview cap, with "+N more lines · ctrl+t".
- The workspace-scope rule from the current code still holds: raw arguments are never shown for scope approvals.

**Wording.**

- "Allow all execute this turn" becomes "Yes, and allow commands for the rest of this turn".
- The session scope names the narrowest useful grant the policy supports. For example, "allow `npm` commands this session" applies when the policy can scope by program; otherwise "allow commands this session".
- "No, and tell the agent what to do instead" opens an `EmbeddedComposer` (AGI-05) inside the dialog. The text is returned to the model with the denial.

### 2.3 Plan review and questions use the same frame

- **Plan review.**
  - The body is the rendered Markdown plan, using the full height.
  - The options are "Approve and run in Safe mode", "Approve and run in Yolo mode", "Keep planning" and "Reject, with feedback". There are no emoji.
  - Feedback uses the embedded composer.
- **Questions.**
  - The title is "The agent asks" plus the agent's name.
  - The question and its options are in the body, and "Other" opens the embedded composer.
  - With several questions, the body shows `1 of 3` and `[`/`]` move between them (unchanged).
  - The countdown is removed for foreground turns too. AGI-02's no-timeout default applies everywhere, and a configured timeout shows as a muted "answer within 4 min" line only when one is set.
- **Other dialogs.** Confirmation dialogs (agent delete, schedule edit, folder trust in TUX-03) and pagers (`/release-notes`, `/help`) also use `Dialog`.

## 3. Non-goals

- Changing the approval policy, the scopes the policy supports, or what is redacted.
- Nesting overlays. `Dialog` stays a single `OverlayHost` overlay, and the embedded composer's picker renders inside it (AGI-05).

## 4. Acceptance criteria

1. **No more fixed widths.** `rg "min\(cols, 66\)" src/` is empty, and every decision overlay renders through `Dialog` (asserted by type in the overlay tests).
2. **Commands are shown whole.** An approval for a 300-character command shows all of it, wrapped, at 80 and 120 columns, including the last 20 characters.
3. **Diffs.** An approval for `patch_file` shows the unified diff and `+N −M`. The test uses a real temporary file.
4. **The reason.** Every approval names its reason. In Safe mode, an execute approval reads "Safe mode asks before running commands."
5. **Number keys.** `2` on an approval responds with `remember=True` (allow for the turn) and closes the dialog. Esc denies. Both use the existing `InteractiveApprovals` fake.
6. **"No, and tell the agent".** Typing "use pnpm instead" returns a denial whose tool result contains that text. This is an integration test through the executor and approval hook.
7. **Plan review.**
   - A three-line plan renders a dialog no taller than its content plus chrome (≤ 12 rows).
   - A 200-line plan uses `rows − 6` and scrolls.
8. **No emoji.** No dialog renders a character outside TUX-01's glyph set (checked against the glyph cmap test).
9. **Screen reader.** With `screen_reader = true`, opening an approval announces "Run a command: cd objective-create-a && npm ci && npm test. Safe mode asks before running commands. 4 options." and renders without box characters.

## 5. Rollout

- `ApprovalOverlay`, `PlanApprovalOverlay` and `QuestionsOverlay` keep their names and service contracts, and their render code is replaced by `Dialog` composition.
- `PromptOverlay`, which remains only as `PlanApprovalOverlay`'s base (AGI-05 amendment), is deleted: feedback uses the embedded composer.
- The approval option table's labels change. Tests that match the old labels are updated, and the `y/a/A/n` keys stay bound.

## 6. Verification

- `uv run pytest tests/unit/test_approval_overlay*.py tests/unit/test_plan_approval*.py tests/unit/test_questions_overlay*.py tests/unit/test_dialog*.py tests/integration/test_approval*.py -q`
- Screenshots of an approval with a command, an approval with a diff, a short plan and a question, at 80 and 160 columns.
