# A sleek, ergonomic TUI (TUX series)

These PRDs redesign agenthicc's interactive screen so that it is quieter, more consistent and faster to drive from the keyboard. They come from studying:

- agenthicc itself, run live in tmux;
- Claude Code 2.1.318, OpenAI Codex 0.166.0 and OpenCode 1.15.11, run in the same 120×40 pane;
- Qoder and DeepSeek Harness, from their documentation.

Start with the study, [TUX-00](TUX-00-study-sleek-ergonomic-tui.md). It holds the screenshots (`screens/`), the 21 findings (F1–F21) with their `file:line` evidence, the comparison table and the design principles.

| PRD | Title | Fixes | Priority |
|---|---|---|---|
| [TUX-00](TUX-00-study-sleek-ergonomic-tui.md) | Study | — (evidence, comparison, routing) | — |
| [TUX-01](TUX-01-design-tokens-and-themes.md) | Design tokens, themes and glyphs | F1 244 colour literals; F2 light terminals; F4 tofu glyphs | P0 (foundation) |
| [TUX-02](TUX-02-layout-and-status-line.md) | Screen layout and one status line | F5 five status rows; F6 repeated info; F8 two hint rows | P0 |
| [TUX-03](TUX-03-welcome-and-first-run.md) | Welcome header and first run | F7 placeholder card; missing-key onboarding | P1 |
| [TUX-04](TUX-04-composer-ergonomics.md) | Composer ergonomics | F9 missing readline keys; F10 picker swallows keys; F11 two Enters | P0 |
| [TUX-05](TUX-05-pickers-and-command-palette.md) | Pickers and command palette | F12 unranked `/` list and alias rows; F13 bare `@` rows; F14 `/model` needs a restart | P1 |
| [TUX-06](TUX-06-transcript-density-and-tool-rows.md) | Transcript density and tool rows | F15 log-like rows; F16 repr commands; F17 long headers; F18 replay times; F20 missing mention as a failed tool | P0 |
| [TUX-07](TUX-07-unified-dialogs.md) | One dialog component for approvals, plans and questions | F3 three overlay dialects at 66 columns; approvals that do not show the command or diff | P1 |
| [TUX-08](TUX-08-errors-notices-and-diagnostics.md) | Errors, notices and diagnostics | F19 `Agent turn failed (TypeError)`; F21 internal names in help; stray stderr | P0 |
| [TUX-09](TUX-09-navigation-and-transcript-view.md) | Navigation: transcript view, search and fullscreen mode | Reaching details that TUX-06 collapses; search; opt-in fullscreen; `/rewind` | P2 |
| [TUX-10](TUX-10-motion-feedback-and-terminal-integration.md) | Motion, feedback and terminal integration | Spinner and verbs, turn-end rows, unfocused notifications, title and progress OSC, OSC 8 links, `/shortcuts` | P2 |

## Order

1. TUX-01, whose tokens and glyphs every other PRD renders with.
2. TUX-04 and TUX-02, the largest everyday gains.
3. TUX-08 and TUX-06. TUX-06's structured payloads change the event log, so TUX-08's `kind` field ships in the same release.
4. TUX-07, TUX-05 and TUX-03.
5. TUX-09, then TUX-10.

## Relation to BEAU

- BEAU-1's renderer inventory and coverage rules remain the transcript's contract.
- TUX-06 replaces BEAU-2's and BEAU-3's render blocks, which were never implemented. TUX-08 replaces BEAU-4's.
- BEAU-5 (workflow and plan mutation blocks) stands, restyled with TUX-01 tokens.

## Keys added across the series

The table lists the new composer and global keys. Every key is in `DEFAULT_ACTIONS`, can be rebound, and is listed by `/shortcuts`.

| Key | Action | PRD |
|---|---|---|
| Ctrl+A/E, Alt+B/F, Ctrl+←/→ | line and word motion | TUX-04 |
| Ctrl+W, Alt+D, Ctrl+K, Ctrl+U, Ctrl+Y | kill and yank | TUX-04 |
| Ctrl+Z / Ctrl+Shift+Z | undo / redo | TUX-04 |
| Shift+Enter, Alt+Enter | newline | TUX-04 |
| Ctrl+R | reverse history search | TUX-04 |
| Ctrl+X Ctrl+E | external editor | TUX-04 |
| Ctrl+P (empty prompt) | command palette | TUX-05 |
| 1–9 | choose and confirm in a dialog | TUX-07 |
| Ctrl+T | transcript view | TUX-09 |
| `?` (empty prompt) | shortcuts | TUX-10 |

Ctrl+O stays `subagents.focus`.

## Reproducing the screenshots

`screens/shot.py` turns a tmux pane, or a saved ANSI capture, into a PNG with its colours:

```bash
python prds/tui-sleek/screens/shot.py <tmux-session> out.png [-S start -E end]
python prds/tui-sleek/screens/shot.py capture.ansi out.png 120
```

It needs Pillow, Rich, DejaVu Sans Mono and Noto Sans Mono. Characters missing from both fonts render as tofu, which is how finding F4 was confirmed.
