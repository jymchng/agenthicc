---
title: "TUX-01: Design tokens, themes and glyphs"
status: Proposed
version: 1.0.0
date: 2026-11-05
scope: "one semantic palette and glyph set for every TUI surface, built-in dark/light/high-contrast/ANSI themes, terminal background detection, NO_COLOR, user themes and /theme"
related_prds: [TUX-00, TUX-02, TUX-06, TUX-07, BEAU-1, AGX-11, AGX-13]
tags: [tui, design-system, theme, accessibility, color]
---

# TUX-01: Design tokens, themes and glyphs

## 1. Problem

From TUX-00: F1, F2 and F4.

- **Colour is hard-coded.**
  - Colours are named at the call site, in 244 places across `src/agenthicc/tui`:
    - `appender.py`: 36 (for example `style="on grey11"` at `:691`, `[green]Completed[/green]` at `:529`);
    - `agents_hub/render.py`: 21;
    - `components.py`: 16 (the state colours dictionary at `components.py:366`);
    - `agents_hub/hub.py`: 15.
  - The input prompt writes raw SGR (`\x1b[1;33m`, `tui/input/renderer.py:46`).
  - Code and diffs use Pygments' `monokai` (`diff_renderer.py:81`).
- **Problems this causes.**
  - Changing the accent means editing dozens of files, and there is no light-terminal variant.
  - A user message's `on grey11` band is a black bar on a light background.
  - The same meaning gets different colours: "running" is cyan in the status line, magenta for pools, and yellow for "thinking".
  - The approval, plan and question overlays use three different header colours (F3).
- **Glyphs.**
  - `⎿`, `✿` and the emoji in overlay headers are missing from common monospace fonts and render as tofu (`screens/ov-plan.png`, `ov-questions.png`).
  - `screen_reader` is the only mode without them, and it also removes all box drawing. There is no "plain glyphs, keep the layout" option.

## 2. Goals

1. **A token module, `tui/theme.py`.**
   - It defines a frozen `Theme` dataclass of semantic **colour tokens**:

     | Token | Meaning | Dark default | Light default |
     |---|---|---|---|
     | `text` | body text | default fg | default fg |
     | `muted` | secondary text, metadata | `#8a8f98` | `#5c6370` |
     | `subtle` | rules, borders, ghosts | `#3a3f47` | `#d0d4da` |
     | `accent` | focus, selection, the prompt chevron, links | `#7aa2f7` | `#2f5bd3` |
     | `accent_bg` | selected-row background | `#1f2a44` | `#e3ebff` |
     | `user_bg` | the user-message band | `#1c1f24` | `#f1f3f5` |
     | `success` | a confirmed good result | `#9ece6a` | `#2e7d32` |
     | `warning` | needs attention soon | `#e0af68` | `#a15c00` |
     | `danger` | failure, destructive choice | `#f7768e` | `#c62828` |
     | `info` | running, neutral progress | `#7dcfff` | `#00639b` |
     | `agent` | subagent/worker identity | `#bb9af7` | `#7b3fbf` |
     | `diff_add` / `diff_del` / `diff_add_bg` / `diff_del_bg` | diff lines | from the theme | from the theme |
     | `mode_safe` / `mode_plan` / `mode_yolo` | permission mode badges | from the theme | from the theme |

   - It also defines a `syntax_theme` name (a Pygments style) for code and diffs.
   - Tokens are exposed as Rich style names (`theme.accent` → `"agenthicc.accent"`) through a `rich.theme.Theme` installed on the session `Console`. Markup therefore reads `[agenthicc.accent]…[/]`, never a colour name.
2. **A glyph set in the same module.**
   - `Glyphs` covers:
     - the prompt chevron;
     - the bullet;
     - the tool-result elbow;
     - success, failure and warning;
     - the spinner frames;
     - the rule character;
     - the ellipsis;
     - arrows;
     - the box corners.
   - It has two complete sets:
     - `unicode`: `❯ ● └ ✓ ✗ ! ⠋⠙⠹⠸⠼⠴⠦⠧ ─ … ↑ ↓ ╭╮╰╯`;
     - `ascii`: `> * \` + x ! |/-\ - ... ^ v ++++`.
   - Only characters present in DejaVu Sans Mono, Noto Sans Mono, Menlo, Consolas and Cascadia Mono are allowed in `unicode`. A test checks each glyph against the bundled DejaVu font's cmap.
   - No emoji appear in chrome, so `⎿` and the 📋 and ❓ headers go. (Model text may of course contain emoji.)
