# User-defined tools

User-defined tools are Python plugins loaded into an agent turn. The normal
project workflow is deliberately small: put a module below
`.agenthicc/tools/`, decorate a callable with lauren-ai's `@tool()`, export it
through `TOOLS`, and restart the session.

This guide describes the current runtime, including the places where a
configuration field or helper exists but is not yet connected to the normal
TUI tool-loading path.

## Built-in self-inspection tools

Before adding a tool, know that every session already carries five read-only
tools for reading agenthicc's *own* documentation and source. They exist because
prose in a system prompt goes stale as soon as the code changes; these read the
installed artefact instead.

| Tool | Purpose |
| --- | --- |
| `list_agenthicc_docs(section="")` | Index of every document: path, title, line count, size |
| `read_agenthicc_doc(path, start_line=1, max_lines=400)` | One bounded window; page with the returned `next_start_line` |
| `search_agenthicc_docs(query, max_results=40, section="")` | Matching lines with document path and line number |
| `inspect_agenthicc_source(target, include_source=True)` | Source, signature, docstring, and member outline of any `agenthicc` module or symbol |
| `search_agenthicc_source(query, max_results=40, module="")` | Matching lines in the package source |

The documentation surface is the `docs/` tree plus `llms.txt`, `llms-full.txt`,
and `README.md`. It resolves from a source checkout's `docs/`, from
`<prefix>/share/agenthicc/docs` in an installed distribution, or from
`AGENTHICC_DOCS_DIR`; a directory counts only when it contains `index.md`.

`inspect_agenthicc_source` accepts a module (`agenthicc.kernel.reducer`), a symbol
(`agenthicc.workflows.plugin:PhaseSpec`), a method
(`agenthicc.workflows.plugin:WorkflowPlugin.build_runner`), or the all-dots form,
including private names. It resolves the module to a file path and parses it with
`ast` — **nothing is imported** — so a module with an unavailable optional
dependency is still inspectable and no import side effect can fire. Pass
`include_source=False` for a cheap outline of a large module first.

Both roots are enforced: a documentation path that traverses out of the tree, and
a source target outside the `agenthicc` package, are refused before any read. All
five are tagged `READ` (the searches also `SEARCH`), so they remain available in
Safe and Plan mode.

Every callable tool must declare capability metadata when it can do so. Safe
allows `READ`, `SEARCH`, and `GIT_READ` directly; `WRITE`, `GIT_WRITE`,
`EXECUTE`, `NETWORK`, and missing or malformed metadata require approval. Plan
hard-blocks the latter capabilities and unannotated tools before approval.
Yolo is the unrestricted mode. These are executor gates, not merely prompt
instructions, so project, MCP, dynamic, and built-in tools follow the same
policy.

## The shortest working path

Create `.agenthicc/tools/project_status.py`:

```python
from lauren_ai import tool


@tool(
    name="project_status",
    description="Return a short status message for the current project.",
)
async def project_status(topic: str = "project") -> dict[str, object]:
    """Return a status message.

    Args:
        topic: The subject to include in the status message.
    """
    return {"ok": True, "topic": topic, "status": "ready"}


TOOLS = [project_status]
```

Then start a new session:

```bash
uv run agenthicc
```

Ask the agent to use `project_status`. The startup scan should report the
loaded project tool. The tool name, description, annotations, and docstring
are used to build the schema shown to lauren-ai. `@tool()` must include the
parentheses.

## Author a tool with `/create-tools`

For an agent-generated tool, invoke the project-authoring skill with its intent:

```text
/create-tools Create a tool that checks the configured Cloakbrowser endpoint
and returns a bounded recoverable status object.
```

The skill guides the agent to generate a complete raw Python module, including
the `@tool` decorator and a literal `TOOLS` export. Review the generated code
and run `/tools reload` after placing it in the trusted project tools directory.

Use `/tools` in the interactive session to inspect the effective tool catalog,
or `/tools reload` to rescan project and user-global tool plugins without
restarting. The listing includes a `Source` column with `builtin` or `plugin`;
MCP tools are shown as plugins because they are integrations loaded into the
session. Press Enter on a tool to see its source category, capabilities, and
runtime type. Reload failures leave the previous tool registry active.

User-global tools use the same shape below `~/.agenthicc/tools/` and are
available to projects run by that user. Agent-specific tools use:

```text
~/.agenthicc/agents/<agent-name>/tools/
.agenthicc/agents/<agent-name>/tools/
```

The scanner recursively loads non-private `*.py` files. Files whose name
starts with `_` are skipped. A module without `TOOLS` is valid but contributes
no callable tools; this is useful for a module that only exports a custom
`SUBAGENT_TYPES` entry.

## Function and class forms

Function-form tools are the easiest option. For stateful or more structured
tools, a no-argument class with a `run` method is also supported:

```python
from lauren_ai import tool


@tool(
    name="summarize_record",
    description="Summarize one record.",
)
class SummarizeRecord:
    async def run(self, record: str) -> dict[str, object]:
        return {"ok": True, "summary": record[:200]}


TOOLS = [SummarizeRecord]
```

