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
`332987a` (`main`), not from historical PRDs or release notes.

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
    symbols above. `llms-full.txt` currently carries 206 `###` headings, so
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
- The version string is the literal `agenthicc 0.1.0`
  (`src/agenthicc/cli/parser.py:44`), matching `pyproject.toml:7`.

### Subcommands

Registered by the `@command(...)` decorator. The group names in the help
epilog (`src/agenthicc/cli/parser.py:160-162`) are:
`agents`, `auth`, `config`, `jobs`, `mcp`, `sessions`, `skills`, `tools`,
`trust`, `workflows`.

| Group | Invocations | Evidence |
|---|---|---|
| auth | `auth login`, `auth logout`, `auth whoami` | `src/agenthicc/cli/commands/auth.py:11,21,30` |
| background | `agents`, `jobs`, `run` | `src/agenthicc/cli/commands/background.py:117,124,131` |
| jobs | `list`, `status`, `cancel`, `resume`, `retry`, `approve`, `reject`, `input`, `rename`, `labels`, `purge`, `archive`, `delete`, `restore` | `src/agenthicc/cli/commands/background.py:162-328` |
| config | `config show`, `config validate`, `config profiles`, `config init` | `src/agenthicc/cli/commands/config.py:41,62,82,108` |
| init | `init` | `src/agenthicc/cli/commands/init.py:12` |
| mcp | `add`, `list`, `get`, `remove`, `connect`, `disconnect`, `refresh`, `doctor`, `auth`, `logout` | `src/agenthicc/cli/commands/mcp.py:20-252` |
| session | `create`, `list`, `show`, `export`, `events`, `send`, `control`, `serve` | `src/agenthicc/cli/commands/session_service.py:35-205` |
| sessions | `list`, `show`, `export`, `inspect` | `src/agenthicc/cli/commands/sessions.py:23,74,92,108` |
| skills | `skills add` | `src/agenthicc/cli/commands/skills.py:13` |
| trust | `trust cli` | `src/agenthicc/cli/commands/trust.py:18` |
| workflows | `workflows list`, `workflows run` | `src/agenthicc/cli/commands/workflows.py:71,98` |

!!! note "`agents` and `jobs` are the same manager"
    Both are registered as top-level entry points to the background-session
    manager (`src/agenthicc/cli/commands/background.py:117,124`), and
    `jobs <subcommand>` is the scriptable form.

---

## Slash commands

The canonical registry is `BUILTIN_COMMANDS`
(`src/agenthicc/commands/builtins.py:892`). It contains **23** entries:

| Command | Argument hint | Aliases | Evidence |
|---|---|---|---|
| `/replay` | `[session-id]` | — | `src/agenthicc/commands/builtins.py:893` |
| `/cancel` | — | `/interrupt` | `src/agenthicc/commands/builtins.py:900` |
| `/clear` | — | — | `src/agenthicc/commands/builtins.py:907` |
| `/commands` | `[reload]` | — | `src/agenthicc/commands/builtins.py:913` |
| `/tools` | `[reload]` | — | `src/agenthicc/commands/builtins.py:922` |
| `/workflows` | `[runs\|reload]` | — | `src/agenthicc/commands/builtins.py:931` |
| `/config` | — | — | `src/agenthicc/commands/builtins.py:940` |
| `/expand` | `[tool-id-or-@path]` | — | `src/agenthicc/commands/builtins.py:947` |
| `/help` | `[/command]` | — | `src/agenthicc/commands/builtins.py:954` |
| `/history` | — | — | `src/agenthicc/commands/builtins.py:961` |
| `/init` | `[write] [--force]` | — | `src/agenthicc/commands/builtins.py:967` |
| `/mcp` | `[status\|reload\|connect NAME\|disconnect NAME\|refresh NAME\|doctor [NAME]]` | — | `src/agenthicc/commands/builtins.py:974` |
| `/model` | `[provider] [model]` | — | `src/agenthicc/commands/builtins.py:983` |
| `/models` | — | — | `src/agenthicc/commands/builtins.py:991` |
| `/skills` | `[reload]` | — | `src/agenthicc/commands/builtins.py:997` |
| `/status` | — | — | `src/agenthicc/commands/builtins.py:1005` |
| `/startup` | — | — | `src/agenthicc/commands/builtins.py:1011` |
| `/ps` | `[terminal-id] [--json]` | `/processes` | `src/agenthicc/commands/builtins.py:1017` |
| `/stop` | `[terminal-id\|all] [--force]` | `/stop-terminal` | `src/agenthicc/commands/builtins.py:1025` |
| `/usage` | — | — | `src/agenthicc/commands/builtins.py:1033` |
| `/mode` | `[Safe\|Plan\|Yolo]` | — | `src/agenthicc/commands/builtins.py:1040` |
| `/workflow` | `<name> \| resume [run-id] \| reset [run-id]` | — | `src/agenthicc/commands/builtins.py:1047` |
| `/compact` | — | — | `src/agenthicc/commands/builtins.py:1057` |

