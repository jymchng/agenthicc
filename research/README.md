# research/

Two investigations, kept out of `docs/` because they are evidence dossiers rather
than user documentation.

| File | What it covers |
| --- | --- |
| [`ask-user-timeout-in-the-agents-hub.md`](ask-user-timeout-in-the-agents-hub.md) | Why a background/hub `ask_user` did not appear to time out, where the deadline actually lives, the one real edge case found, and the change that made the background wait default to 300 s. |
| [`claude-code-input-panel-cursor.md`](claude-code-input-panel-cursor.md) | Claude Code's input-panel cursor: the `cursorOffset` model, painted inverse block vs. real terminal caret, grapheme→word→row→line→history navigation, wrapping/viewport/click mapping, chips, vim mode, edge cases, and a bundle-anchor appendix for `@anthropic-ai/claude-code@2.1.100`. |

## Ground truth used

*This repository* — `src/agenthicc/…`, `tests/…`, `docs/…`, `CHANGELOG.md`,
`prds/agents-interaction/AGI-02-*.md`, read at commit `1b3babe` ("feat: update"),
which contains the AGI-02 amendment.

*Claude Code* — the shipped bundle, not the marketing copy:

- npm tarball `@anthropic-ai/claude-code@2.1.100` → `package/cli.js`
  (13.4 MB, single line, minified ES module, no dependencies).
- Pages fetched under `code.claude.com/docs/en/`:
  `interactive-mode`, `fullscreen`, `terminal-config`, `keybindings`,
  `settings-reference`, `agent-view`.

Quotations from `cli.js` are verbatim slices of the minified bundle; identifiers
are the minifier's, so they are cited by byte offset within the 2.1.100 bundle
where the name alone would be meaningless.
