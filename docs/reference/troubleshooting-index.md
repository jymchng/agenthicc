# Troubleshooting index

Troubleshooting in these documents lives in two places, and they answer
different questions:

- **[Symptom-first](#symptom-first)** — `docs/usage/12-troubleshooting.md`.
  Start here when something is visibly wrong: a command failed, an error was
  printed, a run stopped.
- **[Concept-level traps](#concept-level-traps-by-subject)** — the
  `## Troubleshooting` section of each guide. These are the failures that
  produce *no* error message, usually because the wrong layer is being
  blamed.

A third category exists and is deliberately excluded: known-but-open
correctness findings are tracked in
[Workflow package review](workflow-review.md) rather than indexed here as
symptoms, because they are not user-fixable.

## Symptom first

From `docs/usage/12-troubleshooting.md`.

| Symptom | Where |
|---|---|
| Nothing works and you have not looked at logs yet | [Start with these three](../usage/12-troubleshooting.md#start-with-these-three) |
| Provider rejects the request or claims a missing key | [Provider authentication failures](../usage/12-troubleshooting.md#provider-authentication-failures) |
| The configured model is not accepted | [Model not found](../usage/12-troubleshooting.md#model-not-found) |
| Requests are being throttled | [Rate limits](../usage/12-troubleshooting.md#rate-limits) |
| Config changes are refused at startup | [Configuration is rejected](../usage/12-troubleshooting.md#configuration-is-rejected) |
| You edited a file and nothing changed | [My change had no effect](../usage/12-troubleshooting.md#my-change-had-no-effect) |
| Resuming a session does not restore history | [Session resume problems](../usage/12-troubleshooting.md#session-resume-problems) |
| An MCP server will not connect or keeps failing | [MCP server problems](../usage/12-troubleshooting.md#mcp-server-problems) |
| A tool or command you expect is absent | [A tool or command is missing](../usage/12-troubleshooting.md#a-tool-or-command-is-missing) |
| Paste is mangled or a mode will not switch | [TUI paste and mode issues](../usage/12-troubleshooting.md#tui-paste-and-mode-issues) |
| An unattended run stops on a prompt | [A prompt blocks an unattended run](../usage/12-troubleshooting.md#a-prompt-blocks-an-unattended-run) |

## Concept-level traps by subject

Each guide's troubleshooting section is listed with the specific traps it
covers, so you can jump straight to the relevant one instead of reading the
whole section.

### Install, configuration, and first run

| Guide | Traps |
|---|---|
| [Quickstart](../guides/quickstart.md#troubleshooting) | [Eight workflows, not five](../guides/quickstart.md#eight-workflows-not-five) · [Headless without a workflow](../guides/quickstart.md#headless-without-a-workflow) |
| [Configuration](../guides/configuration.md#troubleshooting) | [Validate, then show, then test](../guides/configuration.md#validate-then-show-then-test) · [Generated configuration must stay parseable](../guides/configuration.md#generated-configuration-must-stay-parseable) |
| [Project bootstrap](../guides/project-bootstrap.md#troubleshooting) | [Initiating is not the same as trusting](../guides/project-bootstrap.md#initiating-is-not-the-same-as-trusting) · [Do not scaffold into a dirty tree](../guides/project-bootstrap.md#do-not-scaffold-into-a-dirty-tree) |
| [Startup](../guides/startup.md#troubleshooting) | [Measure before blaming the runtime](../guides/startup.md#measure-before-blaming-the-runtime) · [A generated workflow breaks startup laziness](../guides/startup.md#a-generated-workflow-breaks-startup-laziness) |

### Kernel, state, and events

| Guide | Traps |
|---|---|
| [Architecture](../guides/architecture.md#troubleshooting) | [The state did not change after an event](../guides/architecture.md#the-state-did-not-change-after-an-event) · [Two `AppState` classes look identical](../guides/architecture.md#two-appstate-classes-look-identical) · [A background or workflow operation never becomes ready](../guides/architecture.md#a-background-or-workflow-operation-never-becomes-ready) |
| [Architecture diagram](../guides/architecture-diagram.md#troubleshooting) | [A node in the diagram has no matching path](../guides/architecture-diagram.md#a-node-in-the-diagram-has-no-matching-path) · [An edge points upward into the kernel](../guides/architecture-diagram.md#an-edge-points-upward-into-the-kernel) · [A new package does not obviously fit a layer](../guides/architecture-diagram.md#a-new-package-does-not-obviously-fit-a-layer) |
| [TUI](../guides/tui.md#troubleshooting) | [Which `AppState` am I looking at?](../guides/tui.md#which-appstate-am-i-looking-at) · [A feature is invisible after a restart](../guides/tui.md#a-feature-is-invisible-after-a-restart) |

### Workflows

| Guide | Traps |
|---|---|
| [Workflows](../guides/workflows.md#troubleshooting) | Missing workflow, unknown agent type, no write tools, resume losing context, `/workflow` doing nothing, `create_workflow` unknown, ignored custom runner |
| [Custom workflows and TOML configuration](../guides/custom-workflows-and-config.md#troubleshooting) | [Eight builtins, and the README says five](../guides/custom-workflows-and-config.md#eight-builtins-and-the-readme-says-five) · [Phase declarations are the contract](../guides/custom-workflows-and-config.md#phase-declarations-are-the-contract) |

### Modes, security, and approvals

| Guide | Traps |
|---|---|
| [Security](../guides/security.md#troubleshooting) | [Which layer actually decides](../guides/security.md#which-layer-actually-decides) · [Grants are per operation and do not accumulate](../guides/security.md#grants-are-per-operation-and-do-not-accumulate) |
| [Exploratory tool calls](../guides/exploratory-tool-calls.md#troubleshooting) | [Exploratory calls are observations, not approvals](../guides/exploratory-tool-calls.md#exploratory-calls-are-observations-not-approvals) · [Presentation is not policy](../guides/exploratory-tool-calls.md#presentation-is-not-policy) |

### Tools, MCP, plugins, and hooks

| Guide | Traps |
|---|---|
| [Tools](../guides/tools.md#troubleshooting) | [Capability gates are re-read on every call](../guides/tools.md#capability-gates-are-re-read-on-every-call) · [A tool that needs network access](../guides/tools.md#a-tool-that-needs-network-access) |
| [Connecting MCP servers](../guides/mcp.md#troubleshooting) | [The transport-alias trap](../guides/mcp.md#the-transport-alias-trap) · [Diagnostic ladder](../guides/mcp.md#diagnostic-ladder) · [Deny by default](../guides/mcp.md#deny-by-default) |
| [Extensions](../guides/plugins.md#troubleshooting) | [Discovery debugging](../guides/plugins.md#discovery-debugging) · [Trust is not a sandbox](../guides/plugins.md#trust-is-not-a-sandbox) |
| [Hooks and lifecycle status](../guides/hooks.md#troubleshooting) | [Do not add a second hook engine](../guides/hooks.md#do-not-add-a-second-hook-engine) · [The historical PRD examples are not current APIs](../guides/hooks.md#the-historical-prd-examples-are-not-current-apis) |

### Commands and execution

| Guide | Traps |
|---|---|
| [User-defined commands](../guides/commands.md#troubleshooting) | [Enumerate the registry instead of guessing](../guides/commands.md#enumerate-the-registry-instead-of-guessing) · [Slash commands are not a security boundary](../guides/commands.md#slash-commands-are-not-a-security-boundary) |
| [Command execution](../guides/command-execution.md#troubleshooting) | [Zero exit versus service readiness](../guides/command-execution.md#zero-exit-versus-service-readiness) · [Cancelling the wrong thing](../guides/command-execution.md#cancelling-the-wrong-thing) |

### Sessions, memory, and background work

| Guide | Traps |
|---|---|
| [Client-neutral sessions](../guides/session-service.md#troubleshooting) | [`sessions` and `session` are different groups](../guides/session-service.md#sessions-and-session-are-different-groups) · [Replay returned a gap instead of history](../guides/session-service.md#replay-returned-a-gap-instead-of-history) · [An adapter imports a Rich widget or the reducer](../guides/session-service.md#an-adapter-imports-a-rich-widget-or-the-reducer) |
| [Memory](../guides/memory.md#troubleshooting) | [The journal is durable, the projection is not](../guides/memory.md#the-journal-is-durable-the-projection-is-not) · [Memory grew without a visible cause](../guides/memory.md#memory-grew-without-a-visible-cause) |
| [Background sessions](../guides/background-sessions.md#troubleshooting) | [A job is a control-plane record, not a runtime](../guides/background-sessions.md#a-job-is-a-control-plane-record-not-a-runtime) · [Manage the index, not the state](../guides/background-sessions.md#manage-the-index-not-the-state) |

### Agents and subagents

| Guide | Traps |
|---|---|
| [Subagents](../guides/subagents.md#troubleshooting-checklist) | [The parent never calls `spawn_subagents`](../guides/subagents.md#the-parent-never-calls-spawn_subagents) · [The tool is not present in the provider request](../guides/subagents.md#the-tool-is-not-present-in-the-provider-request) · [An implementer says it completed but `ok` is false](../guides/subagents.md#an-implementer-says-it-completed-but-ok-is-false) · [A worker fails immediately with no available tools](../guides/subagents.md#a-worker-fails-immediately-with-no-available-tools) · [Safe mode does not show an approval](../guides/subagents.md#safe-mode-does-not-show-an-approval) · [A repeated call returns old results](../guides/subagents.md#a-repeated-call-returns-old-results) · [The provider returns a 400 or tool-schema error](../guides/subagents.md#the-provider-returns-a-400-or-tool-schema-error) |

### Testing, type safety, and accounting

| Guide | Traps |
|---|---|
| [Testing](../guides/testing.md#troubleshooting) | [Test the boundaries, not the happy path](../guides/testing.md#test-the-boundaries-not-the-happy-path) · [Cassette replay is exact](../guides/testing.md#cassette-replay-is-exact) |
| [Type safety](../guides/type-safety.md#troubleshooting) | [Run the whole gate, not one tool](../guides/type-safety.md#run-the-whole-gate-not-one-tool) · [The baseline is not a waiver](../guides/type-safety.md#the-baseline-is-not-a-waiver) |
| [Usage accounting](../guides/usage-accounting.md#troubleshooting) | [The ledger never stores prompts or responses](../guides/usage-accounting.md#the-ledger-never-stores-prompts-or-responses) · [Compaction counts too](../guides/usage-accounting.md#compaction-counts-too) |

## Coverage

Every guide under `docs/guides/` that has a troubleshooting section appears
above. Current counts:

| Measure | Count |
|---|---|
| Guide pages with a troubleshooting section | 24 |
| Traps indexed individually | 55 |
| Guides whose troubleshooting is a bullet list rather than subsections | 1 |
| Symptom entries indexed from `docs/usage/` | 11 |

[Testing](../guides/testing.md) and [Type safety](../guides/type-safety.md) are listed here even
though their audience is contributors, because both traps are reached by
following a user-visible failure.

[Workflows](../guides/workflows.md) is the one guide whose troubleshooting
section is a flat bullet list rather than named subsections, so it is indexed
as a single entry above rather than expanded into individual traps.

## Verifying this page

The checker resolves every `path#anchor` link above against the real heading
text in the target file, using the same slug rules MkDocs applies, and confirms
the counts in the coverage table:

```bash
PYTHONPATH=src python /tmp/check_nav_pages.py
```

## Related

- [Using agenthicc — the complete user manual](../usage/index.md)
- [Glossary](../glossary.md)
- [Documentation fact base](fact-base.md)