The normal plugin loader exports callable objects. A class-form tool is
instantiated by lauren-ai for execution, so its constructor must not require
arguments in the normal project-plugin path. Dependency-injected instances
and the lower-level `Tool`/`ToolBase` contracts belong to explicit
`AgenthiccToolExecutor` registration; they are not automatically discovered
from `TOOLS` merely because they implement those base classes.

## Capabilities and approvals

Declare what a tool can do with the capability decorators in
`agenthicc.tools.capabilities`:

```python
from agenthicc.tools.capabilities import tool_read_search
from lauren_ai import tool


@tool_read_search
@tool(name="search_records", description="Search project records.")
async def search_records(query: str) -> dict[str, object]:
    return {"ok": True, "matches": []}


TOOLS = [search_records]
```

The available tags are `read`, `write`, `execute`, `git_read`, `git_write`,
`network`, and `search`, plus the common combinations such as
`tool_network_read` and `tool_network_write`. In the TUI, the capability gate
reads these tags on every call. A mode can hard-block a capability, and an
approval mode can pause the call for a user decision. An untagged plugin has
an empty capability set and passes the capability gate, so omitting a tag is
not a safety mechanism.

For a tool that always needs an explicit lauren-ai confirmation, use
`@tool(requires_confirmation=True)`. For side effects, also make repeated
calls safe or idempotent; transport and workflow retries can cause the model
to attempt the same logical operation again.

## Context, files, and network access

Lauren-ai can inject a `ToolContext` when the entry point declares a parameter
annotated with that type. The context parameter is hidden from the model's
JSON schema:

```python
from lauren_ai import ToolContext, tool


@tool(name="inspect_context", description="Inspect non-secret call metadata.")
async def inspect_context(ctx: ToolContext) -> dict[str, object]:
    return {
        "ok": True,
        "tool_name": ctx.tool_name,
        "tool_use_id": ctx.tool_use_id,
    }


TOOLS = [inspect_context]
```

Use `WorkspaceView` for filesystem paths and `NetworkGuard.check()` before an
outbound request when the runtime supplies those objects through context
extras. Reject the call if the required guard is unavailable. Use
`agenthicc_http_client()` for HTTP timeouts and `is_network_error()` to turn
transient network failures into bounded, recoverable results.

There is an important current boundary: the ordinary TUI path passes project
callables directly to lauren-ai and does not currently construct a
`ToolSandbox` or inject its `WorkspaceView`/`NetworkGuard` into those plugin
calls. The shared HTTP client provides timeout policy; it does not itself
enforce the network allow-list. Therefore, do not claim that
`[security].allowed_paths` or `network_allow_list` automatically protects a
new project plugin today. Until sandbox injection is implemented, a plugin
must either delegate to an existing bounded built-in tool or create and test
its own fixed boundary explicitly.

Never log credentials, access tokens, full email bodies, or unbounded remote
responses. Bound both inputs and outputs, and return a structured error for a
recoverable failure, for example:

```python
return {
    "ok": False,
    "error": "The remote service timed out; retry later.",
    "recoverable": True,
}
```

## What happens at startup and at call time

The current journey is:

1. `TUISession` scans `~/.agenthicc/tools/` first and
   `.agenthicc/tools/` second. Agent-specific directories are loaded when the
   active agent's registry is built.
2. Each module is imported as Python code. Missing declared or inferred
   dependencies cause that file to be skipped and a startup warning to be
   logged. The normal session path does not auto-install dependencies.
3. Built-ins, user-global tools, project-local tools, and then
   agent-specific tools are merged into a `ToolRegistry`.
4. The `ToolRegistry` deduplicates tools by callable `__name__`. The decorated
   lauren-ai name controls the provider-facing schema, so give both the Python
   callable and the declared tool unique, intentional names. Later entries
   win: project tools can shadow user-global and built-in tools, while
   agent-specific tools have the highest precedence. Shadowing a built-in is
   logged.
5. The registry is attached to the temporary lauren-ai agent class for the
   turn. The model may call any tool that remains in that agent's registry and
   allowed role/phase set.
6. Capability and approval hooks run before the callable. Results are rendered
   as bounded TUI tool output and persisted with the surrounding session
   events.

Project-wide tools are discovered during session construction and cached in
the session context. Restart the session after adding or editing one. The
agent-specific scan happens when an agent turn builds its registry, but a
restart is still the least surprising way to verify the complete catalog.

## Dependencies and import-time behavior

A plugin may declare dependencies in the module:

```python
DEPENDENCIES = ["httpx>=0.27"]
```

or in a sidecar file with the matching stem, such as
`project_status.requirements.txt`. Prefer installing dependencies into the
project's existing environment and leave automatic installation disabled.

The loader currently probes a module to read `DEPENDENCIES` and then imports
it again to load `TOOLS`. Keep module import time side-effect free: do not
send network requests, mutate files, register irreversible state, or print
secrets at import time. Put operational work inside the tool entry point.

## Trust and configuration: current status

Tool plugin files are executable Python. Review them before starting a
session, especially when they come from another repository. The repository
contains a `plugins.trust.check_trust()` helper, but the normal project-tool
discovery path does not currently call it and does not show a trust prompt.
`trusted_plugins.json` is therefore not an automatic permission boundary for
`.agenthicc/tools/` today.

