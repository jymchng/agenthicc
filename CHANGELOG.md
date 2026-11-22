# Changelog

All notable changes to `agenthicc` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Added — long subagent output goes to a file

- **A cut-off reply is saved.** When a worker's reply stops at the output-token
  limit, the partial text is written to
  `.agenthicc/subagent-output/<pool id>/<task id>-attempt<N>.md` under the parent
  session's workspace root (never in a worker's git worktree). The failure names
  the path and `SubagentResult.output_file` carries it. The worker still fails: an
  incomplete reply is not a result. The directory is self-ignoring (`.gitignore`
  with `*`), saving is skipped if the path would leave the workspace, and a failed
  save never changes the failure.
- **Workers are told to use a file for long output.** A worker with a write tool
  gets a `## LONG OUTPUT` section naming its reply limit: content too long for one
  reply is written to a file, checked, and returned by path, even when the task asks
  for full or verbatim content.

### Removed — subagents have no limits

A subagent pool died after ten minutes, and eight of its nine workers ran out of
turns or budget, because pools and worker types capped what a worker could do.
Subagents now run until they finish.

- **No turn limit.** `SubagentTypeSpec.max_turns` is gone, so no built-in or plugin
  type is capped (`explorer` 15, `planner` 10, `reviewer` 10, `verifier` 12 and
  `researcher` 8 were), and the session's `max_agent_turns` no longer applies to
  workers. A worker's reply is still bounded by the output-token limit and ends as
  `output_truncated`.
- **No pool budget.** `subagents.max_tool_calls`, `subagents.max_iterations`,
  `subagents.max_total_time_s`, `subagents.max_budget`, `subagents.on_budget_exceeded`
  and the `budget` argument of `spawn_subagents` are removed. A config that still
  sets one is refused at load and names the key. `SubagentPool` takes none of them,
  `budget_exceeded()` and the `max_*_exceeded` stop reasons are gone, and a pool is
  stopped only on purpose.
- **Usage is still reported.** `subagents/budget.py` is now `subagents/usage.py`
  and keeps only `PoolUsage`, `pool_usage`, `worker_usage` and `worker_run_id`. The
  spawn result and `SubagentPoolCompleted` carry `usage`; `budget` and
  `budget_exceeded_reason` are gone, and the panel no longer shows a cost budget.
- **No `stopped` pool status.** Nothing writes `subagent_pool_stopped` any more, so
  journal folding, background notices and `collect_subagent_results` report only
  `completed`, `cancelled` and `interrupted`.
- Still bounded on purpose: `max_workers`, `max_concurrent`, `max_tasks`,
  `max_active_pools`, `max_depth`, and the per-call `timeout_s` (default one hour).

### Changed — `make_book` chapters speak to the reader as "you"

- **One shared voice.** The new `agenthicc.workflows.make_book.voice` module owns the
  prose style for the whole workflow. The `chapter` phase and the preface written by
  `front_matter` now follow the full guide: the reader is addressed directly as "you"
  and "your" in every section (technical ones included), conjunctions, transitions and
  conversational connectives are used far more densely, sentences are compound and
  complex rather than runs of short declaratives, and each paragraph picks up the
  previous one's thought. The chapter agent revises its own file against the guide
  before it confirms the chapter.
- **Guardrails against padding.** Every added clause must carry an idea; connectives
  must state a true relationship; hedges are kept for points the research leaves open;
  "Furthermore", "Moreover", "Additionally" and "In fact" are capped at once or twice
  per chapter. Headings, code, equations, tables, captions and citations are outside
  the voice.
- **Editing phases keep the voice.** `layout_review`, `final_layout_review` and
  `compile` carry a prose-preservation guard so a layout or build fix cannot condense
  a paragraph or remove its second person.
- **Instructions that fought the voice are gone.** The chapter prompt no longer bans
  "Furthermore"/"Moreover"/"Additionally" and "any formulaic transition", asks for
  "dense, precise prose" and "short, scannable sections", or addresses a "specialist
  colleague"; its two requirements numbered 6 are now 6 and 7. The declarative
  `PhaseSpec` skeleton carries the same shared text. The workflow's phases, tools,
  transition gates and checkpoint format are unchanged.

### Changed — a blank line before each heading in the agents hub

- Each group heading in the hub list (`Running`, `Queued`, …) after the first is now
  preceded by a blank line, so a group no longer runs into the one above it; the
  first heading stays directly under the tab bar. The blank line is a row of its own,
  which keeps scrolling and the selection in view correct with several headings on
  screen. It cannot be selected or clicked, and screen readers and the plain
  `agenthicc agents` listing do not get it.

### Changed — `/background` says what is happening

- **While it waits.** When `/background`, `/bg` or `/detach` is typed while a tool
  call runs, a blinking blue `●` under the call reads
  `Backgrounding after tool call ends…` (it replaces the status-line notice). The
  dot holds still under `reduced_motion` and for screen readers, who are told once.
- **Once it has happened.** The transcript ends with
  `● Successfully backgrounded · 6b9f7614-8ca (running)` instead of `✗ cancelled`.
  Handing a session over cancels the foreground turn, which used to be reported as
  a cancelled (and, in a workflow, paused) run. That turn now closes as ordinary
  work, with no error row, no `Interrupted after …` row and no `Workflow paused`
  notice; the workflow run is recorded exactly as before, so the background worker
  resumes from the same checkpoint. The confirmation comes after the turn's own
  closing rows.

### Fixed — the window title and taskbar progress no longer blank the screen

