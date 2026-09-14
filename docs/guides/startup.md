# Startup and readiness

agenthicc uses progressive startup. The process first establishes the small,
local runtime needed to identify a session and render a usable interface. It
then hydrates optional or expensive integrations behind explicit readiness
phases. This keeps a slow network, a missing browser binary, or a large store
from making a new TUI look hung.

## Fast commands

agenthicc --version and agenthicc --help use a side-effect-free parser.
They do not load project command/workflow files, open a session lease, replay
session history, construct a provider, connect to MCP, launch a browser, or
contact the network. Built-in global options and command-family metadata are
available immediately; project commands are discovered only on a normal
session path.

    uv run agenthicc --version
    uv run agenthicc --help

The normal parser loads one configuration snapshot before dynamic command
discovery. That same snapshot is passed into session construction, so the
runner does not parse TOML/environment/CLI overrides a second time. Direct
Python callers that do not provide a snapshot retain the backwards-compatible
fallback and load configuration while constructing their session.

## Startup phases

StartupCoordinator is a session-owned diagnostic component. Each phase has a
state (not_started, loading, ready, degraded, failed, or cancelled), monotonic
elapsed time, and a bounded error summary. Diagnostics never include API
keys, headers, prompts, transcript text, tool arguments, or arbitrary full
exception payloads.

The first TUI frame is allowed to show while optional phases are loading. Safe
local commands remain usable; an operation that needs a deferred phase waits
for that phase and receives a structured blocker if it failed. The provider,
workflow implementations, project agents, skills, project tools, and project
commands are loaded after the first frame for a fresh interactive session.
Built-in workflow and agent descriptors remain available for safe metadata and
are materialized only when selected.

The TUI status bar shows a compact Startup indicator while work is pending.
/startup prints the complete bounded phase report, including whether the phase
was deferred and its elapsed duration. It is safe to use while startup is in
progress.

The important boundaries are:

| Boundary | Synchronous work | Deferred work |
|---|---|---|
| Bootstrap | configuration snapshot, workspace/policy, owner lease | none |
| Session | metadata index, selected journal/kernel restore, event processor | unrelated session replay |
| Shell | built-in commands, triggers, workspace, input panel | project command/skill/tool discovery |
| Agent operation | selected workflow/agent, provider, required MCP | none of the selected operation's dependencies |
| Optional integrations | validated configuration and policy | MCP connections, browser runtime, semantic index, changelog refresh |

Deferred tasks belong to the same session coordinator and are cancelled and
awaited during shutdown. They cannot publish into a closed session.

## Session service and the metadata index

SessionService construction does not replay every saved session. The
append-only JSONL files in its store remain authoritative. A small atomic
index.json projection contains only session id, project root, lifecycle,
timestamps, sequence, capabilities, and file fingerprints. Listing and
pagination use this bounded projection. Selecting, resuming, controlling, or
subscribing to one session materializes only that session's event history.

Legacy stores without an index are supported: listing reads the first bounded
valid event from each log, while selected-session access performs the existing
full replay. A missing, stale, incompatible, oversized, or partially written
index is rebuilt from event files. Index writes use a unique fsynced temporary
file, atomic replacement, directory fsync where supported, and a short
cross-process advisory lock. If the projection cannot be updated, the event
append still succeeds and the next access retries repair.

The index is never a second source of truth. Corrupt event lines retain the
existing tolerant replay policy, sequence/cursor and compaction semantics are
unchanged, and a selected session's conversation id/journal remains owned by
the normal session-conversation layer.

## Changelog and optional backends

The welcome panel is rendered from local static content and a bounded,
last-known-good cache. The remote JSON changelog at
https://agenthicc.dev/changelog.json refreshes in the background after the
session coordinator exists. Timeout, HTTP, malformed JSON, and schema errors
produce No list under the persistent What's new heading. The cache is bounded,
age-checked, atomically written with mode 600, and never used as an instruction
or tool source. AGENTHICC_CHANGELOG_CACHE can select an isolated cache path in
tests.