The following settings and helpers exist, but should not be treated as proof
that a user plugin is isolated in the current TUI path:

| Surface | Current behavior |
|---|---|
| `[tools].allowed` / `denied` | Parsed and available to policy-building code; not wired into the TUI's direct plugin registry path. |
| `[security].allowed_paths` | Parsed configuration and `WorkspaceView` input; not automatically injected into project callables. |
| `[security].network_allow_list` | Used by an explicit `NetworkGuard`; not automatically applied by `agenthicc_http_client()`. |
| `[plugins].auto_install` | A loader option exists, but normal session discovery calls the scanner with auto-install disabled. |
| Plugin trust manifest | The trust helper exists, but normal tool discovery does not invoke it. |

Use capability decorators, mode restrictions, approval prompts, explicit
resource boundaries, and tests together. The [security guide](security.md)
contains the broader checklist and current limitations.

## Optional CloakBrowser tools

The session can expose nine built-in `cloakbrowser_*` tools when
`[tools.cloakbrowser].enabled = true` (the configuration default): `status`,
`open`, `snapshot`, `click`, `fill`, `press`, `wait_for`, `screenshot`, and
`close`. Install the optional `cloakbrowser` extra to make the browser backend
available; without it, a configured allow-list produces a
`dependency_missing` status and navigation remains unavailable. The default
empty allow-list reports `not_configured`. They are closures over one browser manager keyed by
the session's stable conversation ID, so direct turns and workflow phases see
the same browser context. Page, profile, and artifact directories use opaque
browser-session handles rather than the provider conversation ID. Browser calls
accept an optional bounded `operation_id`; reusing it returns the original
structured receipt and prevents a retry from repeating a mutation.

The adapter enforces an operator domain/origin allow-list, HTTP(S)-only
navigation, DNS/private-address checks, bounded pages/actions/snapshots/screenshots,
safe keyboard keys, and rejection of sensitive form fields. Allow-list entries
may be bare hosts or exact HTTP(S) origins such as `https://example.com:8443`;
`https://*.example.com` permits subdomains only. It provides no raw
JavaScript, arbitrary CDP, proxy, cookie, storage, or stealth controls. Page
screenshots are written only through `WorkspaceView` below
`.agenthicc/browser-artifacts/`. The live browser context is never serialized;
workflow checkpoints retain only redacted page metadata and reattach the same
session manager on resume.

`allow_all_domains = true` is an explicit broad-access opt-in, not the default.
It permits public HTTP(S) hosts on the configured ports but does not bypass
DNS, loopback, or private-address protections.

Custom workflows receive these tools through `WorkflowConfig`. Their authoring
and validation phases are browser-free by default. `create_workflow` exposes
`describe_cloakbrowser_tools()` and `describe_playwright_tools()` so a generated
workflow can declare the right `NETWORK`/`READ` or `NETWORK`/`WRITE` phase
capabilities without importing CloakBrowser or Playwright directly.

### Optional Playwright tools

The Microsoft Playwright backend provides the same nine bounded tool operations
with a `playwright_*` prefix. Set `[tools].browser_backend = "playwright"` and
install the optional `playwright` extra; the available names are
`playwright_status`, `playwright_open`, `playwright_snapshot`,
`playwright_click`, `playwright_fill`, `playwright_press`,
`playwright_wait_for`, `playwright_screenshot`, and `playwright_close`.
Choose `browser_type = "chromium"`, `"firefox"`, or `"webkit"` in
`[tools.playwright]`. The backend is lazy and returns `dependency_missing` when
the Python package or its browser runtime is unavailable. It never imports
Playwright or starts a browser when CloakBrowser is selected.

The upstream wrapper and binary have independent platform and licensing
requirements; agenthicc does not download binaries or silently install the
extra. Operators are responsible for authorized use and for retaining any
license key only in the configured environment variable.

## Testing a user-defined tool

At minimum, test the callable directly and test its plugin boundary:

- successful input and the exact returned shape;
- missing, malformed, and out-of-range input;
- capability denial and approval behavior for side effects;
- path traversal, absolute-path, and symlink escapes for file tools;
- disallowed hosts, timeouts, and transient network errors for network tools;
- bounded output and cancellation behavior;
- retry behavior, including duplicate side effects and idempotency keys;
- discovery from `TOOLS`, missing dependencies, import errors, and name
  collisions.

Useful repository contracts are covered by
`tests/unit/test_plugin_discovery.py`,
`tests/unit/test_plugin_registry.py`,
`tests/unit/test_plugin_security.py`,
`tests/unit/test_tool_executor_contract.py`, and
`tests/unit/test_sandbox.py`.

For the prompt-driven authoring skill, submit its instructions directly:

```text
/create-tools Create a tool that reports the current project status.
```

The `/create-tools` skill is independent of `create_workflow`; it gives the
agent project-specific guidance for producing a Lauren `@tool`-decorated
`TOOLS` export. Review generated code and reload the tool registry after
placing a trusted plugin in `.agenthicc/tools/`.