3. **Built-in themes.**
   - `dark` (default on a dark background), `light`, `high-contrast` (WCAG AAA 7:1 body text) and `ansi`.
   - `ansi` uses only the 16 terminal palette colours, so it inherits the user's terminal scheme, as Qoder's `ANSI` theme does.
   - Every theme meets WCAG AA (4.5:1) for `text` and `muted` against its background, and 3:1 for `subtle`. A unit test computes the ratios.
4. **Choosing the theme.**
   - Configuration: `[ui] theme = "auto" | "dark" | "light" | "high-contrast" | "ansi" | "<custom name>"`, and `[ui] glyphs = "auto" | "unicode" | "ascii"`.
   - `auto` queries the terminal background with OSC 11 at start-up (50 ms timeout, only on a TTY, through `get_backend()`). If that fails, it falls back to `COLORFGBG`, then to `dark`.
   - `glyphs = "auto"` picks `ascii` when the locale encoding is not UTF-8 or `TERM=linux`.
   - `NO_COLOR` (any value) forces the `ansi` theme with every colour token removed. Bold, dim and reverse are kept, so selection stays visible.
   - `screen_reader` keeps its current meaning, and now also implies `glyphs = "ascii"`.
5. **Custom themes.**
   - A TOML file in `~/.agenthicc/themes/<name>.toml` or `.agenthicc/themes/<name>.toml` (the project one wins) sets any subset of tokens over a base (`extends = "dark"`).
   - Unknown tokens and invalid colours fail with the file, the line and the token name. A broken theme falls back to `auto` with one notice, and the session still starts.
6. **`/theme`.**
   - A picker lists the themes, with a live preview: a user message, a tool row, a diff and the status line, rendered with each theme while it is highlighted.
   - Enter applies the theme at once (no restart) and offers to save it to the user configuration.
   - `agenthicc config show` prints the effective theme and glyph set.
7. **Migration of every literal.** Every colour literal and every raw SGR sequence in `src/agenthicc/tui` and `diff_renderer.py` is replaced by a token. This includes:
   - the 244 Rich colour names;
   - `build_prompt`'s escape codes;
   - `diff_renderer`'s `dim green` and `monokai`;
   - the state colour dictionary in `components.py`;
   - the hub renderer.

   A lint test (`tests/unit/test_theme_tokens.py`) fails on any colour name or SGR literal in those paths, outside `tui/theme.py`. It uses a regular expression over `[red]`, `style="…"` and `\x1b[3xm`, with an allow-list only for `bold`, `dim`, `italic`, `reverse` and `underline`.

## 3. Non-goals

- Restyling individual components. The other TUX PRDs specify their own layouts and use these tokens.
- Changing colours in the model's output, which is rendered from Markdown.
- 24-bit colour detection beyond what Rich already does. On a 256-colour terminal, Rich downsamples the hex tokens.

## 4. Acceptance criteria

1. Running `rg -n '\[(red|green|yellow|blue|magenta|cyan|white|grey\d+)' src/agenthicc/tui` and the SGR equivalent returns nothing outside `tui/theme.py`. The lint test enforces this.
2. With `theme = "light"` on a white terminal, a replayed session shows:
   - a light `user_bg` band;
   - readable muted text (contrast ≥ 4.5:1, asserted on the computed palette);
   - a light syntax theme in diffs.

   A screenshot is added to `prds/tui-sleek/screens/` with `shot.py` and a light background.
3. `NO_COLOR=1 agenthicc --headless` is unaffected, and `NO_COLOR=1 agenthicc` writes no SGR colour parameters (30–37, 38, 40–47, 48, 90–97, 100–107). A test checks the captured output of a scripted session.
4. `glyphs = "ascii"` renders the welcome screen, status line, a tool row, a diff and every overlay without a character above U+007F. The test renders each with a recording console and checks this.
5. `/theme` switches from dark to light during a session. The next frame and every later scrollback line use the new tokens, and nothing is restarted.
6. A custom theme with a misspelled token reports `themes/acme.toml: unknown token "acent"` and starts with `auto`.
7. The OSC 11 query is skipped when stdin is not a TTY, and never adds more than 50 ms to start-up. This is checked by AGX-14's start-up budget test.

## 5. Rollout

- Ship in two steps.
  1. Add the module, the themes and the lint test, with an allow-list that includes every current offender.
  2. Burn the allow-list down to zero, file by file. The PRD is done when the allow-list is empty.
- The `dark` theme's values are chosen to look like today's screen on a dark terminal, so users see an improvement, not a different product.

## 6. Verification

- `uv run pytest tests/unit/test_theme*.py tests/unit/test_welcome*.py tests/unit/test_appender*.py -q`
- `uv run nox -s lint typecheck`
- Light, dark and ASCII screenshots of the six reference screens in TUX-00 §2.