!!! note "Counting the registry"
    Count the `BUILTIN_COMMANDS` list literal rather than grepping for command
    names: `/model` and `/models` are separate entries, and the file also
    contains command names inside handler strings and printed messages that are
    not registry entries.

`/workflow` and `/compact` deliberately carry `handler=None`; they are
intercepted in `TUISession.route()` before dispatch so they can reach
session-local state, and exist in the registry purely so the trigger picker
can display and complete them (`src/agenthicc/commands/builtins.py:1047-1056`, `1057-1064`; routing at
`src/agenthicc/runners/tui_session.py:2679,2681`).

### Commands registered outside `BUILTIN_COMMANDS`

These are easy to miss and have caused documentation errors, so they are
recorded explicitly.

| Command | Registered by | Evidence |
|---|---|---|
| `/background` (alias `/bg`) | injected by the background integration, guarded against double registration | `src/agenthicc/background/integration.py:117-121` |

`/background` is *not* in `BUILTIN_COMMANDS`; that is why the constant alone
under-reports the command surface.

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

!!! warning "The README workflow table is incomplete"
    `README.md` lists only five workflows (`code_plan`, `create_workflow`,
    `copy_website`, `reconstruct_site`, `site_imitate`). The registry declares
    eight. `goal_flow`, `make_book`, and `make_agenthicc_tool` are missing from
    that table even though the README documents `goal_flow` and `make_book` in
    detail further down the page.

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
(`src/agenthicc/config.py:1490`):

| TOML section | Field | Evidence |
|---|---|---|
| `[execution]` | `execution` | `config.py:1491` |
| `[providers]` | `providers` | `config.py:1492` |
| `[behaviour]` | `behaviour` | `config.py:1493` |
| `[hooks]` | `hooks` | `config.py:1494` |
| `[tools]` | `tools` | `config.py:1495` |
| `[memory]` | `memory` | `config.py:1496` |
| `[security]` | `security` | `config.py:1497` |
| `[api]` | `api` | `config.py:1498` |
| `[plugins]` | `plugins` | `config.py:1499` |
| `[skills]` | `skills` | `config.py:1500` |
| `[agents]` | `agents` | `config.py:1501` |
| `[storage]` | `storage` | `config.py:1502` |
| `[workflows.<name>]` | `workflows` | `config.py:1503` |

!!! danger "`[behaviour]` is spelled with a British `-our`"
    The section key is `behaviour` in code (`config.py:1493`). This is a
    **config key**, not prose, so it must stay byte-accurate in every example,
    table, and migration note. The same rule applies to the `/compact`
    description string "Summarise conversation history…"
    (`src/agenthicc/commands/builtins.py:1060`).

### Field inventory per section

