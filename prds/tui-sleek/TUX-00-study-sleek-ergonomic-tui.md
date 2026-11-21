---
title: "TUX-00: Study — making agenthicc's TUI sleek and ergonomic"
status: Study
version: 1.0.0
date: 2026-11-05
scope: "the whole interactive screen of agenthicc (welcome, status, composer, pickers, transcript, overlays, errors, hub), compared against Claude Code, OpenAI Codex, OpenCode, Qoder and DeepSeek Harness"
related_prds: [TUX-01, TUX-02, TUX-03, TUX-04, TUX-05, TUX-06, TUX-07, TUX-08, TUX-09, TUX-10, BEAU-1, BEAU-2, BEAU-3, BEAU-4, BEAU-5, AGX-11, AGX-13, PRD-256]
tags: [tui, ux, design-system, ergonomics, research]
---

# TUX-00: Study — making agenthicc's TUI sleek and ergonomic

## 1. Method

- **agenthicc** (`main` at `4784f74`) ran in tmux at 120×40, with real input.
  - A real session (`c14af6ba`, 827 events) was replayed with `/replay` to see a
    working transcript.
  - The overlays were rendered from their classes with representative
    requests.
- **Comparisons:** Claude Code 2.1.318, OpenAI Codex 0.166.0 and OpenCode
  1.15.11 ran in the same 120×40 pane, from an empty scratch repository. No
  prompts were sent. Qoder and DeepSeek Harness were studied from their
  documentation, cited in §4.
- **Screenshots:** `screens/shot.py` turns a tmux pane, or a saved ANSI file,
  into a PNG with real colours (`python shot.py SESSION OUT.png [-S n -E m]`).
  Every image cited below is in `screens/`.
- **Scope:** BEAU-1 to 5 (Proposed) set the art direction for the scrollback
  transcript only. This series covers the whole screen and folds the BEAU
  transcript work into TUX-06 (§6).

## 2. What agenthicc looks like today

| Screen | File |
|---|---|
| Idle, after start | `screens/agt-idle.png` |
| `/` picker | `screens/agt-slash.png` |
| `@` picker | `screens/agt-at.png` |
| A failed turn (no API key) | `screens/agt-error.png` |
| A real transcript (replay) | `screens/agt-tools.png` |
| `/model` | `screens/agt-model.png` |
| Agents hub over the session | `screens/agt-hub.png` |
| Tool approval, plan review, questions | `screens/ov-approval.png`, `ov-plan.png`, `ov-questions.png` |

## 3. Findings

Each finding is numbered for the PRDs to cite (F1…), and is backed by a
screenshot or a source reference.

### 3.1 Visual system

- **F1. No colour system.**
  - The TUI has 244 hard-coded colour names (`red`, `cyan`, `bold yellow`,
    `grey11`, …) across `src/agenthicc/tui`. The most are in `appender.py`
    (36), `agents_hub/render.py` (21) and `components.py` (16).
  - Code blocks use Pygments' `monokai` (`diff_renderer.py:81`).
  - There is no theme, no light-terminal variant and no `NO_COLOR` path
    beyond `screen_reader`.
- **F2. It breaks on a light terminal.** User messages sit on `on grey11`, a
  near-black band, and dim grey text on a white background drops below
  readable contrast.
- **F3. Three overlay dialects.**
  - The approval overlay uses an orange `⚠` and `[y]`/`[a]` keys.
  - Plan review uses a cyan header with an emoji that falls back to tofu.
  - Questions uses another emoji and a "240s remaining" countdown.
  - All three are fixed at 66 columns whatever the terminal width, and their
    wording mixes "Approve - Safe" with "Reject — add feedback".
- **F4. Missing glyphs.** `⎿` and the overlay emoji are missing from common
  monospace fonts (DejaVu Sans Mono, Noto Sans Mono) and render as tofu.
  There is no ASCII fallback.

### 3.2 Layout and status

- **F5. Five status lines sit above the prompt** (`agt-idle.png`):
  - `✿ Idle`;
  - `anthropic | claude-opus-4-8`;
  - a raw session UUID;
  - the working directory;
  - `0 turns │ cost unknown │ usage unavailable`, with "usage unavailable" in
    warning orange before anything has happened.

  After a turn the first line reads `✿ Idle | ↑ 0 ↓ 0 ?`.
- **F6. Repeated information.** The model and directory appear in the welcome
  card and again in the status block. The hints show `Ctrl+G agents` and
  `← agents`.