- **Control sequences bypass the live block's text proxy.** While the live block
  runs, `sys.stdout` is a Rich proxy that prints what it is given as text and
  strips control characters, including the BEL that ends a title, progress or
  notification sequence. The terminal title was therefore written unterminated,
  and a terminal or emulator reads everything after an open `OSC 2` as part of
  the title: the screen went blank. Title, progress and notification sequences
  now go to the stream underneath (`terminal_stream()`), on POSIX and Windows.

### Added — motion, feedback and terminal integration (TUX-10)

- **A status line that says what is happening.** The state segment reads
  `Reading src/app.py`, `Running npm test`, `Editing …`, `Working with 2 agents`,
  `Retrying (2/5) in 8s` or `◆ Waiting for you · approve a command` instead of an
  internal tool name, with the elapsed time after 3 s and `↓ 2.1k tokens`
  after 10 s. The turning flower and the bold letter over `Thinking` stay, and
  `reduced_motion` shows two identical frames half a second apart.
- **Turn-end rows that say what the turn did.** `✾ Worked for 1m 12s | 10:20 AM`
  gains `└ 6 tools · edited 3 files +34 −8`; an interrupted turn reads
  `✾ Interrupted after 22s` with `nothing was changed` when no write succeeded,
  a failed one `✾ Stopped after 8s`, and a quick answer has no row.
- **The terminal knows.** When it is unfocused, one notification (OSC 9, OSC 777
  or the bell, `[ui] notify`) when a request over 10 s ends, fails or needs an
  approval; the window title (`✿ fix flaky test · agenthicc`, restored on exit,
  `[ui] window_title`); and OSC 9;4 taskbar progress in Windows Terminal, ConEmu
  and Ghostty, determinate only when a compaction reports measured progress.
- **Clickable paths.** Paths in tool rows, diff headers, notices and the
  transcript view, and URLs in the assistant's text, are OSC 8 links
  (`[ui] hyperlinks`, `editor_links = "vscode" | "cursor" | "idea" | "zed"`).
- **`/shortcuts` and `?`.** One page of every key in every context, generated
  from the key map, saying which are yours (`composer.shortcuts`).

### Changed

- The background attention router no longer sets the window title; the title
  adds `(2)` when other sessions need you. `_emit_terminal_notification` is
  replaced by `tui.notifications.NotificationSink`.

### Added — navigation (TUX-09)

- **A transcript view.** `Ctrl+T` (also while a turn runs), `/history`, `/logs`
  and `/rewind` open one full-screen view over the session log, entered from
  the composer so the prompt is exactly as it was when `q` returns. It draws
  every event with the scrollback's own renderers, numbers the user turns,
  jumps with `[`/`]` (turns) and `{`/`}` (failures), expands a tool call to its
  stored output with Enter, searches (`/`, `n`, `N`; case-insensitive unless
  the query has capitals; on a worker thread, so a 100 MB log never stalls the
  keys), copies an item with `y` (OSC 52, or the system clipboard where the
  terminal cannot take it) and opens it in `$PAGER` with `e`. A decision
  dialog's `Ctrl+T` opens its full arguments or diff here, and `Esc` returns to
  the dialog. Text is wrapped, never cut with an ellipsis, and a long pasted
  prompt expands with Enter.
- **A fullscreen layout.** `[ui] layout = "fullscreen"` or `/fullscreen` runs
  the session in the alternate screen: a scrolling transcript pane over the
  docked composer and status line. PgUp/PgDn, Shift+↑/↓ and the wheel scroll
  it; scrolling up shows `↓ N new · End to follow`. Leaving prints the
  conversation to the normal screen in compact form. `screen_reader` forces
  `inline`.
- **`/rewind`.** Pick a user turn, then "Edit and resend from here" forks the
  conversation (the log and the model's memory up to that turn) into a new
  session with the prompt in the composer. The original session is never
  changed, and files are not restored.

### Removed — navigation (TUX-09)

- `TranscriptHistoryOverlay` and the `/logs` pager (`LogTailOverlay`,
  `diagnostics.log_tail`): the transcript view replaces both. The dialog's own
  scrolling details body (`Dialog.showing_details`) is gone too; `Ctrl+T`
  opens the transcript view. `Ctrl+T` can no longer be a keybindings `leader`.

### Changed — selections have no background

- **A selected row is the accent bar and bold text.** The answer form and the
  `ask_user` options, the agents hub list, dialogs, the `/` `@` and Ctrl+P
  pickers, `/help`, `/history`, the registry lists, the subagent panel and the
  theme picker no longer draw a background behind the selected row. Reverse video
  is gone from the `/config` menu and the hub's confirm choices too (the focused
  row is bold in the accent colour). The `accent_bg` theme token is removed.

### Changed — a message typed during a turn reaches the model during it

- **Enter now sends into the running turn.** A message typed while a turn runs
  waited for the turn to end, which for a long turn or a workflow run meant it
  sat unread for minutes or hours. Now the running turn takes it at its next
  tool call, one message per call in the order typed (the queue row says
  `at next tool`), and if the turn ends first the rest is sent as the next turn.
  This holds for a direct reply and for every workflow. Commands and skills
  (`/…`, `$…`) are routed locally, so they still wait for the end of the turn.
- **One key fewer.** `Alt+Enter` / `Ctrl+Enter` used to be "interject" while a
  turn ran; Enter does that now, so they are a line break in every state and the
  `composer.interject` action, its footer hint, `SendMessageCommand.interject`
  and `QueuedMessage.interject` are gone (`claim_interjection` is
  `claim_message`).
- **One blank line, never two.** The scrollback prints a blank line only when the
  previous output was not one, so a block's trailing blank, the gap owed before
  the next block and the blank an event with nothing to show opened can no longer
  stack up after `✾ Worked for …`.
