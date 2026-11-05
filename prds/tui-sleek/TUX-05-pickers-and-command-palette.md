---
title: "TUX-05: Pickers and command palette"
status: Proposed
version: 1.0.0
date: 2026-11-05
scope: "the / command picker, the @ mention picker, a Ctrl+P command palette with search, groups and shortcuts, and interactive pickers for /model, /mode, /agent and /theme that apply without a restart"
related_prds: [TUX-00, TUX-01, TUX-04, TUX-07, PRD-45, PRD-77, AGX-09, AGX-13]
tags: [tui, pickers, command-palette, model-switching, ergonomics]
---

# TUX-05: Pickers and command palette

## 1. Problem

From TUX-00: F12, F13 and F14.

**The `/` picker** (`screens/agt-slash.png`) is built by `SlashCommandHandler.get_matches` (`tui/triggers/slash_command.py:36`). Its problems:

- **Matching and order.**
  - It matches by **prefix only** (`name.startswith(partial)`): `/del` finds nothing for `/agent delete`, and `/comp` does not find `/compact` by its description.
  - Results come in registry order, which is alphabetical. The commands used daily (`/model`, `/compact`, `/clear`, `/resume`) are scattered among the rest.
- **Aliases are separate rows**, with the same description: `/background` and `/detach`, `/cfg` and `/config`.
- **No position.** There is no count or position ("12 of 48") and no scroll indicator.
- **The selection is shown twice.** The selected row is drawn in reverse video, and its hint is repeated on the line below (`trigger_picker.py:159–160`).
- **Groups are invisible.** `Command.group` already exists (`commands/command.py:158`: Built-in, Skills, Plugins, MCP) but is not shown.

**The `@` picker** (`agt-at.png`):

- It shows bare relative paths. A directory differs from a file only by a trailing `/`.
- The matched characters are not highlighted.
- Recently mentioned or edited files are not preferred.

**`/model`** (`commands/builtins.py:636`) prints a static table into the scrollback, then "Note: restart required for the change to take effect" (`:690`).

**What the others do:**

- Claude Code ranks the `/` list by relevance and wraps descriptions with a hanging indent (`cc-slash.png`).
- OpenCode's Ctrl+P palette (`oc-slash.png`) is a centred modal with:
  - a search field;
  - group headings (Suggested, Session, Agent, …);
  - right-aligned shortcuts;
  - an accent-coloured selected row.

  Its model picker switches without a restart.
- DeepSeek Harness applies a model route "immediately without restarting".

## 2. Goals

### 2.1 One picker component

`tui/workspace/picker.py: Picker[T]`, rendered with TUX-01 tokens.

```text
 ❯ /co▌
   ─ Suggested ──────────────────────────────────────────────
 ▌ /compact        Summarise the conversation to free context      ctrl+x c
   /config         Show or change configuration
   ─ Session ────────────────────────────────────────────────
   /copy           Copy the last response
   /cost           Token and cost report (alias /usage)
                                                         4 of 4 ·  ↑↓ tab ⏎
```

- **The selected row** has an `accent` bar (`▌`) and an `accent_bg` background, not reverse video.
- **Matched characters** are bold in `accent`.
- **Shortcuts** are right-aligned in `muted`, from the keymap (AGX-13). Every command with a bound key shows it.
- **Group headings** are muted rules.
- **The footer** shows `N of M` and the keys. The repeated hint line is removed.
- **Descriptions** wrap with a hanging indent under the description column, at most two rows. Below 60 columns the description goes on its own row.
- **Rows that cannot run** are dimmed, with the reason on the second row, for example "not available while a turn runs" (PRD-189 `BusyPolicy`).
- **Height.** The picker uses up to `min(12, rows / 3)` rows, never covering the composer.

### 2.2 The `/` picker

- **Matching.** It is fuzzy over the name, the aliases and the description, using `mentions.matcher.filter_and_rank` (already used by `@`). The ranking is:
  1. exact name;
  2. name prefix;
  3. alias prefix;
  4. name fuzzy;
  5. description word.

  Ties are broken by how often and how recently the user ran the command, counted per project in the history store.
- **Grouping.**
  - With an empty fragment it is grouped: Suggested (the five most used), Session, Agents, Workflows, Configuration, then the existing Skills, Plugins and MCP groups.
  - Each built-in command is assigned one of these groups in `commands/builtins.py`.
  - With a fragment, the results are a single ranked list.
- **Aliases** collapse into their command's row, as `(alias /usage)`. Typing an alias ranks its command first.

### 2.3 The `@` picker

- Each row has a kind glyph and a muted detail:
  - directory `▸`;
  - file `·`;
  - agent `@agent:`;
  - a symbol, when the code graph is available (`#`).

  The detail is the size or line count for a file, and the entry count for a directory.