- **F7. The welcome card is a placeholder.** "What's new" reads "No list", the
  tip says "create a AGENTS.md", and the card does not mention that no API
  key is set (`/model` shows `ANTHROPIC_API_KEY ✗ not set`).
- **F8. Two hint rows at once.** The hub opens inline while the composer's
  hint row stays visible below it (`agt-hub.png`).

### 3.3 Composer

- **F9. Few editing keys** (`tui/actions.py`).
  - The composer binds only Enter, Ctrl+J, Backspace, Ctrl+U, ←/→, Home/End,
    ↑/↓, Shift+Tab, Tab, Ctrl+G, Ctrl+B and Ctrl+O.
  - Missing: Delete, Ctrl+A/E, Ctrl+W, Ctrl+K/Y, Alt+B/F word movement,
    Ctrl+R history search, an external editor, and undo.
  - Qoder documents all of these (§4).
- **F10. An open picker swallows editing keys.** With the `@` picker open,
  Ctrl+U went to the picker and did not clear the prompt. The leftover text
  was then submitted with `/help` appended, as the message
  `see @src/agenthicc/tui/w/help`.
- **F11. A command takes two Enters.** The first Enter on a highlighted
  command completes its name; a second runs it.

### 3.4 Pickers

- **F12. The `/` picker is unordered** (`agt-slash.png`).
  - It is alphabetical and ungrouped, with no match count or scroll position.
  - Aliases are separate rows: `/background` and `/detach` both show "Detach
    the active session into a background agent".
  - The line under the list repeats the selected row.
  - Claude Code ranks by relevance and wraps descriptions with a hanging
    indent (`cc-slash.png`). OpenCode groups commands under headings, with
    search and right-aligned shortcuts (`oc-slash.png`).
- **F13. The `@` picker is bare paths.** It shows no kind marker beyond a
  trailing `/`, no match highlight and no recency (`agt-at.png`).
- **F14. `/model` is a table, not a picker.** It prints a static provider
  table into the scrollback and says "restart required for the change to take
  effect" (`agt-model.png`).

### 3.5 Transcript

From the replay in `agt-tools.png`:

- **F15. It reads like a log.**
  - Every tool row prints 4 lines of output and "⋯ +N more lines".
  - Every success says "Completed" in green.
  - A memory read dumps raw JSON.
- **F16. Commands are hidden.**
  - `Run('cd /root/youtube_projects/youtube-shorts/objective-create-a)` shows a
    Python repr, truncated at the shared `cd` prefix, with unbalanced quotes.
  - The command that matters is never shown.
- **F17. Long headers.** Turn headers read
  `● assistant (deepseek-v4.1-flash)  05:04:03 CET +0100`.
- **F18. Replay shows the wrong time.** `/replay` stamps every turn with the
  replay's time, not the original.

### 3.6 Errors and diagnostics

- **F19. Missing credentials are reported as a type name.**
  - With no API key, the turn renders "ERROR Agent turn failed (TypeError)".
  - The Anthropic SDK's own message is clear: "Could not resolve
    authentication method. Expected one of api_key, auth_token…" (reproduced).
  - It is discarded, so the user is not told what to do.
- **F20. A missing file looks like a failed tool.** An `@` mention of a path
  that does not exist renders as a failed tool row, `✗ Read(...)`.
- **F21. The hub's help leaks internals.** `?` shows internal condition flags
  in user text: `(library_tab, not needs_approval)`,
  `(editable_definition, not session)`.

## 4. The other TUIs

