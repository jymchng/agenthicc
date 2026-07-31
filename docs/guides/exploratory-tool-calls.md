# Exploratory tool-call presentation

Agenthicc can compress contiguous, successful read-only discovery calls into a
derived `Explored` block in the Rich TUI:

```text
● Explored
  └ Read command.py
  └ Search def _emit in _runner.py
```

When one read call contains several files, the first two are shown and the
remaining count is explicit, for example `Read one.py, two.py, and 3 more
files.`. This is presentation metadata only; every file result remains an
individual tool event.

This is presentation-only. The executor still runs each call separately, the
model receives each normal tool result, and the conversation store, session
log, workflow state, and replay path retain one event per call.

## Enable it

The rollout flag is enabled by default. It can be set explicitly in the
project or user TOML configuration:

```toml
[tools]
group_exploratory_calls = true
```

Set it to `false` to restore the existing individual tool rows. This setting
does not grant capabilities, bypass approvals, or change tool availability.

## Classification

Built-in filesystem readers, search/inspection tools, and read-only git
inspection tools are explicitly marked. Mutations, command execution,
terminal lifecycle, browser interactions, network tools, workflow controls,
and unknown tools remain individual by default.

Project and workflow callables can opt in when their result is genuinely
read-only:

```python
from lauren_ai._tools import tool
from agenthicc.tools.capabilities import tool_exploratory, tool_read

@tool_exploratory
@tool_read
@tool()
async def inspect_manifest(path: str) -> dict[str, object]:
    """Read a manifest without changing project state."""
    ...
```

Class-based tools may set `exploratory = True`. The marker is independent of
security capability metadata; omitting it is the safe default.

Targets displayed in the block are bounded and sensitive argument keys are
redacted. A group is capped at 12 visible children with an explicit overflow
count. Failures and all non-exploratory events flush the group and remain
prominent. Old session logs without presentation metadata continue to render
as individual tool rows.