- **A real terminal's size wins over a stale `COLUMNS` / `LINES`.** Rich takes
  those variables over the terminal's own answer, so a shell that had exported
  them made the TUI wrap for one width and draw at another (cut lines, a live
  block repainted over itself). On a terminal they are now ignored. `/status`
  gained a `Terminal` row (the size the terminal reports, `TERM`, tmux or screen,
  and a `$COLUMNS` that disagrees) to compare with output that looks cut off.

### Fixed — the agents hub no longer cuts text off

- **Answer form.** A long option (`A — One screen, interleaved (recommended): the
  shell stays l…`) was cut at the panel's edge. Options now wrap under their own
  text with the highlight on every row, and a short terminal scrolls the options
  around the cursor (`↑ 3 more`, `↓ 2 more`) instead of pushing the hint and
  footer off screen.
- **Transcript.** The hub's transcript showed the first line of each reply, cut
  at 240 characters, and tool calls the same way. A reply now keeps its
  paragraphs (up to 8,000 characters) and tool calls their summary and first
  output line; each entry wraps under its role and an entry beyond 40 lines ends
  in `… N more lines`.
- **Requests and details.** The command in an approval request was flattened to
  one line and cut at 240 characters. It now keeps its lines, wraps, and the
  detail view shows it whole (up to 4,000 characters). Errors, the latest
  activity, the receipt, change stats, attempts, the status notice, the empty
  state and the help screen's descriptions wrap the same way. A capped block says
  how many lines it left out; nothing is cut without a marker.

### Changed — compaction in the transcript

- **A quieter compaction indicator.** While the conversation is compacted, a
  row under the transcript reads `◜ Refreshing context · 2s`: a ring that turns
  once a second in the theme's accent colour and a time that moves every
  second. It replaces the `Compacting` word in the status line and the
  `⎋ Compacting conversation…` line in the scrollback. The ring rests as `◌`
  under `reduced_motion` and `screen_reader`. When the compaction ends the row
  gives way to `✓ Context refreshed · 849 tokens · 3s`, or to
  `✗ Context refresh failed · 3s` with the reason underneath. A thin bar
  appears under the row only for a history long enough to be summarised in
  several model calls, and follows the calls that have really finished; a
  compaction that is one call shows the spinner alone. The `compaction_status`
  event gained `elapsed_s` and `label`; the compactor no longer appends
  `system` text events, and `/compact` no longer repeats the outcome as a
  status-line notice.

### Changed — agents experience (AGX series)

- **CLI start-up (AGX-01).** Built-in commands are declared in
  `src/agenthicc/cli/manifest.py`; an invocation imports only the command it
  runs instead of every command module, and help and "unknown command" errors
  import none. `agenthicc agents --json` loads 169 modules instead of 457,
  `agenthicc --help` 111 instead of 211. The `kernel` and `background` packages
  resolve their public names lazily, MCP server configuration moved to
  `agenthicc.tools.mcp_server_config` so reading configuration no longer
  imports the MCP client, and `--version` reads package metadata only when it
  is requested. `/background`, `/bg` and `/detach` are now ordinary built-in
  commands intercepted by `TUISession`, instead of being patched in when the
  background CLI module was imported. A project CLI plugin that shadows a user
  plugin now actually produces the shadowing warning.
- **Interactive start-up (AGX-02).** The TUI draws its first frame without
  lauren-ai, the OS keyring or Rich's emoji table: 363 modules instead of 665,
  and about 60% less CPU before the prompt accepts input. The provider config,
  runner and the conversation-journal fold now run in an `agent_runtime`
  phase on worker threads after the first frame; a message sent while it loads
  is queued with a "loading agent runtime" notice and runs once it is ready,
  and workflow and interrupted-turn recovery wait for it. Whether to show
  sponsored messages is decided off the event loop from a cached account tier
  (`~/.agenthicc/account-tier.json`, owner-only, no token material, fresh for a
  day), so an ordinary start never touches the keyring; `AuthClient.get_token()`
  also reads the keyring on a worker thread. Memory layers, the background
  inbox and the workspace file cache open their SQLite databases on first use;
  the agents hub prepares its inbox on a worker thread as it opens.
  `/startup` prints process start → first frame, first frame → agent runtime
  ready and the module count at the first frame. Extension discovery no longer
  fingerprints every session journal under `~/.agenthicc`, and the dead
  `agenthicc.cli.auth` module is gone.
- **Session index for the agents screen (AGX-03).** The background registry is
  read through a derived SQLite index, `~/.agenthicc/background/index.sqlite3`,
  instead of folding `events.jsonl` into memory: a fresh process shows its
  first page in milliseconds at 10,000 sessions, and status counts, filters and
  search are indexed queries. Search now also matches intent, labels and latest
  activity. The first open imports the old `projection-v1.json` snapshot and
  deletes it; when the index must be rebuilt from the log, the screen shows
  progress and recent sessions, and leaving it cancels the rebuild. The
  scheduler's capacity and queue checks read only active sessions. The
  `background.manager.projection_batch_size` setting is gone; a configuration
  that still sets it loads and the value is ignored.