| Section | Fields | Evidence |
|---|---|---|
| `ExecutionSettings` (33) | `max_concurrent_intents`, `max_parallel_tasks`, `agent_pool_size`, `max_agent_turns`, `authoring_max_generation_attempts`, `authoring_max_phase_turns`, `max_output_tokens`, `turn_timeout_s`, `auto_compact`, `context_windows`, `prompt_cache`, `file_cache`, `transport_max_retries`, `transport_retry_base_delay_s`, `transport_retry_max_total_s`, `llm_sdk_max_retries`, `profile`, `provider`, `model`, `api_key`, `api_key_env`, `base_url`, `default_headers`, `default_query`, `client_options`, `request_options`, `timeout_s`, `temperature`, `top_p`, `max_completion_tokens`, `provider_capabilities`, `_resolved_profile`, `session_header` | `config.py:986` |
| `ToolSettings` (10) | `mcp_servers`, `plugins`, `allowed`, `denied`, `max_live_tool_calls`, `group_exploratory_calls`, `http_timeout_s`, `cloakbrowser`, `playwright`, `browser_backend` | `config.py:1287` |
| `MemorySettings` (3) | `project_memory_path`, `vector_db`, `session_ttl_seconds` | `config.py:1315` |
| `SecuritySettings` (5) | `sandbox_mode`, `allowed_paths`, `network_allow_list`, `max_tool_cpu_seconds`, `max_tool_memory_mb` | `config.py:1322` |
| `ApiSettings` (3) | `host`, `port`, `api_key_env` | `config.py:1331` |
| `PluginSettings` (9) | `auto_trust`, `auto_install`, `install_target`, `allowed_modules`, `timeout_seconds`, `disabled`, `trust_file`, `audit_file`, `strict_cli_shadow` | `config.py:1338` |
| `BehaviourSettings` (3) | `verbose`, `confirm_exits`, `resume_transcript_turns` | `config.py:1353` |
| `CloakBrowserSettings` (15) | `enabled`, `transport`, `cdp_endpoint`, `allowed_domains`, `headless`, `navigation_timeout_s`, `action_timeout_s`, `max_pages`, `max_actions_per_turn`, `max_snapshot_chars`, `max_screenshot_bytes`, `allow_persistent_profiles`, `profile_root`, `license_key_env`, `allow_all_domains` | `config.py:1129` |
| `PlaywrightSettings` (16) | `enabled`, `transport`, `browser_type`, `browser_channel`, `executable_path`, `allowed_domains`, `headless`, `navigation_timeout_s`, `action_timeout_s`, `max_pages`, `max_actions_per_turn`, `max_snapshot_chars`, `max_screenshot_bytes`, `allow_persistent_profiles`, `profile_root`, `allow_all_domains` | `config.py:1220` |
| `SkillsSettings` (2) | `install_default_skills`, `default_skill_directory` | `config.py:1455` |
| `StorageSettings` (2) | `s3`, `default_backend` | `config.py:1482` |

`ExecutionSettings` also contains the naming inversion recorded in
`docs/reference/repository-state.md`: `timeout_s` is the LLM timeout
(`_DEFAULT_LLM_TIMEOUT_S: float = 3_600.0`, `config.py:110`) while
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

