---
title: "TUX-03: Welcome header and first run"
status: Implemented with amendments (2026-11-14, branch tux-tux03)
version: 1.0.0
date: 2026-11-05
scope: "the start-up header, the release notes it shows, a credential and workspace check before the first prompt, first-run onboarding, and composer suggestions"
related_prds: [TUX-00, TUX-01, TUX-02, TUX-05, TUX-08, AGI-04, AGX-02]
tags: [tui, onboarding, welcome, ergonomics]
---

# TUX-03: Welcome header and first run

## Implementation status

Implemented on branch `tux-tux03`. Every acceptance criterion in §4 has a test;
the table names it. The amendments to the design follow.

| AC | Test |
|---|---|
| 1 Height | `tests/unit/test_welcome_header.py::test_the_header_is_at_most_three_rows_after_the_first_run` (widths 40–200), `test_the_first_run_header_is_at_most_four_rows`, `test_a_notice_row_after_the_header_stays_within_the_budget`; `tests/e2e/test_first_run_e2e.py::test_a_later_start_is_three_rows_and_whats_new_appears_only_after_an_upgrade` (the real CLI in a pseudo-terminal) |
| 2 No placeholders | `test_welcome_header.py::test_no_placeholder_text_appears_anywhere_in_src`, `test_the_remote_changelog_is_gone_from_src_and_scripts`; `tests/unit/test_first_run_session.py::test_starting_up_makes_no_network_request` (the shared HTTP client and `httpx` fail on any call) |
| 3 Missing key | `tests/e2e/test_first_run_e2e.py::test_a_prompt_with_no_key_makes_no_provider_request_and_opens_model` (a scripted `MockTransport` records zero calls; `/model` is dispatched; the notice appears under the header and again after "hello") and `test_a_first_run_without_a_key_shows_the_header_the_notice_and_holds_the_prompt` (the real CLI, read through `pyte`); `test_first_run_session.py::test_a_prompt_with_no_key_is_not_sent_and_opens_model` and the unblocking tests |
| 4 Key from a profile; Ollama | `tests/unit/test_preflight.py::test_a_key_from_a_saved_profiles_api_key_env_passes`, `test_ollama_needs_no_key`, `test_a_secret_reference_names_its_variable`, `test_an_anthropic_token_passes_without_a_key` |
| 5 `/release-notes`; the hint once | `tests/unit/test_release_notes.py` (parsing, which versions are listed, the pager, the command with the installed version's entries, the next view since the last one seen); `tests/unit/test_first_run.py::test_whats_new_appears_once_after_an_upgrade_and_not_on_the_next_start`; the e2e `…whats_new_appears_only_after_an_upgrade` opens the pager in the real CLI and starts a second time |
| 6 `banner = "off"` | `test_first_run_session.py::test_banner_off_prints_nothing_before_the_composer_except_a_failed_check` and `…with_every_check_passing_prints_nothing_at_all`; the e2e `test_banner_off_prints_nothing_before_the_composer` |
| 7 Suggestions | `tests/unit/test_suggestions.py::test_the_placeholder_names_init_without_agents_md_and_not_with_it`, the resume, attention, tip and Tab tests |

Also covered: the pre-flight check itself (`tests/unit/test_preflight.py`),
`/status`, `config show`, `[ui] banner` and `/resume`
(`tests/unit/test_preflight_surfaces.py`), the first-run facts and the trust
store (`tests/unit/test_first_run.py`), and "Work in <directory>?" end to end
(`tests/unit/test_first_run_session.py`, and the e2e
`test_an_unknown_directory_asks_once_and_remembers_the_answer`).

### Amendments to the design above

- **The header is printed after the first frame, not before it.** The old
  panel was printed before the live block started, so its imports counted
  against the first-frame budget of AGX-02 (385 modules on Python 3.13, which
  the base was already at). The version comes from package metadata, and
  `importlib.metadata` alone loads 17 modules. `TUISession.run` therefore
  prints the header and the notices right after `Workspace.start()` and
  `record_first_frame_modules`, through the console the live block already
  hooks, and the first frame is 374 modules (the budget is unchanged).
  The panel's `rich.panel`, `rich.table`, `rich.rule` and
  `rich.box` imports left the first frame with it.
- **`banner = "full"`.** §2.1 says the mascot appears only on the first run, and
  also defines `full`. `compact` (the default) shows the mascot on the first
  run on a machine only; `full` shows it on every start; `off` prints nothing
  before the composer except a failed pre-flight notice. The mascot is ASCII
  (`( o.o )`), so it draws under every glyph set.
- **What `state.json` holds.** `version` (the last version that started),
  `release_notes_seen` and `tip_index`. The "what's new" hint appears when
  `version` differs from the installed one (an upgrade, or a downgrade), and
  the state is updated at the same start, so the hint appears once.
  `release_notes_seen` moves only when `/release-notes` is opened, so the pager
  lists everything since the last notes the user read, not since the last start.
- **Which sections `/release-notes` lists.** `Unreleased` (work in this build
  that no release has, shown whenever it has content), the installed version,
  and every version between the last one seen and the installed one. A build
  with no section of its own (a source tree reports `0+unknown`) shows the
  newest release. The changelog is the repository's `CHANGELOG.md`, shipped to
  `share/agenthicc/docs` by `[tool.setuptools.data-files]` and found next to the
  docs tree.
- **The pager is a `Dialog` with no options.** In that case `Dialog` now scrolls
  its body with the arrows (`dialog.up`/`dialog.down`); PgUp/PgDn already did.
  Rich draws list bullets as `•`, which is not in the glyph set, so the body is
  rendered with `glyphs().bullet`.
- **The pre-flight check.**
  - Only a missing credential is *blocking*. A missing model, an unwritable
    workspace and a missing tool are warnings.
  - The model check passes when a model is configured or `PROVIDER_DEFAULT_MODELS`
    has one for the provider; the repository holds no per-provider model list to
    check a name against.
  - `rg` is not required (search falls back to the built-in scanner), so its
    absence is an `info` notice. `bwrap` is checked only on Linux in a project
    with `.agenthicc/environment.toml`, the only place isolation is configured.
  - `ANTHROPIC_AUTH_TOKEN` counts as an anthropic credential. A provider with no
    variable in `PROVIDER_API_KEY_ENVVAR` (Ollama) needs none; LiteLLM keeps the
    variable the `/model` table already shows for it.
  - It is a pure function of the configuration, the environment's `PATH` and the
    working directory, in `runners/preflight.py`. `/status` (a `Pre-flight` row)
    and `agenthicc config show` print the same findings.
  - The TUI checks credentials again at every submission, so a key exported
    differently or a `/model` switch unblocks prompts without a restart. The
    refused prompt is put back in the composer, so a long prompt is not lost. The
    gate sits before the queue, so a prompt typed while the runtime loads cannot
    slip past it.
  - The old pre-header `LLM config error` line (a hand-coloured print in the
    session context) is gone; the same text appears as a notice under the header
    when the pre-flight check has no finding of its own.
- **"Work in <directory>?"**
  - A directory is *known* when it is a git repository, has a project
    `.agenthicc/agenthicc.toml`, or has an earlier session recorded for it (the
    current session does not count). Agenthicc creates other files under
    `.agenthicc/` itself, so the directory's existence says nothing.
  - The answer is stored in `~/.agenthicc/trust.json` (`workspaces`), next to the
    extension records, by `ExtensionTrust.remember_workspace`.
  - "Yes, in Plan mode" also starts later sessions in that directory in Plan mode
    (unless an explicit mode other than Safe was chosen). Esc is "not now":
    nothing is stored and the question returns.
  - "Choose another directory…" opens an embedded composer for a path, and ends
    the session with `SessionSwitch("directory", path)`, which
    `_run_tui_session` answers by starting a new session there.
- **`/resume` did not exist.** §2.5 suggests `/resume`, so it is added: it opens
  the agents hub on its History tab, where Enter resumes a saved session (the
  same list `agenthicc sessions list` opens). An *interrupted* session is the
  newest other session of the directory whose log ends after a `user_message` or
  `turn_start` with no `turn_complete` after it.
- **Suggestions.**
  - `/init` is suggested only inside a git repository, and an `AGENTS.md` in any
    directory up to the repository root counts.
  - "2 agents need you — press ←" reads `2 sessions need you · press ←`, the
    unit TUX-02 settled on, and `·` is the glyph set's separator (the em dash is
    not in it).
  - The order is the order of §2.5: `/init`, resume, attention, then a tip.
  - The twenty tips take their keys from the user's key bindings, so a rebound
    key is the one named; a tip whose action is unbound is skipped. Ten name a
    key and ten a slash command, and a test checks every command tip against
    `BUILTIN_COMMANDS`.
  - The tip rotates by one at each start, from the counter in `state.json`
    (deterministic, and it never changes while visible). A stale `/init`
    suggestion is replaced when a command or a turn has created `AGENTS.md`.
  - Tab is the new action `composer.accept_suggestion` (condition
    `suggestion`: an empty prompt whose suggestion is a command, while no turn
    runs); `agents.cycle` is `unless suggestion`, so Tab still switches agents
    otherwise.
- **The dispatcher copied a subset of `CommandContext`.** `CommandDispatcher`
  now builds the handler's context with `dataclasses.replace(ctx, text=…,
  args=…)`, so `/resume` and `/status` can reach the session. The same change is
  on the TUX-05 branch, and its tests are updated the same way.
- **The AC 3 test has no cassette.** There is no provider traffic to record; a
  scripted `MockTransport` asserting zero calls is the stronger check, and it
  would fail without the gate.
- **Tests never touch the developer's state file.** `tests/conftest.py` points
  `agenthicc.user_state.state_path` at a file of each test's own.
- **Token migration is complete.** `_PENDING` in `test_theme_tokens.py` is empty:
  `tui/welcome.py` was its last entry.
- **Screenshots.** §6's screenshots were not produced as images. The end-to-end
  tests read the real CLI's screen through `pyte`, and the unit tests assert the
  rendered rows at fixed widths.

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
