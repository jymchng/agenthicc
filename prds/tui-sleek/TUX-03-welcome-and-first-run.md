---
title: "TUX-03: Welcome header and first run"
status: Proposed
version: 1.0.0
date: 2026-11-05
scope: "the start-up header, the release notes it shows, a credential and workspace check before the first prompt, first-run onboarding, and composer suggestions"
related_prds: [TUX-00, TUX-01, TUX-02, TUX-05, TUX-08, AGI-04, AGX-02]
tags: [tui, onboarding, welcome, ergonomics]
---

# TUX-03: Welcome header and first run

## 1. Problem

From TUX-00: F7, and the onboarding half of F19. See `screens/agt-idle.png`.

**The welcome card** (`tui/welcome.py:274`, `render_welcome`) is a full-width rounded panel, 13 rows tall at 120 columns, with:

- **A mascot, the name, and "state-driven agent operating system".**
- **"Welcome back!"**, even on the first run in a new directory.
- **The model and directory**, which the status block below repeats (F6).
- **A fixed tip**, "Run /init to create a AGENTS.md file …". It has an article error, and is shown even when `AGENTS.md` already exists.
- **"What's new".** It is fetched from `https://agenthicc.dev/changelog.json` (`welcome.py:31`). When the fetch fails or returns nothing, the section prints "No list".

**Nothing checks that a turn can succeed.**

- With no `ANTHROPIC_API_KEY`, the card looks normal.
- The first prompt fails with "ERROR Agent turn failed (TypeError)" (`screens/agt-error.png`).
- Only `/model` reveals `ANTHROPIC_API_KEY ✗ not set` (`agt-model.png`).

**What the others do:**

- Claude Code's header is three lines: version; model · effort · plan; cwd (`cc-idle.png`). It asks once whether to trust a new folder.
- OpenCode shows only its logo and the prompt card.
- DeepSeek Harness disables the composer until a workspace is chosen.
- Qoder can hide the banner (`hideBanner`).

## 2. Goals

### 2.1 A compact header

It is printed once into the scrollback, at most four rows:

```text
 ◆ agenthicc 0.42.0                                   what's new: /release-notes
   opus-4.8 · anthropic · Safe mode
   ~/python_projects/agenthicc  (main)
```

- **Line 1:** the name in `accent` and the installed version, read from package metadata.
- **Line 2:** the model, provider and mode.
- **Line 3:** the working directory, home-abbreviated and middle-truncated (`~/py…/agenthicc`), with the git branch.
- **Right-aligned on line 1:** `what's new: /release-notes`, shown only when the version has changed since the last run (stored in `~/.agenthicc/state.json`).
- The mascot and tagline appear only on the first run on a machine, and never again.
- `[ui] banner = "full" | "compact" | "off"`; the default is `compact`. `off` prints nothing, as Qoder's `hideBanner` does.
- At under 60 columns, lines 2 and 3 merge and wrap.

### 2.2 Release notes that exist

- The packaged `CHANGELOG.md` is the source. The network fetch is deleted, along with its cache and the "No list" path.
- `/release-notes` shows the entries for the installed version and every version since the last one seen, in a pager overlay (TUX-07's dialog with a scrollable body).

### 2.3 A pre-flight check before the first prompt

It runs during start-up, off the critical path (AGX-02), and finishes within the start-up budget. It checks:

- **Credentials.** For the active provider and saved profile, there must be a resolvable key or token (`PROVIDER_API_KEY_ENVVAR`, the profile's `api_key_env`, or `SecretReference`). The key is not sent anywhere: no network call.
- **The model.** It must be known to the provider's model list in `config.PROVIDER_DEFAULT_MODELS` metadata, or explicitly configured.
- **The workspace.** It must be writable, or the mode must be Plan.
- **Required tools.** `bwrap` on Linux when isolation is configured; `rg`.

Results:

- **A failed check prints one notice under the header** (TUX-08 notice style), naming the fix:

  ```text
   ! No API key for anthropic. Set ANTHROPIC_API_KEY, or run /model to pick another provider.
  ```

- **The composer stays usable** for slash commands. Submitting a prompt while the credential check has failed does not call the provider: it shows the same notice and opens `/model` (TUX-05).
- **When all checks pass, nothing is printed.**

### 2.4 First run in a directory

- When the directory is not yet known to agenthicc and is not a git repository, or is the home directory, show TUX-07's dialog once: "Work in ~/Downloads? agenthicc can read and change files here." The choices are "Yes", "Yes, in Plan mode" and "Choose another directory…".
- The answer is stored per directory, in the existing project trust store (`plugins/trust.py`).

### 2.5 Composer suggestions

The empty composer's placeholder (TUX-02) is chosen by context:

- `AGENTS.md` is missing: "try /init to write AGENTS.md for this repository".
- An interrupted session exists for the directory: "resume your last session: /resume".
- Background agents need you: "2 agents need you — press ←".
- Otherwise, a rotating, deterministic tip from a list of 20 tips. Each tip names a feature with its key (`@ adds a file`, `ctrl+r searches history`, `shift+tab cycles mode`). The tip changes once per session, never while visible.

Tab accepts a suggestion that is a command.

## 3. Non-goals

- A setup wizard, or a provider login flow (OAuth). `/model` (TUX-05) is the place to change the provider.
- Changing `/init`'s output.

## 4. Acceptance criteria

1. **Height.** At 120 columns, after the first run, the header is ≤ 3 rows, plus at most one notice row (tested with `render_welcome` at fixed widths).
2. **No placeholders.** `No list`, `Welcome back!` and `a AGENTS.md` appear nowhere in `src/`, and no start-up path makes a network request for release notes. The test patches the shared HTTP client to fail on any call during start-up.
3. **Missing key.** With `ANTHROPIC_API_KEY` unset and the anthropic provider active:
   - the header is followed by the missing-key notice;
   - submitting "hello" makes no provider request (`MockTransport` asserts zero calls) and opens the `/model` picker.

   This is an e2e test with a cassette.
4. **A key from a profile.** A key provided through a saved profile's `api_key_env` passes the check, and so does Ollama with no key.
5. **`/release-notes`.** It lists the entries of the packaged `CHANGELOG.md` for the current version. The `what's new` hint appears once after an upgrade and not on the next start.
6. **`banner = "off"`.** It prints nothing before the composer, except a failed pre-flight notice.
7. **Suggestions.** In a repository without `AGENTS.md` the placeholder suggests `/init`, and with `AGENTS.md` it does not.

## 5. Rollout

- `fetch_changelog`, `load_cached_changelog`, the cache file and `CHANGELOG_URL` are deleted, along with their tests.
- The header is the only welcome renderer.
- The pre-flight check is a pure function, `runners/preflight.py: check_session(config, env) -> tuple[PreflightIssue, ...]`, reused by `agenthicc config show` and `/status`.

## 6. Verification

- `uv run pytest tests/unit/test_welcome*.py tests/unit/test_preflight*.py tests/e2e/test_first_run*.py -q`
- AGX-02's start-up budget test.
- Screenshots: first run, normal start, missing key.
