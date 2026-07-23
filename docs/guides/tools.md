# User-defined tools

User-defined tools are Python plugins loaded into an agent turn. The normal
project workflow is deliberately small: put a module below
`.agenthicc/tools/`, decorate a callable with lauren-ai's `@tool()`, export it
through `TOOLS`, and restart the session.

This guide describes the current runtime, including the places where a
configuration field or helper exists but is not yet connected to the normal
TUI tool-loading path.

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

For the built-in authoring workflow, use `/create-tools <instructions>`.
That skill helps generate the module and tests; it does not establish trust or
replace a human review of executable plugin code.
