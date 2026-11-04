---
title: "TUIR-05: The attention badge says what it counts"
status: Implemented (2026-11-04; tools wave 6, pending merge)
version: 1.0.0
date: 2026-11-03
scope: "the footer's background-attention badges: name what they count so they are not read as subagent state"
related_prds:
  - TUIR-00
  - PRD-141
  - AGX-10
tags:
  - tui
  - ux
  - background-sessions
---

# TUIR-05: The attention badge says what it counts

## Implementation status (2026-11-04)

Delivered in tools wave 6 (branch `tools-wave-6-result-contract`).

- `agenthicc.tui.attention_labels` words both badges in singular and plural:
  `◆ 7 sessions need you`, `◆ 1 session needs you`, `● 1 session running`.
  The footer, the terminal title and the coalesced toast all use it.
- When space allows, the needs-you badge carries the `agents.open_hub` key
  from the keymap: `◆ 7 sessions need you (Ctrl+G)`.
- Narrower widths step down in this order:
  1. drop the key;
  2. shorten the running badge to `● 1 sess.`;
  3. shorten both to `sess.`;
  4. show the bare numbers.

  The needs-you badge keeps its words longest.
- Subagent counts stay on the pool panel; the footer row only counts
  sessions.

Tests: `tests/unit/test_attention_badges.py` renders a real `AppState` with
seven waiting sessions at 80 and 120 columns, steps down through 40, 26 and
14 columns, and checks singular and plural for 1 and 2.
`tests/unit/test_tui_attention.py` checks the title and the toast.

## 1. Problem

TUIR-00 F1. In the reported screenshot, a pool of six explorers ran while the
footer read `◆ 7 need you | ● 1 running`. The badge is built in
`src/agenthicc/tui/workspace/components.py:693-699` from
`conversation.background_needs_you` and `background_running`, which
`src/agenthicc/tui/attention.py:146-151` publishes from the **background
session registry** (PRD-141): sessions waiting for approval or input, or
orphaned. It has nothing to do with the subagent pool next to it, but beside a
pool panel it reads as "seven of my subagents need me" and "only one is
running" — both false, and it sent the investigation looking for a subagent
state bug.

## 2. Goals

1. **Name the unit.** The badges say what they count: `◆ 7 sessions need you`
   and `● 1 session running` (singular and plural), and the terminal title
   follows (`attention.py:177-183`).
2. **Point at the place to act.** When space allows, the needs-you badge
   carries the `agents.open_hub` key from the keymap (`Ctrl+G` by default,
   `src/agenthicc/tui/actions.py:176-182`), so the user can open the hub where
   those sessions are listed.
3. **Subagent counts stay on the panel.** The subagent panel summary keeps
   reporting workers; the footer never mixes the two counts.
4. **Narrow widths keep meaning.** At narrow widths the badge drops the word
   before it drops the unit (`◆ 7 sess.` before `◆ 7`), following the AGX-08
   width matrix.

## 3. Acceptance criteria

1. With seven waiting background sessions and a running subagent pool, the
   footer reads `◆ 7 sessions need you` and the panel summary reports the
   pool's own workers (render test at 80 and 120 columns).
2. Singular and plural forms are correct for 1 and 2.
3. The terminal title uses the same words.