Playwright and CloakBrowser configuration/policy are still validated by the
normal security boundary, but their optional modules and browser processes are
not imported or launched merely to render a TUI. Their session/tool factories
are lazy. Missing optional dependencies therefore affect only a browser
operation and leave unrelated local work available. MCP configuration is
validated during session setup; optional connections/catalogues start in the
background, while an operation that declares a required MCP dependency waits
for its existing required-resource failure semantics.

Semantic/vector memory follows the same pattern: the session/project memory
layers are available through the existing router, while the heavier semantic
implementation is constructed on its first search or insert.

## Measuring startup

The benchmark helper runs isolated child processes and reports p50/p95 rather
than conflating process-spawn overhead with application work:

    uv run python scripts/benchmark_startup.py --samples 5 --offline
    uv run python scripts/benchmark_startup.py --samples 5 --sessions 100 --events 1000 --offline

Use temporary homes and synthetic event logs for repeatable comparisons. A
benchmark result is diagnostic; CI thresholds should be applied on stable
hardware and should distinguish local startup from intentionally slow remote
dependencies.

## Extension contract for generated workflows

Generated workflows from create_workflow use the same session-owned
registries, readiness gates, provider laziness, conversation id, journal,
checkpoint, workspace policy, and MCP/browser boundaries as built-ins. A
generated module must keep optional imports inside a phase/tool factory and
declare the readiness dependency for any operation that needs it. Import-time
side effects, eager provider/browser/MCP construction, and private parallel
session stores are invalid workflow implementations.

## Try it

The side-effect-free parser can be exercised directly, and the configuration
snapshot can be validated without opening a session:

```bash
PYTHONPATH=src python -m agenthicc --version
```

```text
agenthicc 0.1.0
```

```bash
PYTHONPATH=src python -m agenthicc config validate
```

```text
Configuration is valid: legacy execution settings (anthropic/deepseek-v4.1-flash)
```

The trailing description reflects your own configured provider and model, so it
differs per machine; the `Configuration is valid: ` prefix and a zero exit code
do not. `--version` never builds the full parser, which is why it answers even
when a project extension is broken.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `agenthicc --version` is slow | A project extension or durable store is being loaded | It should not be: `--version` and `--help` are answered before command discovery (`src/agenthicc/cli/parser.py:100-108`). Check for a shell alias or wrapper that runs something else first |
| A new TUI shows a `Startup` indicator that never clears | A deferred phase is still `loading`, or one reached `failed` | Run `/startup` for the bounded phase report; a failed *optional* phase renders as `degraded` and does not block local work |
| An operation blocks waiting for a dependency | It declared a required-resource dependency, such as the MCP catalogue | That is correct fail-closed behavior. Fix the dependency rather than retrying the operation |
| A project tool or slash command is missing right after launch | Project extension discovery is deferred until after the first frame | Wait for the shell phase to become `ready`, then `/tools reload` or `/commands reload` |
| Listing sessions replays a large history | A missing, stale, or incompatible `index.json` forced a rebuild | Expected after an upgrade. The index is a projection, never a second source of truth; the JSONL files stay authoritative |
| Two processes fight over the session index | Writes use a short cross-process advisory lock | Retry; a failed index update still lets the event append succeed and repairs on next access |
| Browser or MCP work fails while everything else is fine | Optional modules and processes are lazy by design | Missing optional dependencies should affect only the browser/MCP operation. Install the relevant extra if you need it |
| `What's new` says `No list` | The remote changelog fetch timed out, returned non-JSON, or failed schema validation | Local static content plus the last-known-good cache still render. Check network reachability or set `AGENTHICC_CHANGELOG_CACHE` for an isolated cache path |

### Measure before blaming the runtime

Startup benchmarks deliberately run isolated child processes and report p50/p95,
so process-spawn overhead is not counted as application work:

```bash
uv run python scripts/benchmark_startup.py --samples 5 --offline
```

If p50 is fine but p95 spikes, look at the deferred optional phases rather than
at the bootstrap phase. Use a temporary home and synthetic event logs for
repeatable numbers.

### A generated workflow breaks startup laziness

Generated workflows must keep optional imports inside a phase or tool factory
and declare a readiness dependency for anything they need. An import-time side
effect, an eagerly constructed provider/browser/MCP client, or a private
parallel session store is not a valid workflow implementation — it defeats the
first-frame guarantee for every session, not just its own.