- **Event-driven agents screen (AGX-04).** The agents screen no longer
  refreshes four times a second or runs maintenance on a five-second timer: it
  wakes when `events.jsonl`, the input inbox or the selected session's journal
  changes (inotify on Linux, kqueue on macOS/BSD, a stat fallback elsewhere)
  or when a worker process exits (`pidfd_open` on Linux), so an idle screen at
  10,000 sessions uses about 0.03% of a core, and a change made by another
  process appears within 200 ms (p95). Maintenance probes a worker only when it
  is first seen, exits, misses its heartbeat deadline or cannot be watched,
  reads only input-inbox files that changed, and dispatches the queue only when
  queued work exists and the registry changed; with nothing due it probes and
  writes nothing. `updated` events now carry only the changed fields (a
  single-field update is about 170 bytes instead of 1.7 KB), and existing
  full-record logs replay unchanged. Fixed along the way: flagging a stale
  heartbeat no longer counts as activity, so the flag no longer clears itself
  on the next pass; the Windows worker probe no longer calls `os.kill(pid, 0)`,
  which sends CTRL_C_EVENT there; and `background.stale_after_s` is now
  applied — every supervisor is built with `BackgroundSupervisor.from_settings()`,
  which also gives worker, parallel-plan and worktree-tool supervisors the
  configured cancel grace and wall timeout. `background.manager.refresh_interval_s`
  is now the stat interval used without native change notification, and
  `maintenance_interval_s` the probe interval for workers whose exit cannot be
  watched.
- **The agents hub (AGX-05, AGX-06, AGX-07).** One screen now shows every
  agent: `agenthicc agents` opens it full screen, and `/agents`, Ctrl+G or ← on
  an empty prompt open it over the conversation. It replaces the background
  manager, the `agents --run` goal-run screen, the `sessions list` selector and
  the `/inbox` overlay with four tabs — Running (active, queued, pinned and
  recently finished agents, goal runs with their workers nested), Needs you
  (pending approvals, questions and lost workers, oldest first), History
  (finished agents and saved sessions, newest first, read in windows as you
  scroll) and Library. Rows lead with the status in one vocabulary shared with
  `agenthicc agents list` (`◆ needs approval`, `⚠ worker lost`, `queued #3` …),
  drop columns rather than squeezing them as the terminal narrows, keep the
  selection on the same agent as the list changes, mark agents whose state
  changed since you last looked, and can be searched with words plus
  `status:`, `project:`, `agent:`, `label:` and `run:`. Space opens a peek
  panel with the exact pending request; Enter opens the detail view: the
  request card (`y`/`n` resolve exactly that inbox item), progress, a live
  redacted transcript tail, a worktree agent's diff summary, its attempts, and
  only the fields that have a value. `+` starts a new background agent.
  Attaching from inside a session switches to that agent and returns to your
  session when it ends; `/background` now hands the conversation off and opens
  the hub with the new agent selected. Workers record each agent's report
  line — the first sentence of its latest message, redacted — and its tokens
  and cost after each completed provider call, shown in the list and the
  detail header. The `jobs` command group moved under `agents`
  (`agents list`, `agents status`, `agents cancel`, …) and `jobs` is gone;
  `sessions list` on a terminal opens the History tab. `BackgroundManager`,
  `GoalRunManager`, `SessionManager` and `BackgroundInboxOverlay` are removed,
  `BackgroundManagerService` is now `AgentsService`, `BackgroundManagerSettings`
  is `AgentsHubSettings` (still configured under `[background.manager]`), and
  the store reads `BackgroundWindow`s with `query_window`, `select` and
  `count` instead of `BackgroundPage`s. The export redactor now also masks
  secret assignments such as `token=…`, `API_KEY: …` and `password = "…"`.
