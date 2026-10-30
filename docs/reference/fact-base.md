# Documentation fact base

This page is the **provenance record** for the rest of the documentation. Every
surface claim in `README.md`, `AGENTS.md`, `CLAUDE.md`, `skills/`, `llms.txt`,
`llms-full.txt`, and `docs/` is expected to trace back to a `path:line`
citation recorded here.

Use it two ways:

- **When writing docs** — look the item up here instead of inferring it from
  neighboring prose or from memory.
- **When reviewing docs** — re-run the verification commands in
  [Re-verifying this page](#re-verifying-this-page) and compare the counts.

Everything below was read from the working tree at commit
`077bfbc` (`main`) plus the documentation edits not yet committed on top of it,
not from historical PRDs or release notes.

!!! info "Scope of this page"
    This is a map, not a tutorial. It records *where* something is defined and
    *what it is called*; the subsystem guides explain *how it works*.

---

## Public Python surface

### `agenthicc.kernel`

The kernel is the only package that declares a public export list.

| Fact | Evidence |
|---|---|
| `kernel.__all__` exists and lists **22** symbols | `src/agenthicc/kernel/__init__.py:27` |
| Top-level `agenthicc.__all__` is **`None`** — `src/agenthicc/__init__.py` is a 0-byte file | `src/agenthicc/__init__.py` (size 0) |

The 22 kernel symbols:

```text
AgentInstance  AgentStatus  AppState  Effect  EffectExecutor  EffectType
Event  EventProcessor  Intent  IntentStatus  NodeStatus  NoOpEffectExecutor
PermissionRule  ReducerFn  SecurityPolicy  SystemSettings  Task
ToolRegistration  Workflow  WorkflowNode  restore_from_log  root_reducer
```

!!! warning "Why this matters for `llms_check`"
    `nox -s llms_check` (`noxfile.py:244`) collects
    `agenthicc.__all__` *and* `agenthicc.kernel.__all__`. Because the
    top-level list is `None`, the enforced set is exactly the 22 kernel
    symbols above. `llms-full.txt` currently carries 355 `###` headings, so
    the check passes with wide margin — but do not delete kernel headings.

### Other public packages

These declare `__all__` for their own subpackage; they are not part of the
top-level export contract.

| Surface | Evidence |
|---|---|
| `agenthicc.testing` public API — `SessionCassette`, `CassetteEntry`, `ApprovalEntry`, `MockApprovalService`, `ReplayResult`, `run_headless_replay` | `src/agenthicc/testing/__init__.py:29` |
| `agenthicc.skills.loader` public API — `canonical_skill_name`, `discover_skills`, `discover_skills_with_diagnostics`, … | `src/agenthicc/skills/loader.py:20` |

---

## CLI surface

### Global flags

Defined once in `_add_global_flags` and applied to both the bootstrap parser
and the real parser.

| Flag | Takes a value | Evidence |
|---|---|---|
| `--headless` | — | `src/agenthicc/cli/parser.py:18` |
| `--workflow NAME` | yes | `src/agenthicc/cli/parser.py:23` |
| `--mode MODE` | yes | `src/agenthicc/cli/parser.py:32` |
| `--config PATH` | yes | `src/agenthicc/cli/parser.py:39` |
| `--version` | — | `src/agenthicc/cli/parser.py:44` |
| `--continue` | — | `src/agenthicc/cli/parser.py:46` |
| `--resume ID` | yes | `src/agenthicc/cli/parser.py:52` |
| `--record-cassette [DIR]` | optional | `src/agenthicc/cli/parser.py:58` |
| `--set KEY=VALUE` | repeatable | `src/agenthicc/cli/parser.py:70` |
| `--set-secret KEY=ENV_VAR` | repeatable | `src/agenthicc/cli/parser.py:78` |
| `--dangerously-skip-permissions` | — | `src/agenthicc/cli/parser.py:89` |

Two behaviors worth documenting precisely:

- `--version` and `--help` are answered **before** command discovery, so they
  never load project extensions or durable stores
  (`src/agenthicc/cli/parser.py:100-108`).
- The version string is `agenthicc <version>`, where `<version>` is the
  installed distribution's version from package metadata
  (`package_version()` in `src/agenthicc/cli/parser.py`), so it always matches
  the wheel built from `pyproject.toml`. An uninstalled source tree reports
  `0+unknown`.

### Subcommands

Built-in commands are declared in `src/agenthicc/cli/manifest.py`
(`BUILTIN_COMMANDS`, `BUILTIN_GROUPS`). An invocation imports only the module
of the command it selects; `agenthicc --help` lists every built-in command from
the manifest without importing any of them. Plugin commands in
`~/.agenthicc/cli/` and `.agenthicc/cli/` register with `@command(...)` and
override a built-in command at the same path.

| Top-level | Invocations | Module |
|---|---|---|
| login, logout, whoami | `login`, `logout`, `whoami` | `cli/commands/auth.py` |
| agents | `agents`, `agents list`, `agents status`, `agents cancel`, `agents resume`, `agents retry`, `agents priority`, `agents approve`, `agents reject`, `agents input`, `agents inbox list`, `agents inbox answer`, `agents inbox approve`, `agents inbox reject`, `agents inbox expire`, `agents notifications`, `agents rename`, `agents labels`, `agents purge`, `agents archive`, `agents delete`, `agents restore`, `agents actions`, `agents action` | `cli/commands/background.py`, `cli/commands/worktrees.py` |
| run | `run` | `cli/commands/background.py` |
| config | `config show`, `config validate`, `config profiles`, `config init` | `cli/commands/config.py` |
| init | `init` | `cli/commands/init.py` |
| mcp | `mcp add`, `mcp list`, `mcp get`, `mcp remove`, `mcp connect`, `mcp disconnect`, `mcp refresh`, `mcp doctor`, `mcp auth`, `mcp logout` | `cli/commands/mcp.py` |
| runs | `runs`, `runs list`, `runs show`, `runs cancel`, `runs resume` | `cli/commands/runs.py` |
| attach | `attach` | `cli/commands/runs.py` |
| schedules | `schedules`, `schedules list`, `schedules create`, `schedules show`, `schedules pause`, `schedules resume`, `schedules run-now`, `schedules retry`, `schedules update`, `schedules delete`, `schedules service-status`, `schedules history` | `cli/commands/schedules.py` |
| session | `session create`, `session list`, `session show`, `session export`, `session events`, `session send`, `session control`, `session serve` | `cli/commands/session_service.py` |
| sessions | `sessions list`, `sessions show`, `sessions export`, `sessions inspect` | `cli/commands/sessions.py` |
| skills | `skills add` | `cli/commands/skills.py` |
| trust | `trust list`, `trust add`, `trust revoke` | `cli/commands/trust.py` |
| workflows | `workflows list`, `workflows run` | `cli/commands/workflows.py` |
| worktrees | `worktrees list`, `worktrees show`, `worktrees recover`, `worktrees integrate`, `worktrees dispatch`, `worktrees actions`, `worktrees action`, `worktrees setup`, `worktrees trust` | `cli/commands/worktrees.py` |

!!! note "`agents` is the agents hub and its scriptable form"
    `agenthicc agents [SESSION_ID] [--tab TAB] [--run RUN_ID]` opens the agents
    hub (AGX-05, `src/agenthicc/tui/workspace/agents_hub/`); `agents
    <subcommand>` is the scriptable form, and `agents --json` prints the
    complete projection without a terminal. The former `jobs` group moved under
    `agents` and is no longer a command.

---

## Slash commands

The canonical registry is `BUILTIN_COMMANDS`
(`src/agenthicc/commands/builtins.py:1353`). It contains **35** entries:

| Command | Argument hint | Aliases | Evidence |
|---|---|---|---|
| `/replay` | `[session-id]` | — | `src/agenthicc/commands/builtins.py:1355` |
| `/cancel` | — | `/interrupt` | `src/agenthicc/commands/builtins.py:1362` |
| `/loop` | `[interval] <prompt> \| status \| pause \| resume \| stop` | — | `src/agenthicc/commands/builtins.py:1369` |
| `/loops` | — | — | `src/agenthicc/commands/builtins.py:1378` |
| `/schedules` | — | — | `src/agenthicc/commands/builtins.py:1386` |
| `/schedule` | `create ... \| run-now ID \| pause ID \| resume ID NEXT-RUN \| delete ID --confirm` | — | `src/agenthicc/commands/builtins.py:1394` |
| `/agents` | `[running\|needs-you\|history\|library]` | — | `src/agenthicc/commands/builtins.py:1402` |
| `/inbox` | — | — | `src/agenthicc/commands/builtins.py:1410` |
| `/subagents` | — | — | `src/agenthicc/commands/builtins.py:1417` |
| `/clear` | — | — | `src/agenthicc/commands/builtins.py:1424` |
| `/commands` | `[reload]` | — | `src/agenthicc/commands/builtins.py:1430` |
| `/tools` | `[reload]` | — | `src/agenthicc/commands/builtins.py:1439` |
| `/workflows` | `[runs\|reload]` | — | `src/agenthicc/commands/builtins.py:1448` |
| `/config` | — | — | `src/agenthicc/commands/builtins.py:1457` |
| `/expand` | `[tool-id-or-@path]` | — | `src/agenthicc/commands/builtins.py:1464` |
| `/help` | `[/command]` | — | `src/agenthicc/commands/builtins.py:1471` |
| `/history` | `[repair]` | — | `src/agenthicc/commands/builtins.py:1478` |
| `/init` | `[write] [--force]` | — | `src/agenthicc/commands/builtins.py:1486` |
| `/mcp` | `[status\|reload\|connect NAME\|disconnect NAME\|refresh NAME\|doctor [NAME]]` | — | `src/agenthicc/commands/builtins.py:1493` |
| `/model` | `[provider] [model]` | — | `src/agenthicc/commands/builtins.py:1502` |
| `/models` | — | — | `src/agenthicc/commands/builtins.py:1510` |
| `/skills` | `[reload]` | — | `src/agenthicc/commands/builtins.py:1516` |
| `/status` | — | — | `src/agenthicc/commands/builtins.py:1524` |
| `/startup` | — | — | `src/agenthicc/commands/builtins.py:1530` |
| `/keybindings` | — | — | `src/agenthicc/commands/builtins.py:1536` |
| `/perf` | — | — | `src/agenthicc/commands/builtins.py:1542` |
| `/ps` | `[terminal-id] [--json]` | `/processes` | `src/agenthicc/commands/builtins.py:1548` |
| `/background` | `(alias: /bg)` | `/bg` | `src/agenthicc/commands/builtins.py:1556` |
| `/detach` | — | — | `src/agenthicc/commands/builtins.py:1563` |
| `/workers` | `[--json]` | — | `src/agenthicc/commands/builtins.py:1568` |
| `/stop` | `[terminal-id\|all] [--force]` | `/stop-terminal` | `src/agenthicc/commands/builtins.py:1575` |
| `/usage` | — | — | `src/agenthicc/commands/builtins.py:1583` |
| `/mode` | `[Safe\|Plan\|Yolo]` | — | `src/agenthicc/commands/builtins.py:1590` |
| `/workflow` | `<name> \| resume [run-id] \| reset [run-id]` | — | `src/agenthicc/commands/builtins.py:1597` |
| `/compact` | — | — | `src/agenthicc/commands/builtins.py:1607` |

!!! note "Counting the registry"
    Count the `BUILTIN_COMMANDS` list literal rather than grepping for command
    names: `/model` and `/models` are separate entries, and the file also
    contains command names inside handler strings and printed messages that are
    not registry entries.

`/workflow` and `/compact` deliberately carry `handler=None`; they are
intercepted in `TUISession.route()` before dispatch so they can reach
session-local state, and exist in the registry purely so the trigger picker
can display and complete them (`src/agenthicc/commands/builtins.py:1597-1606`, `1607-1614`; routing at
`src/agenthicc/runners/tui_session.py:3492,3494`).

### Commands intercepted before dispatch

These are easy to miss and have caused documentation errors, so they are
recorded explicitly. All of them are `BUILTIN_COMMANDS` entries, so the picker,
`/help` and completion see them, but `TUISession` handles them itself:

| Command | Handled by | Evidence |
|---|---|---|
| `/workflow`, `/compact` | `TUISession.route()` (session memory and workflow state) | `src/agenthicc/runners/tui_session.py:3492,3494` |
| `/background` (alias `/bg`), `/detach` | `TUISession.dispatch_slash()` (the background handoff, `_BACKGROUND_HANDOFF_COMMANDS`) | `src/agenthicc/runners/tui_session.py:536,2685` |

### Bootstrap skills that provide slash commands

These are skills, loaded from the skill bootstrap table, that expose a
command-shaped entry point rather than entries in `BUILTIN_COMMANDS`:

| Command | Evidence |
|---|---|
| `/create-tools <instructions>` | `src/agenthicc/skills/bootstrap.py:222` |
| `/create-commands <instructions>` | `src/agenthicc/skills/bootstrap.py:269` |

---

## Workflows

Declared in the lazy builtin table, each entry as
`(name, module, class, aliases)`:

| Workflow | Module | Evidence |
|---|---|---|
| `code_plan` (alias `Plan`) | `agenthicc.workflows.code_plan.definition` | `src/agenthicc/workflows/loader.py:47` |
| `copy_website` | `agenthicc.workflows.copy_website` | `loader.py:50` |
| `create_workflow` | `agenthicc.workflows.create_workflow.definition` | `loader.py:53` |
| `goal_flow` | `agenthicc.workflows.goal_flow.runner` | `loader.py:58` |
| `make_agenthicc_tool` | `agenthicc.workflows.make_agenthicc_tool.runner` | `loader.py:61` |
| `make_book` | `agenthicc.workflows.make_book.runner` | `loader.py:66` |
| `reconstruct_site` | `agenthicc.workflows.reconstruct_site` | `loader.py:71` |
| `site_imitate` | `agenthicc.workflows.site_imitate.runner` | `loader.py:74` |

`code_plan` declares its name in code as well, at
`src/agenthicc/workflows/code_plan/definition.py:56`, and `create_workflow` at
`src/agenthicc/workflows/create_workflow/definition.py:77`.

!!! note "The README workflow table now matches the registry"
    The README table (`README.md:45-52`) lists all eight registered workflows,
    so registry and README agree on the built-in set. The defect recorded
    earlier — a five-row table against an eight-entry registry — is closed.
    The feature-summary row at `README.md:32` names five of the eight as
    examples.

Registry mechanics live in `src/agenthicc/workflows/registry.py`
(`register`, `register_lazy`, `register_alias` at lines 28, 53, 94).

---

## Modes

| Mode | Aliases | Evidence |
|---|---|---|
| `Safe` | `Guard`, `Ask` | `src/agenthicc/modes/builtin.py:62`; aliases at `:58` |
| `Plan` | `Review` | `src/agenthicc/modes/builtin.py:73`; alias at `:58` |
| `Yolo` | `Auto` | `src/agenthicc/modes/builtin.py:84`; alias at `:58` |

`ModeManager` reports `"Safe"` when no mode is active
(`src/agenthicc/modes/manager.py:41,64-65`).

---

## Agent roles

`BUILTIN_AGENT_DEFINITIONS` (`src/agenthicc/agents/builtin.py:89`) declares
seven roles:

| Role | Evidence |
|---|---|
| `planner` | `src/agenthicc/agents/builtin.py:91` |
| `executor` | `src/agenthicc/agents/builtin.py:96` |
| `reviewer` | `src/agenthicc/agents/builtin.py:101` |
| `explorer` | `src/agenthicc/agents/builtin.py:106` |
| `verifier` | `src/agenthicc/agents/builtin.py:111` |
| `human` | `src/agenthicc/agents/builtin.py:116` |
| `auto` | `src/agenthicc/agents/builtin.py:121` |

---

## Configuration sections

Top-level `AgenthiccConfig` fields map one-to-one onto TOML sections
(`src/agenthicc/config.py:1566`):

| TOML section | Field | Evidence |
|---|---|---|
| `[execution]` | `execution` | `config.py:1567` |
| `[providers]` | `providers` | `config.py:1568` |
| `[behaviour]` | `behaviour` | `config.py:1569` |
| `[tools]` | `tools` | `config.py:1570` |
| `[security]` | `security` | `config.py:1571` |
| `[plugins]` | `plugins` | `config.py:1572` |
| `[skills]` | `skills` | `config.py:1573` |
| `[agents]` | `agents` | `config.py:1574` |
| `[workflows.<name>]` | `workflows` | `config.py:1575` |
| `[loops]` | `loops` | `config.py:1580` |

Two further tables are read outside these fields: `[memory.context_windows]`
fills `execution.context_windows`, and `[background]` is read by
`src/agenthicc/background/settings.py`. The `[api]`, `[hooks]`, `[memory]` (other
than `context_windows`) and `[storage]` sections were removed by PRD-250 S-03
because nothing read them.

!!! danger "`[behaviour]` is spelled with a British `-our`"
    The section key is `behaviour` in code (`config.py:1569`). This is a
    **config key**, not prose, so it must stay byte-accurate in every example,
    table, and migration note. The same rule applies to the `/compact`
    description string "Summarise conversation history…"
    (`src/agenthicc/commands/builtins.py:1060`).

### Field inventory per section

| Section | Fields | Evidence |
|---|---|---|
| `ExecutionSettings` (31) | `max_parallel_tasks`, `max_agent_turns`, `authoring_max_generation_attempts`, `authoring_max_phase_turns`, `max_output_tokens`, `turn_timeout_s`, `auto_compact`, `context_windows`, `prompt_cache`, `file_cache`, `transport_max_retries`, `transport_retry_base_delay_s`, `transport_retry_max_total_s`, `llm_sdk_max_retries`, `profile`, `provider`, `model`, `api_key`, `api_key_env`, `base_url`, `default_headers`, `default_query`, `client_options`, `request_options`, `timeout_s`, `temperature`, `top_p`, `max_completion_tokens`, `_resolved_profile`, `session_header`, `irrecoverable_error_max_retries` | `config.py:1039` |
| `ToolSettings` (11) | `mcp_servers`, `plugins`, `allowed`, `denied`, `max_live_tool_calls`, `group_exploratory_calls`, `http_timeout_s`, `question_timeout_s`, `cloakbrowser`, `playwright`, `browser_backend` | `config.py:1355` |
| `SecuritySettings` (2) | `allowed_paths`, `network_allow_list` | `config.py:1393` |
| `PluginSettings` (1) | `strict_cli_shadow` | `config.py:1399` |
| `BehaviourSettings` (6) | `resume_transcript_turns`, `resume_transcript_max_events`, `resume_transcript_max_bytes`, `resume_transcript_max_event_bytes`, `composer_max_rows`, `frame_interval_ms` | `config.py:1410` |
| `UISettings` (3) | `mouse`, `screen_reader`, `reduced_motion` | `config.py` (`[ui]`) |
| `CloakBrowserSettings` (15) | `enabled`, `transport`, `cdp_endpoint`, `allowed_domains`, `headless`, `navigation_timeout_s`, `action_timeout_s`, `max_pages`, `max_actions_per_turn`, `max_snapshot_chars`, `max_screenshot_bytes`, `allow_persistent_profiles`, `profile_root`, `license_key_env`, `allow_all_domains` | `config.py:1197` |
| `PlaywrightSettings` (16) | `enabled`, `transport`, `browser_type`, `browser_channel`, `executable_path`, `allowed_domains`, `headless`, `navigation_timeout_s`, `action_timeout_s`, `max_pages`, `max_actions_per_turn`, `max_snapshot_chars`, `max_screenshot_bytes`, `allow_persistent_profiles`, `profile_root`, `allow_all_domains` | `config.py:1288` |
| `SkillsSettings` (2) | `install_default_skills`, `default_skill_directory` | `config.py:1558` |

`ExecutionSettings` also contains the naming inversion recorded in
`docs/reference/repository-state.md`: `timeout_s` is the LLM timeout
(`_DEFAULT_LLM_TIMEOUT_S: float = 3_600.0`, `config.py:117`) while
`turn_timeout_s` governs a turn.

---

## Skills schema

The skill loader is the authority on `SKILL.md` frontmatter. Documenting
skills against anything else produces files that silently lose their metadata.

| Rule | Constraint | Evidence |
|---|---|---|
| Allowed frontmatter fields | `name`, `description`, `author`, `tags`, `suggestedTopics`, `disallowAutoTriggering`, `tools`, `disabledTools`, `maxTurnDepth`, `model`, `aliases`, `allowedAgents`, `deniedAgents`, `permissions`, `source`, `version` | `src/agenthicc/skills/loader.py:31-50` |
| Field aliases accepted | e.g. `suggested_topics`/`topics`, `alias`, `allowed_agents`/`allowAgents`/`allow_agents` | `loader.py:51-59` |
| Canonical name length limit | 64 | `loader.py:28` |
| Description length limit | 1 536 | `loader.py:29` |
| Canonical name pattern | `^[a-z0-9]+(?:-[a-z0-9]+)*$` (kebab-case) | `loader.py:30` |
| Unknown fields | reported as `unknown-frontmatter-field` | `loader.py:372` |
| Missing `name` | reported as `missing-name`; the directory name is used instead | `loader.py:384` |
| Missing `description` | reported as `missing-description` | `loader.py:401` |
| Missing frontmatter | reported as `missing-frontmatter`, falls back to legacy metadata | `loader.py:333` |
| Directory name that is not already canonical | reported as `legacy-directory-name`; the directory name stays a compatibility alias | `loader.py:307-312` |
| A non-directory in the skills root | reported as `ignored-entry` | observed on `skills/README.md` |
| Discovery roots | project `<project>/.agenthicc/skills`, user `~/.agenthicc/skills`; project scope wins | `loader.py:587-588`, `loader.py:670` |

!!! success "Every `SKILL.md` in `skills/` now satisfies this schema"
    All ten skills under `skills/` declare a canonical `name` and a real
    `description`, so the loader reports **one** diagnostic — the non-directory
    entry `skills/README.md`:

    ```text
    skills/README.md: skill root entry is not a directory (ignored-entry)
    ```

    Earlier revisions used `skill:` and `summary:` frontmatter. Neither is a
    known field, so the loader emitted `unknown-frontmatter-field`, then
    `missing-name` and `missing-description`, and fell back to the directory
    name with an empty description — 16 diagnostics across five skills. That
    was a documentation defect, not a code defect: the loader behaved as
    designed. The skills were rewritten against the source; see the
    [stale reference register](#stale-reference-register).

---

## Documentation surfaces

### What the documentation tools publish

| Surface | Evidence |
|---|---|
| Extra root-level documents served to agents: `llms.txt`, `llms-full.txt`, `README.md` | `src/agenthicc/tools/introspect/__init__.py:66` |
| Served suffixes: `.md`, `.txt`, `.json` | `introspect/__init__.py:69` |
| Discovery marker for a real docs tree: `index.md` | `introspect/__init__.py:61` |
| Environment override for the docs root: `AGENTHICC_DOCS_DIR` | `introspect/__init__.py:58` |
| Read page size cap: 2 000 lines | `introspect/__init__.py:72` |
| Search result cap: 200 | `introspect/__init__.py:73` |
| Source read cap: 120 000 bytes | `introspect/__init__.py:74` |

Because these three root files are part of the agent-facing surface, their
contents are held to the same accuracy bar as `docs/`.

### Site configuration

`mkdocs.yml` drives the published site. `mkdocs build --strict` exits 0 with
zero warnings and zero errors, and its `nav` lists **54** entries for the
**54** pages under `docs/`: every page is reachable from the nav, and no nav
entry points at a missing file.

Earlier revisions of this page recorded four orphaned pages —
`docs/guides/exploratory-tool-calls.md`, `docs/guides/startup.md`,
`docs/guides/usage-accounting.md`, and `docs/reference/usage-ledger.md` — that
were reachable only by direct link (and, for `startup.md`, from the README).
All four are now in the nav.

---

## Stale reference register

Symbols and modules that are **verified absent**. The API package directory
`src/agenthicc/api/` does not exist on disk, and the TUI symbols below were
removed when the TUI moved to a Rich Live workspace. None is importable, and — because `skills/` was rewritten
against the source — no skill names any of them. Every surviving mention warns
the reader that the thing is gone.

| Symbol / module | Status | Where it is named now |
|---|---|---|
| `agenthicc.api.server` | **absent** — no `src/agenthicc/api/` package | `docs/reference/api.md` states the absence |
| `create_app` | **absent** — no ASGI app | `docs/reference/api.md` states the absence |
| FastAPI / REST / WebSocket server | **absent** — no ASGI app, no API extra | `docs/reference/api.md` states the absence |
| `agenthicc.tui.app` | **absent** — no `tui/app.py` | `docs/index.md:10` warns it is historical |
| `TranscriptModel` | **absent** — no `tui/transcript.py` | `docs/tui-architecture.md`, `docs/guides/tui.md:4` |
| `render_frame_ansi` | **absent** — the prompt-toolkit renderer is gone | `docs/guides/testing.md:82` |
| `agenthicc.tui.transcript` | **absent** — no `tui/transcript.py` | `CLAUDE.md:31`, `llms.txt:296` |
| `prompt_toolkit` / `build_app` | **absent** — the TUI is Rich Live based | `docs/guides/tui.md:4` |

This table previously pointed at `SKILL.md` files under `skills/`, because those
skills asserted the absent symbols as current. The skills were rewritten in the
same campaign that produced this page, so those rows described a state that no
longer exists. Re-check with:

```bash
# expect no matches: these four symbols are absent from every skill
grep -rln 'create_app\|TranscriptModel\|render_frame_ansi\|prompt_toolkit' skills/
```

### Correctly historical references — do **not** "fix" these

The following mentions are accurate: each one explicitly tells the reader that
the thing does not exist. Removing them would delete the warning.

| Reference | Why it is correct |
|---|---|
| `docs/reference/api.md` | States there is no `agenthicc.api` package and no REST/WebSocket implementation |
| `docs/tui-architecture.md` | Compatibility pointer explaining the old `TranscriptModel` and `render_frame_ansi` modules are gone |
| `docs/guides/tui.md:4` | Notes the prompt-toolkit `build_app()`/`TranscriptModel` design is historical |
| `docs/guides/testing.md:82` | States the removed `render_frame_ansi`/`pyte` contract is not current |
| `docs/index.md:10` | Warns about the absent API and historical TUI implementation |
| `CLAUDE.md:31` | Records that `tui.transcript` is absent |
| `llms.txt:296` | Names `tui.transcript`/`tui.events` as historical |

The asymmetry this table documents — `docs/`, `README.md`, `CLAUDE.md`, and
`llms.txt` warned about the absence while `skills/` asserted presence — was the
single largest accuracy defect in the documentation set. It is now closed: the
skills were rewritten against the source, and no skill names any symbol in the
register above.

### Claims that look stale but are verified real

Checked specifically because they are counter-intuitive. Do not delete these.

| Claim | Verification |
|---|---|
| `/create-tools` and `/create-commands` exist | Provided as bootstrap skills, `src/agenthicc/skills/bootstrap.py:222,269` |
| `/bg`, `/background` and `/detach` exist | Registered in `src/agenthicc/commands/builtins.py` and intercepted by `TUISession.dispatch_slash` |
| A TUI command picker sees `/workflow` and `/compact` | Both are registry entries with `handler=None`, intercepted in `TUISession.route()` |
| `name_that_ui.py` exists | Present at `src/agenthicc/workflows/name_that_ui.py` — older notes said it was missing |
| `EventBusTestHarness` exists | A pytest fixture defined at `tests/conftest.py:89`, used to drive the kernel event bus from tests — it is *not* part of the `agenthicc.testing` public API, and an earlier register row wrongly called it absent |
| README anchor `#goal_flow-adding-work-discovered-during-implementation` resolves | Heading `### \`goal_flow\`: adding work discovered during implementation`, `docs/guides/workflows.md:338` |
| README anchor `#make_book-phase-handoffs` resolves | Heading `#### \`make_book\` phase handoffs`, `docs/guides/workflows.md:259` |
| README anchor `#pause-crash-recovery-and-workflow-resume` resolves | Heading `### Pause, crash recovery, and \`/workflow resume\``, `docs/guides/workflows.md:41` |

**Correction to an earlier note.** The three README anchor rows above were
previously labelled "fragile" in planning notes. That label was wrong: all
three resolve, and they are recorded here as verified-real precisely so they
are not "fixed" into breakage. The genuine fragility was different — README.md
lives *outside* the MkDocs `docs/` directory, so `mkdocs build --strict` never
validated it and no gate covered its 53 local links at all. That gap is now
closed by the README checker, which resolves every relative link and every
`path#anchor` against the real MkDocs slug function (see
`docs/reference/verification-baseline.md`).

---

## Stale trees: scope every search before trusting it

The working directory contains generated copies of the source that are **not**
the source of truth. They are gitignored, so `git status` stays clean and
nothing warns you, but `grep -r`, `find`, and `rglob` will happily return them.

| Path | What it is | Dated |
|---|---|---|
| `build/lib/agenthicc/` | A frozen copy of `src/agenthicc/` from an older build | `build/lib/agenthicc/commands/builtins.py` is 922 lines vs **1072** in `src/` |
| `dist/` | Built distributions | gitignored |
| `site/` | Rendered MkDocs output | gitignored |
| `.venv/` | Installed packages, including an installed `agenthicc` | gitignored |

All four are gitignored: `.gitignore:11` (`build/`), `:13` (`dist/`), `:155`
(`.venv`), and `:227` (`site/`).

!!! danger "This bit already"
    Drafting this page, a helper that walked the tree resolved the bare
    filename `builtins.py` to `build/lib/agenthicc/commands/builtins.py`
    (922 lines) rather than the real file (1072 lines) and reported **18
    valid line citations as out of range**. The citations were correct; the
    resolver was reading stale code.

    When gathering facts, name paths explicitly (`src/agenthicc/...`) or
    exclude `build/`, `dist/`, `site/`, and `.venv/`. Never infer a fact from
    `build/`. If a count disagrees with this page, check which file you are
    reading before concluding the page is wrong.

---

## Re-verifying this page

Run these from the repository root. Each is read-only.

```bash
# 1. Kernel export surface (expect 22 symbols; top-level __all__ is None)
PYTHONPATH=src python -c "
import agenthicc, agenthicc.kernel
print('top-level __all__:', getattr(agenthicc, '__all__', None))
print('kernel symbols:', len(agenthicc.kernel.__all__))"

# 2. The API package must stay absent
test ! -e src/agenthicc/api && echo 'src/agenthicc/api absent: OK'

# 3. Slash-command registry (expect 23 entries)
python -c "
import ast, pathlib
tree = ast.parse(pathlib.Path('src/agenthicc/commands/builtins.py').read_text())
for node in ast.walk(tree):
    if isinstance(node, ast.AnnAssign) and getattr(node.target, 'id', None) == 'BUILTIN_COMMANDS':
        print('BUILTIN_COMMANDS entries:', len(node.value.elts))"

# 4. CLI subcommands (built-in manifest)
python -c "from agenthicc.cli.manifest import BUILTIN_COMMANDS; print(*(' '.join(r.path) for r in BUILTIN_COMMANDS), sep='\n')"

# 5. Workflow table
sed -n '45,80p' src/agenthicc/workflows/loader.py

# 6. Skill frontmatter schema
sed -n '31,50p' src/agenthicc/skills/loader.py

# 7. Config sections
sed -n '1491,1503p' src/agenthicc/config.py

# 8. llms-full.txt heading count (expect 355)
grep -c '^### ' llms-full.txt
```

To confirm the frontmatter finding end to end, run the loader against the
repository's own skills directory and inspect the diagnostics:

```bash
PYTHONPATH=src python -c "
from pathlib import Path
from agenthicc.skills.loader import discover_skills_with_diagnostics

result = discover_skills_with_diagnostics(project_dir=Path('.'), user_dir=Path('/nonexistent'))
print('skills found:', len(result.skills))
for slug, skill in result.skills.items():
    print(f'  {slug:24} name={skill.name!r} description={skill.description!r}')
print('diagnostics:', len(result.diagnostics))
for diagnostic in result.diagnostics:
    print('  DIAG', diagnostic)
"
```

Both arguments must be `Path` objects: the loader joins them with `/`, so
passing strings raises `TypeError`. `skills` is a `dict[str, SkillDef]` keyed by
slug (`src/agenthicc/skills/loader.py:156`), so iterate `.items()` when you want
the validated definition rather than the key.

On an unmodified checkout this prints ten skills, each with a real
`description`, and one diagnostic (`skills/README.md`, `ignored-entry`).

## Related

- [Current repository state](repository-state.md) — evidence-backed boundaries and known risks
- [CLI reference](cli.md) — flags and subcommands in detail
- [Kernel reference](kernel.md) — events, reducers, and persistence
- [Storage reference](storage.md) — files, paths, and retention