- Paths are shown as directory in `muted` plus filename in `text`, with matched characters highlighted.
- Recently mentioned files (this session) and files changed in the working tree (`git status`, run off the event loop) are boosted, and shown first when the fragment is empty.
- Entries ignored by `.gitignore` are excluded unless the fragment names them exactly.

### 2.4 The Ctrl+P command palette

- Ctrl+P on an empty prompt opens a centred modal: the TUX-07 dialog frame, 60 % width, clamped to 60–100 columns.
- It lists every command, every keymap action that has a binding (for example "Open agents hub ctrl+g", "Cycle mode shift+tab"), and every theme.
- It has a search field and the same groups.
- Enter runs the item. An item that needs arguments opens its argument picker inside the same modal.
- On a non-empty prompt, Ctrl+P keeps its history meaning (TUX-04).

### 2.5 Pickers for the settings that change most

`/model`, `/mode`, `/agent` and `/theme` with no argument open a picker, not a table.

- **`/model`:**
  - **Rows.** Rows are grouped by provider, plus saved profiles. Each row shows the model, its context window and whether the credentials resolve:
    - `✓`;
    - or `✗ set ANTHROPIC_API_KEY`, which is dimmed and cannot be chosen until the key is set.
  - **Applying it.** Choosing a row applies it **from the next turn**, without a restart.
    - The session's `execution.provider` and `model` are updated, and the transport is rebuilt through the same factory the session used at start-up.
    - A `model_changed` event is emitted into the conversation, which renders as a one-line notice: `◆ model → sonnet-4.6 (from next turn)`.
    - It asks "Save as default?", and `s` saves it to the user configuration.
  - **While a turn runs**, the change is queued until the turn ends.
- **`/mode`** lists Safe, Plan and Yolo, plus plugin modes, with each mode's one-line policy summary.
- **`/agent`** reuses AGX-09's library list.
- **`/theme`** is specified in TUX-01.

With arguments (`/model anthropic claude-sonnet-4-6`), the command applies directly, as now, but without the restart note.

## 3. Non-goals

- New commands, beyond `/status` (TUX-02) and `/release-notes` (TUX-03).
- Changing what the commands do, except `/model` no longer needing a restart.
- A code-symbol index. `#` symbol rows appear only where codegraph data is already available to the session.

## 4. Acceptance criteria

1. **Ranking.**
   - `/comp` ranks `/compact` first.
   - `/summar` finds `/compact` by its description.
   - `/usage` ranks `/cost` first, with `(alias /usage)`.

   Each is a unit test on `SlashCommandHandler.get_matches`.
2. **Aliases.** No two rows in an empty-fragment `/` listing have the same description. The test checks the whole built-in registry.
3. **Shortcuts.** Every command bound in `DEFAULT_ACTIONS` shows its key in its row.
4. **The `@` picker.**
   - With a changed file `src/a.py` in the working tree, the first row of an empty `@` picker is `src/a.py`.
   - A directory row starts with `▸`.
   - The `git status` call is not on the event loop (the TUIR-04 lag gate passes with a 10,000-file fixture).
5. **The palette.** Ctrl+P on an empty prompt opens the palette, and typing `agents` finds "Open agents hub ctrl+g". Enter opens the hub.
6. **`/model` with no restart.** In an e2e test with two `MockTransport`s:
   - choosing the second model makes the next turn's request go to the second transport;
   - the conversation contains one `model_changed` event;
   - no restart text appears anywhere (`rg "restart required" src/` is empty).
7. **Missing credentials.** A provider with no credentials cannot be selected, and its row names the variable to set.
8. **Width.** At 50 columns, descriptions move to their own row and no row exceeds the width. There are snapshot tests at 50, 80 and 120 columns.

## 5. Rollout

- `TriggerPickerOverlay`'s rendering is replaced by `Picker`, keeping its overlay contract.
- The `display` field of `MatchItem` is dropped in favour of structured `label`, `detail`, `group`, `glyph` and `keys` fields. All triggers (`at_mention.py`, `slash_command.py` and plugin triggers through the documented plugin API) are migrated, and the plugin documentation is updated.
- Model switching reuses the session's transport factory. It is covered by the same credential redaction (no key appears in any event payload).

## 6. Verification

- `uv run pytest tests/unit/test_slash*.py tests/unit/test_at_mention*.py tests/unit/test_picker*.py tests/e2e/test_model_switch*.py -q`
- `uv run nox -s llms_check` (the `MatchItem` and `Picker` symbols are public).
- Screenshots of the `/`, `@`, palette and `/model` pickers.