- **Subagent panel (AGX-08).** While a turn's subagents run, a panel under the
  prompt shows each worker's status, what it is doing now (its current tool and
  target, the approval it waits for or its latest message), how long it has run
  and its tokens; finished rows fold away after 30 seconds and failures stay
  until dismissed. Ctrl+O or `/subagents` focuses the panel, Enter opens a
  worker's transcript (←/→ for its siblings), and `x` stops one worker while
  the others and the turn go on. Transcripts are kept per worker under the
  session's `subagents/` directory, redacted and bounded to 2 MiB, and stay
  available after the turn and across resumes; the parent agent never reads
  them. A worker's approval prompt now names it, `o` opens its transcript and
  Esc denies only that call; background sessions record the requester in the
  inbox. In a session, the agents hub's Running tab lists the turn's workers.
  Fixed along the way: an answer to a worker's clarification question that
  arrived before the worker looked for it was lost, so the worker finished
  without it; and the pool's progress updates never reached signal
  subscribers, because it re-published the same mutated object. Backgrounding
  a running fan-out (the PRD's phase 2) is not implemented: the PRD gates it on
  its own replay-safety review.
- **Key bindings and accessibility (AGX-13).** Keys resolve to named actions
  from one registry, which also produces the footer hints, so hints can no
  longer advertise keys that do nothing (the old `R Retry`, `Esc Dismiss` and
  `Ctrl+L Clear` hints did). Bindings can be changed in
  `~/.agenthicc/keybindings.toml` and apply at once; `/keybindings` shows the
  effective bindings and `agenthicc keybindings edit` opens the file. The key
  decoder now knows Ctrl+A–Z, Alt combinations, modified arrows, Home and End,
  Delete, Insert, F1–F12, focus and mouse reports on POSIX and Windows;
  Ctrl+letters, Alt combinations and unknown escape sequences no longer arrive
  as Esc (which could pause a running turn). A new `[ui]` section adds
  `screen_reader` (no spinners or box drawing, state changes printed as
  sentences), `reduced_motion` (a static `●` instead of animation) and `mouse`
  (`auto`, `on`, `off`). The agents hub's keys use the same registry, in the
  `agents`, `agents.list`, `agents.peek` and `agents.detail` contexts, which
  inherit one another's actions so an operation such as stop works wherever an
  agent is selected; its footer hints and `?` help come from it.
- **TUI render pipeline (AGX-11).** The live block is no longer rebuilt and
  rewritten whole on every change. A frame scheduler merges redraw requests
  into at most one frame per `[behaviour] frame_interval_ms` (16 ms) while
  typing still echoes at once, and the spinner runs at 10 Hz instead of 20.
  Status and footer rows are rendered once per distinct row, and a new
  line-diffing writer rewrites only the rows that changed: a spinner frame is
  one row of about 50 bytes, and an idle TUI draws nothing. The prompt folds
  each line at the terminal width (continuation rows are indented under `❯`),
  re-folds only the line being edited, and shows at most
  `[behaviour] composer_max_rows` rows (12) around the cursor, with
  `↑ N more lines` / `↓ N more lines` for the rest — typing in a 10,000-character
  or 200-line prompt costs about 1 ms of CPU per frame instead of up to 83 ms.
  On terminals that report support for synchronized output (mode 2026), every
  frame is written atomically. `/perf` shows frame counts and render and
  write cost.
- **Agent-turn bookkeeping (AGX-12).** The session memory no longer copies the
  conversation on every provider step: `ensure_valid_and_persist()` returns
  before copying when the tool history is valid, and rollback snapshots keep
  message references instead of deep copies. On the 510-step `goal_flow`
  replay, full-history copies fall from 2,245 to 35 and validation scans from
  2,935 to 1,771, with byte-identical provider requests and journal records.

### Changed — the composer frame is yellow (TUX-01/TUX-02)

- New theme token `frame` draws the composer's frame: both rules, or the rounded
  box's corners and side edges. It is yellow, matching the welcome panel's
  border (`#8a6d00` in the light theme, where yellow would be unreadable on
  white; `#ffe066` in high-contrast). The title, mode badge and hint row keep
  their own tokens, and every other rule and border stays `subtle`.
- The `>` chevron of the prompt is `frame` too, in bold — in the composer and in
  the `/` and `@` pickers that repeat the prompt above their matches. It was
  `accent` before, so the panel's outline and its chevron now share one colour
  instead of two.

### Changed — the empty composer's placeholder is dimmed (TUX-02)

- The hint after the caret, `ask, or type / for commands, @ for files` (and its
  narrower forms), now renders in the `muted` token **plus** the `dim`
  attribute instead of `muted` alone, so an empty prompt reads as a hint rather
  than as content. Because `dim` is an attribute and not a colour, it still
  distinguishes the placeholder under `--theme no-color`; every colour remains a
  theme token (TUX-01).

### Changed — the start-up header, a pre-flight check and first-run help (TUX-03)

- The welcome panel is replaced by a three-line header: `◆ agenthicc 0.42.0`
  (with `what's new: /release-notes` after an upgrade), the model, provider and
  mode, and the directory with its git branch. The first run on a machine also
  shows the mascot once. `[ui] banner = "compact" | "full" | "off"` chooses how
  much of it prints. The panel's `No list` placeholder, its `Welcome back!`
  greeting and its fixed `/init` tip are gone.
- **Release notes are local.** The remote `changelog.json` fetch, its cache and
  `AGENTHICC_CHANGELOG_CACHE` are deleted. `/release-notes` pages the
  `CHANGELOG.md` that ships with the package: the installed version, everything
  since the last version you read, and unreleased work.
- **A pre-flight check** (`agenthicc.runners.preflight.check_session`) looks for
  a credential, a model, a writable workspace and the tools agenthicc uses,
  without a network call. A failed check prints one notice under the header
  (`No API key for anthropic. Set ANTHROPIC_API_KEY, or run /model to pick
  another provider.`); a prompt submitted while the credential check fails is
  not sent: the notice shows again, the prompt stays in the composer and
  `/model` runs. `/status` and `agenthicc config show` print the same findings.
- **First run in a directory.** A directory that is not a git repository and has
  no history with agenthicc, or the home directory, asks "Work in ~/Downloads?"
  once (Yes, Yes in Plan mode, Choose another directory…). The answer is stored
  per directory in `~/.agenthicc/trust.json`.
- **Composer suggestions.** The empty composer suggests `/init` when the
  repository has no `AGENTS.md`, `/resume` after an interrupted session,
  `N sessions need you · press ←`, or one of twenty tips that name a key from
  your key bindings or a command. Tab accepts a suggestion that is a command.
  `/resume` is new: it opens the agents hub's History tab.
- The command dispatcher passes every `CommandContext` field to a handler
  instead of a copied subset, so `/resume` and `/status` can reach the session.

### Changed — background questions have a five-minute deadline by default

- `[background] question_timeout_s` now defaults to `300` seconds instead of
  `0`. A hub/background agent's `ask_user` no longer waits indefinitely: after
  five minutes the agent continues with its safest stated assumption and the
  question stays in **Needs you** as `◆ answer late`, where a late answer still
  reaches it as the session's next message. Set
  `[background] question_timeout_s = 0` to restore the old unbounded wait. The
  foreground `[tools] question_timeout_s` is unchanged and still governs only
  the Questions overlay.

### Changed — required quality gates (PRD-250 WS-1)

- One CI workflow, `.github/workflows/ci.yml`, runs every gate — lint and
  format, mypy, the type ratchet, `llms_check`, a strict docs build, unit and
  integration tests on Python 3.11–3.13, E2E and performance tests, a Windows
  job for the lock, filesystem and terminal code, and branch-and-subprocess
  coverage — and ends in one aggregate **Required checks** job.
  `scripts/apply_branch_protection.sh` makes that check and code-owner review
  required on `main`; it is not applied automatically.
- Releases re-run that CI for the tagged commit, require the tag to name the
  package version, and check that the built wheel's `agenthicc --version`
  reports it. `agenthicc --version` now reads the installed distribution's
  version instead of a literal.
- `mypy src/agenthicc` is clean. Six bundled workflow runners that subclass
  `CodePlanRunner` carry a module-level `override` suppression with an owner
  (PRD-250 WS-4.2) and an expiry date that `type_audit` enforces.
- `scripts/type_audit.py` holds the baseline in both directions — a stale
  baseline fails until it is lowered with `--update` — rejects a pull request
  that raises a baseline value (`--baseline-ref`), and counts module-level
  `# mypy:` suppressions. The baseline was lowered to the measured debt, and
  `getattr` calls fell from 716 to 500 through typed access.
- Nox sessions run on the uv backend, so each tool runs at its declared
  version inside the session's virtualenv; previously a session could run a
  copy that happened to be on `PATH`, or none at all on a clean runner. The two
  `dev` dependency sets are one `dev` extra that also declares `twine` and the
  book/screenshot test dependency; core/extra duplicates were removed.
- Coverage measures branches and CLI/worker subprocesses, and the floor is
  `[tool.coverage.report] fail_under`. Pre-commit hooks run the project's own
  ruff, mypy, type ratchet and unit tests. `.github/CODEOWNERS` owns the
  quality baselines and gate definitions.
- Tests that start a fresh interpreter — a CLI command or a detached worker —
  take their wall-clock ceiling from `tests/cli_process.py`, and each test's
  `pytest-timeout` covers all of its waits. The 15- and 30-second ceilings they
  replace expired under traced coverage on a loaded host, and some longer
  ceilings could never apply under the 60-second default. The coverage session
  gives a test without its own mark three times that default, because tracing
  roughly triples CPU time.

### Fixed — verified defects (PRD-250 D-01–D-10)

- Project-memory SQLite connections are closed; fire-and-forget tasks are owned
  by a `SupervisedTasks` supervisor that logs failures and drains on shutdown;
  the kernel processor keeps no unbounded event list (use `add_sink`), rejects
  undeclared events with a published `KernelEventRejected`, counts dropped
  notifications, writes its log off the event loop, drains with
  `Queue.join()`, and stops after the events already emitted (cancelling
  `run()` now always stops it, including on Python 3.11); every store uses one
  fail-closed `InterProcessLock`; plugin
  dependencies are read statically so each plugin imports once; reactive
  subscriber failures are logged with the signal name and `Computed.dispose()`
  releases subscriptions; bugbear and async findings are fixed and the `B`,
  `ASYNC`, `RUF006` and `RUF100` rules are enabled.
- A workflow checkpoint now restores nested `design_tokens` losslessly, and the
  base reconstruct-site runner declares the re-entry hooks it calls.

### Security — trust gaps (PRD-250 S-01–S-03)

- Skill `` !`command` `` placeholders are never executed when a skill loads; the
  agent is asked to run each command through its command tool, where approvals,
  the mode ceiling and the sandbox apply.
- Every Python extension loader — tools, commands, workflows, modes, agents and
  CLI plugins — imports a project file only when `~/.agenthicc/trust.json`
  holds a record of its current SHA-256. Manage trust with `agenthicc trust
  list`, `add` and `revoke`. The old CLI-only trust manifest and audit file are
  removed.
- Settings that nothing read were deleted instead of left inert, including the
  `[api]`, `[hooks]` and `[storage]` sections, `[memory]`'s storage keys,
  `security.sandbox_mode` and most `[plugins]` keys; a test asserts every
  remaining field has a reader. The undeclared `execution.base_system_prompt`
  and mention-budget reads were removed.

### Added — hermetic cassette replay

- `run_headless_replay(..., hermetic=True)` replaces side-effecting built-in
  tools with schema-identical stubs inside the tool registry
  (`override_builtin_tools`, `hermetic_builtin_groups`) and owns a durable
  workflow handle, so a recorded `goal_flow` campaign replays as a regression
  test without running any recorded command.

### Added — session-scoped recurring `/loop` prompts (PRD-193)

- Added the interactive `/loop [interval] <prompt-or-registered-command>`
  scheduler with idle-safe dispatch, one pending iteration while busy, and no
  concurrent turns.
- Added `/loop status`, `/loop pause`, `/loop resume`, and `/loop stop`, with
  finite lifetime, bounded failure backoff, replacement, and explicit resume
  semantics after process restart.
- Persisted loop definitions under each session's `loop.json` with atomic
  owner-only writes; scheduled turns reuse the normal conversation,
  checkpoint, approval, permission, and security paths.
- Documented configuration, command syntax, session storage, troubleshooting,
  headless behavior, and operational limits across the README and MkDocs usage
  and reference pages.
- Added `/loops`, a table-based schedule-job manager with immediate run-now,
  owner-safe deletion, keyboard navigation, and delete confirmation.

### Changed — queued input during tool execution

- Ordinary messages queued while the agent is running are now delivered after
  the current tool batch completes, before the next model request. Queued slash
  commands and skills retain FIFO local routing when the run becomes idle.

### Documentation — repository map and improvement roadmap

- Rewrote the README, contributor/agent guidance, and MkDocs landing page to
  match the current Rich workspace, headless stdin runner, kernel, workflows,
  tools, and persistence layers.
- Added configuration, security, extensions, testing, CLI, storage, and
  contributor documentation, plus PRD-138 covering the remaining repository
  improvements and implementation/documentation drift.
- Marked the historical REST API and prompt-toolkit TUI references as planned
  or compatibility material until their source packages exist again.
- Added an evidence-backed current repository-state reference, refreshed
  PRD-138's baseline, reconciled the hook/executor adapter documentation, and
  added orphaned guides and references to the MkDocs navigation.

### Added — Configurable session memory budget

- `[execution] session_memory_max_tokens` (default 32000) sets the token budget of
  the session conversation memory (`ShortTermMemory`).  Settable from
  `agenthicc.toml` and the CLI (`--set execution.session_memory_max_tokens=N`), and
  honoured by both the interactive session and the workflow runners.

### Added — Conversation durability & resumable execution (PRD-129)

- **Phase 1 — idempotent tools.** A turn-scoped `IdempotencyLedger` (lauren-ai)
  replays a tool result across a transport-retry rollback instead of re-executing
  it, so side-effecting tools (`write_file`, `run_bash`, `git_commit`) apply once.
  Replay is rollback-scoped (a `promote`/`committed`/`pending` model), so a
  legitimate repeat call within one attempt still runs live; replayed results are
  rekeyed to the current `tool_use_id`.
- **Fixed** a provider 400 (`tool_result ... must have a corresponding tool_use
  block`) introduced by the first-cut ledger: it deduped a repeat `(name,input)`
  call within a single attempt and replayed a stale `tool_use_id`.
- **Phase 2 — durable journal.** `ShortTermMemory` is now a projection of an
  append-only, `fsync`-ed `ConversationJournal`; every transition is durable, so a
  crash mid-turn no longer loses the in-flight turn, and `--resume` folds the
  journal back into memory. Removed the superseded SQLite `conversation_store.py`.
- **Phase 3 — resumable execution.** Turn-lifecycle markers + a
  `DurableIdempotencyLedger` + a `RunCoordinator` detect a turn interrupted by a
  hard crash and re-drive it from where it left off, replaying already-completed
  tools. Defers to the PRD-94 workflow resume when a workflow run is in progress.

### Removed — Kernel runtime trio (PRD-128)

- Deleted the unwired `agenthicc.runtime` package — `CommunicationTools`, `AgentPool`,
  `AgentRecord`, and `Scheduler` (`runtime/comm_tools.py`, `runtime/pool.py`,
  `runtime/scheduler.py`). This was the original PRD-01/PRD-03 kernel agent-execution
  layer, superseded by lauren-ai's `AgentRunnerBase` and the `agenthicc.workflows`
  runners. It had zero production consumers: nothing live emitted the events it
  produced, every live `EventProcessor` uses `NoOpEffectExecutor` (so its effects were
  discarded), and `AppState.agents` / `AppState.tasks` were read by no live code. Only
  its own unit/integration tests instantiated it.
- Removed the five tests that exercised the trio: `test_agent_pool.py`,
  `test_comm_tools.py`, `test_scheduler.py`, `test_mcp_connect.py`, and
  `test_runtime_cycle.py`.
- Documentation sync: removed all references to the trio (and to the already-removed
  PRD-116 `agenthicc.workflow` package that lingered alongside it) from CLAUDE.md,
  AGENTS.md, CONTRIBUTING.md, README.md, `llms.txt`, `llms-full.txt`, the `docs/`
  site (deleted `reference/communication-tools.md` and `guides/agents.md`), and the
  shipped reference skills (deleted `skills/writing-agents`; cleaned
  `skills/testing-agenthicc` and `skills/extending-with-hooks`).
- **Deferred to a follow-up (Phase 2):** pruning the now-orphaned kernel reducer
  branches (`_agent_spawn_request`, `_task_created`, `_task_assigned`,
  `_workflow_node_added/removed`, `_agent_status_changed`), the
  `EffectType.spawn_agent` / `assign_task` / `start_workflow_node` members, the
  `AppState.agents` / `AppState.tasks` fields + `AgentInstance` / `Task` dataclasses,
  and the now-inert `agent_pool_size` / `max_parallel_tasks` config fields.

---

## [0.1.0] — 2025-01-01

### Added — Kernel

- `AppState` immutable dataclass — single source of truth for all runtime state
- `EventProcessor` — multi-producer single-consumer asyncio queue; applies pure reducers
  sequentially; append-only JSON-lines event log; periodic snapshot; subscriber notification
- `root_reducer` — pure `(AppState, Event) → (AppState, list[Effect])` with handlers for
  12 event types: `IntentCreated`, `IntentStatusChanged`, `AgentSpawnRequest`,
  `AgentStatusChanged`, `WorkflowCreated`, `WorkflowNodeAdded`, `WorkflowNodeRemoved`,
  `WorkflowNodeStatusChanged`, `TaskCreated`, `TaskAssigned`, `ToolRegistered`,
  `HookRegistered`
- `restore_from_log()` — replays the JSON-lines event log for crash recovery;
  tolerates corrupt trailing lines from mid-write crashes
- Copy-on-write `AppState.with_*()` helpers for zero-mutation state updates
- `Effect` / `EffectType` descriptors for side-effectful actions

### Added — Workflow Engine

- `find_ready_nodes()` — O(n) scan returning all dispatchable pending nodes
- `detect_cycle()` — 3-colour iterative DFS cycle check; used before every node addition
- `topological_sort()` — Kahn's algorithm; raises `CycleError` on cycles
- `DAGExecutor` — concurrent node dispatch via `asyncio.Semaphore`, double-dispatch
  prevention, auto-skip of nodes whose dependencies failed, `run_workflow()` drives
  to terminal state via subscriber-based event loop
- `IntentParser` — regex/heuristic goal extraction (polite-prefix stripping, entity
  extraction, deadline/priority constraint detection)
- `IntentValidator` — capacity check against `max_concurrent_intents`
- `StaticPlanner` — JSON task-array → `NodeSpec` list with single-node fallback
- `LlmPlanner` — runner-injected planner with JSON parse + fallback
- `WorkflowModifier` — atomic node add/remove with DAG integrity guarantees

### Added — Agent Runtime

- `AgentPool` — `asyncio.Queue`-based idle/busy pool with `acquire()` / `release()`
- `Scheduler` — assigns oldest pending task to idle agent via kernel events
- `CommunicationTools` — 9 async methods covering all PRD inter-agent communication:
  `agent_spawn`, `agent_send_message`, `task_create`, `task_assign`,
  `workflow_modify` (with inline cycle guard), `application_log`, `application_ui_update`,
  `tool_define`, `hook_register` — every method emits kernel events, never mutates state

### Added — Tool Execution Layer

- `Tool` ABC with `execute(args, ctx) → Any`
- `ToolResultEnvelope` dataclass — unified ok/error result with duration_ms
- `LifecycleHook` ABC — `on_before → Rejection`, `on_after`, `on_error → RecoveryAction`
- `RecoveryAction` enum: `RETRY`, `FALLBACK`, `ESCALATE`, `SKIP`
- `HookRegistry` and `HookRunner` — parallel hook gather via `asyncio.gather`
- `LaurenToolHookAdapter` — bridges agenthicc hooks into lauren-ai `ToolHook` protocol
- `load_hook_from_dotpath()` — importlib-based dynamic hook loading
- `AgenthiccToolExecutor` — permission → before-hooks → `asyncio.wait_for` execution →
  after-hooks → error-hooks (RETRY honored once) → ToolCallStarted/Complete events
- `execute_parallel()` — fan-out up to `tool_call_budget`, extras get budget_exceeded
- `WorkspaceView` — `os.path.realpath`-based path-prefix sandbox; blocks traversal,
  symlink escape, and absolute escapes
- `NetworkGuard` — domain allow-list with subdomain matching

### Added — Memory Architecture

- `SessionMemoryLayer` — in-process LRU+TTL cache (CPython-safe lock-free reads,
  serialised writes via `asyncio.Lock`)
- `ProjectMemoryLayer` — SQLite key-value store (namespaced) + content-addressed artifact
  table (sha256 hex artifact IDs)
- `GlobalMemoryLayer` — user-wide SQLite at `~/.agenthicc/global.db`
- `MemoryRouter` — tier routing with permission checker; `read`, `write`,
  `publish_artifact`, `read_artifact`
- `SemanticIndex` — cosine-similarity bag-of-words vector search (zero extra deps)

### Added — TUI

- `TranscriptModel` — pure Python rendering model: labeled agent turn blocks,
  tool call entries with `ToolCallState` (pending → running → success/failure),
  Braille spinner frames, cost/token footer
- `diff_lines()` — `difflib.SequenceMatcher`-based line diff (keep/add/remove)
- `TUIEventAdapter` — maps kernel events to `TranscriptModel` mutations;
  `subscribe_to()` + `sync()` for incremental replay; `consume(queue)` for live feed
- `render_frame_ansi()` — positions input bar unconditionally on the last terminal row
  (`rows`); menu overlay anchored above status line; never displaces input bar
- `build_app()` — prompt_toolkit `Application` with `HSplit` layout; slash-command
  detection; `ConditionalContainer` menu overlay; headless JSON-lines fallback
- `INPUT_PROMPT = "> "` — consistent input prefix across TUI and headless

### Added — Configuration & Security

- `load_config()` — deep-merge TOML from `agenthicc.toml` + `~/.agenthicc.toml`;
  user config overrides project config; graceful missing-file handling
- `AgenthiccConfig` — typed dataclass hierarchy: `ExecutionSettings`, `MemorySettings`,
  `SecuritySettings`, `ApiSettings`, `ToolSettings`
- `PermissionChecker` — fnmatch pattern matching against `SecurityPolicy.permission_rules`;
  fail-closed default; path-prefix and network-domain condition matching

### Added — Headless API

- `create_app()` — FastAPI application with lifespan-managed `EventProcessor`
- `POST /v1/intents` — submit intent text; returns `intent_id`
- `GET /v1/intents/{intent_id}` — poll status; 404 on unknown
- `GET /v1/state/summary` — agent/workflow/intent counts + last event type
- `WebSocket /v1/ws` — streams state summary JSON on every kernel event
- API key authentication via `Authorization: Bearer` header

### Added — lauren-ai Integration

- Signal bridge pattern: `SignalBus` handlers convert `lauren_ai` lifecycle signals
  (`ModelCallStarted`, `ToolCallStarted`, `ToolCallComplete`, `AgentRunComplete`)
  into agenthicc kernel events
- `_build_runner_for_agent()` from `lauren_ai.testing` used to resolve tools and
  connect runners in tests
- Three E2E tests using real `AgentRunnerBase` + `MockTransport` against the kernel

### Added — Tests

- 276 tests across 26 files (15 unit, 7 integration, 4 e2e)
- Hypothesis property-based testing for reducer purity and determinism
- pyte vt100 emulator TUI tests verifying input bar position on last row
- FastAPI `TestClient` + WebSocket integration tests
- Full Argon2 refactor E2E scenario: 3 parallel agents, test failure, debugger recovery

---

[Unreleased]: https://github.com/agenthicc/agenthicc/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/agenthicc/agenthicc/releases/tag/v0.1.0