!!! bug "Every `SKILL.md` in `skills/` currently fails this schema"
    All five skills use `skill:` and `summary:` frontmatter. Neither is in
    `_KNOWN_FIELDS`, so the loader emits `unknown-frontmatter-field` for each,
    then `missing-name` and `missing-description`, and falls back to the
    directory name with an empty description. Running the loader against this
    repository's `skills/` directory produces **16 diagnostics**:

    ```text
    skills/extending-with-hooks/SKILL.md: unknown field(s): skill, summary (unknown-frontmatter-field)
    skills/extending-with-hooks/SKILL.md: name is missing; directory name is used (missing-name)
    skills/extending-with-hooks/SKILL.md: description is missing (missing-description)
    ... the same three for headless-api, running-the-tui, testing-agenthicc, and using-memory ...
    skills/README.md: skill root entry is not a directory (ignored-entry)
    ```

    This is a documentation defect, not a code defect — the loader is behaving
    as designed. See the [stale reference register](#stale-reference-register).

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

`mkdocs.yml` drives the published site. The nav currently resolves: **no nav
entry points at a missing file**.

Four pages exist but are **absent from the nav**, so they are unreachable by
navigation:

| Orphan page | Title | File |
|---|---|---|
| `docs/guides/exploratory-tool-calls.md` | Exploratory tool-call presentation | `docs/guides/exploratory-tool-calls.md:1` |
| `docs/guides/startup.md` | Startup and readiness | `docs/guides/startup.md:1` |
| `docs/guides/usage-accounting.md` | Usage accounting and `/usage` | `docs/guides/usage-accounting.md:1` |
| `docs/reference/usage-ledger.md` | Usage ledger reference | `docs/reference/usage-ledger.md:1` |

`README.md` links to `./docs/guides/startup.md`, so a reader following the
README reaches a page the site navigation does not offer.

---

## Stale reference register

Verified-absent symbols and modules. `src/agenthicc/api/` does not exist on
disk; the modules below are named in prose but are not importable.

| Symbol / module | Status | Referenced in |
|---|---|---|
| `agenthicc.api.server` | **absent** — no `src/agenthicc/api/` package | `skills/headless-api/SKILL.md:24,51,323`; `skills/testing-agenthicc/SKILL.md:272` |
| `create_app` | **absent** | `skills/headless-api/SKILL.md:5,21,24,33,36,51,62,277,289,323,328,352,368`; `skills/testing-agenthicc/SKILL.md:272,277` |
| FastAPI / REST / WebSocket server | **absent** — no ASGI app, no API extra | `skills/headless-api/SKILL.md` (whole document) |
| `agenthicc.tui.app` | **absent** — no `tui/app.py` | `skills/running-the-tui/SKILL.md:219,350` |
| `TranscriptModel` | **absent** — no `tui/transcript.py` | `skills/running-the-tui/SKILL.md:127,351,353`; `skills/testing-agenthicc/SKILL.md:240,243,246` |
| `render_frame_ansi` | **absent** | `skills/running-the-tui/SKILL.md:344,346,350,357,375` |
| `agenthicc.tui.transcript` | **absent** | `skills/running-the-tui/SKILL.md:351`; `skills/testing-agenthicc/SKILL.md:243` |
| `EventBusTestHarness` | **absent** — not in `agenthicc.testing.__all__` | `skills/testing-agenthicc/SKILL.md:5,15,62,107,110,318`; `skills/README.md:30` |
| `prompt_toolkit` / `build_app` | **absent** — the TUI is Rich Live based | `skills/running-the-tui/SKILL.md:4,266-269,295,376` |

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
| `llms.txt:236` | Names `tui.transcript`/`tui.events` as historical |

The pattern is sound: `docs/`, `README.md`, `CLAUDE.md`, and `llms.txt` warn
about absence; only `skills/` asserts presence. That asymmetry is the single
largest accuracy defect in the documentation set.

### Claims that look stale but are verified real

Checked specifically because they are counter-intuitive. Do not delete these.

| Claim | Verification |
|---|---|
| `/create-tools` and `/create-commands` exist | Provided as bootstrap skills, `src/agenthicc/skills/bootstrap.py:222,269` |
| `/bg` and `/background` exist | Injected at `src/agenthicc/background/integration.py:117-121` |
| `agenthicc agents` and `agenthicc jobs` both exist | Both registered, `src/agenthicc/cli/commands/background.py:117,124` |
| A TUI command picker sees `/workflow` and `/compact` | Both are registry entries with `handler=None`, intercepted in `TUISession.route()` |
| `name_that_ui.py` exists | Present at `src/agenthicc/workflows/name_that_ui.py` — older notes said it was missing |
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

All three are ignored per `.gitignore:11` (`build/`), `:13` (`dist/`), and
`:227` (`site/`).

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

# 4. CLI subcommand decorators
grep -rho '@command([^)]*)' src/agenthicc/cli/commands/ | sort

# 5. Workflow table
sed -n '45,80p' src/agenthicc/workflows/loader.py

# 6. Skill frontmatter schema
sed -n '31,50p' src/agenthicc/skills/loader.py

# 7. Config sections
sed -n '1491,1503p' src/agenthicc/config.py

# 8. llms-full.txt heading count (expect >= 22)
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

On an unmodified checkout this prints five skills with `description=''` and 16
diagnostics.

## Related

- [Current repository state](repository-state.md) — evidence-backed boundaries and known risks
- [CLI reference](cli.md) — flags and subcommands in detail
- [Kernel reference](kernel.md) — events, reducers, and persistence
- [Storage reference](storage.md) — files, paths, and retention