| Product | What it does well (observed or documented) | Take | Leave |
|---|---|---|---|
| **Claude Code** (`cc-idle.png`, `cc-slash.png`) | Three-line header (version; model · effort · plan; cwd). Notices marked by a left bar. The prompt sits between two rules, with the session title on the top rule. Placeholder suggestion ("Try …"). One status line: mode · agents left, effort right. Ranked `/` list with wrapped descriptions. | Compact header; one status line; session title on the rule; ranked picker; placeholder suggestions. | Brand mark and colours. |
| **OpenCode** (`oc-idle.png`, `oc-slash.png`; [TUI docs](https://opencode.ai/docs/tui/)) | Full-screen, centred, tinted prompt card with accent bar. Agent · model · provider · effort inside the card. Only two hints. Bottom bar: cwd:branch and version. Modal command palette with search, groups and shortcuts. Themes. `/details` toggles tool detail. Leader key (`ctrl+x`). | Grouped palette with shortcuts; the card's in-place model line; tool-detail toggle; theme set. | The full-screen-only model, which loses native scrollback. |
| **OpenAI Codex** (`cx-idle.png`) | Minimal prompt (`› Ask Codex to do anything`, `? for shortcuts`). | A single hint pointing to `?`. | Its own `ERROR rmcp…` log lines printed over the UI (`cx-idle.png`): agenthicc must never let stderr reach the screen (TUX-08). |
| **Qoder** ([interface](https://docs.qoder.com/cli/interface.md)) | The status bar under the input shows model · workspace · +N −N. It can be replaced by a script that receives session JSON, and drops low-priority segments when narrow. Context bar turns green, yellow, red. Full readline keys, Ctrl+R search, `Ctrl+X` external editor, Vim mode. Themes switch automatically with the terminal background; custom themes. OSC status (`idle/working/blocked/done/error`) and a dynamic window title. `/shortcuts`. | Script status line; priority segments; readline set; external editor; auto light/dark; OSC status; window title. | Vim mode for now (a later PRD). |
| **DeepSeek Harness** ([quickstart](https://deepseek-harness.github.io/deepseek-harness/en/guide/quickstart)) | Documented as a Web UI. Model routes apply "immediately without restarting"; the composer is unavailable until a workspace is chosen; approvals follow the active policy. | Switching models without a restart (TUX-05). | — |

## 5. Design principles for the series

1. **One quiet status line, not a block.** Show what the user would act on,
   and drop the rest by priority as the terminal narrows.
2. **Semantic colour only.** Every colour comes from a theme token
   (`accent`, `success`, `warning`, `danger`, `muted`, `subtle`, `user_bg`, …),
   with light and dark variants, `NO_COLOR`, and an ASCII glyph fallback.
3. **The transcript is a narrative.** One line per tool by default, with the
   real command and the result in plain words, and detail on demand.
4. **Every key works where the user's attention is.** Editing keys reach the
   prompt even with a picker open, and Enter on a highlighted command runs it.
5. **One dialog component.** Approvals, plans and questions share a header,
   width, accent, key legend and wording.
6. **Errors say what to do.** A missing key names the variable and the
   command that fixes it; raw tracebacks and stderr never reach the screen.
7. **No regression to responsiveness or accessibility.** AGX-11's frame
   pipeline, TUIR-04's loop-lag budget, AGX-13's keymap and screen-reader mode
   remain the contract.

## 6. Routing

| PRD | Title | Fixes |
|---|---|---|
| [TUX-01](TUX-01-design-tokens-and-themes.md) | Design tokens, themes and glyphs | F1, F2, F4 |
| [TUX-02](TUX-02-layout-and-status-line.md) | Screen layout and one status line | F5, F6, F8 |
| [TUX-03](TUX-03-welcome-and-first-run.md) | Welcome screen and first run | F7, F19 (onboarding half) |
| [TUX-04](TUX-04-composer-ergonomics.md) | Composer ergonomics | F9, F10, F11 |
| [TUX-05](TUX-05-pickers-and-command-palette.md) | Pickers and command palette | F12, F13, F14 |
| [TUX-06](TUX-06-transcript-density-and-tool-rows.md) | Transcript density and tool rows (supersedes BEAU-2/3) | F15, F16, F17, F18, F20 |
| [TUX-07](TUX-07-unified-dialogs.md) | One dialog component for approvals, plans and questions | F3 |
| [TUX-08](TUX-08-errors-notices-and-diagnostics.md) | Errors, notices and diagnostics (supersedes BEAU-4) | F19, F21 |
| [TUX-09](TUX-09-navigation-and-transcript-view.md) | Navigation: transcript view, search and fullscreen mode | — (new capability) |
| [TUX-10](TUX-10-motion-feedback-and-terminal-integration.md) | Motion, feedback and terminal integration | — (polish) |

Order:
1. TUX-01, whose tokens every other PRD uses.
2. TUX-02 and TUX-04, the largest everyday gains.
3. TUX-06 and TUX-08.
4. TUX-05, TUX-07 and TUX-03.
5. TUX-09 and TUX-10.

BEAU-1's renderer inventory remains the transcript's source of truth. TUX-06
and TUX-08 replace BEAU-2/3/4's visual specifications, which were never
implemented. BEAU-5 (workflow mutation blocks) stays as written, restyled
with TUX-01 tokens.
