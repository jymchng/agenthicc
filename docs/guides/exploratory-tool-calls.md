# Exploratory tool-call presentation

Agenthicc can compress contiguous, successful read-only discovery calls into
one derived `Explored` row in the Rich TUI (TUX-06):

```text
● Explored 9 files, 3 searches  command.py, "def _emit" in _runner.py, …
```

The row counts the files read (a `batch_read` of three files counts three),
the searches, the listings and any other lookups, and lists each call's
summary as muted detail, cut to the terminal width. With
`[ui] tool_detail = "verbose"` (or `/details verbose`) the individual calls
follow as `└ Read command.py` rows, at most 12 of them and then
`└ …and N more calls`. This is presentation metadata only; every call remains
an individual tool event.

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

Targets displayed in the row are the calls' bounded summaries, and sensitive
argument keys are redacted. Failures and all visible non-exploratory events
close the group, and a failure prints as its own expanded row. Old session logs
without presentation metadata continue to render as individual tool rows.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Exploratory calls are shown one-by-one and you wanted a grouped view | Grouping is a presentation setting | Set `[tools] group_exploratory_calls` to the behaviour you want; the calls themselves are unaffected |
| A burst of tool calls floods the transcript | The live-call ceiling is too high for an interactive session | Lower `[tools] max_live_tool_calls` |
| An exploratory call stalls the turn | It is waiting on a timeout | The shared HTTP timeout policy applies: set `[tools] http_timeout_s` and per-operative timeouts rather than leaving them open |
| An exploratory call reads outside the workspace and is refused | The workspace boundary applies to reads too | Add the path to `[security].allowed_paths`, or approve the target in Safe mode |
| A research tool reaches a host that is not allowed | `NetworkGuard` enforces the allow-list | Extend `network_allow_list` deliberately; an empty list denies everything |
| Grouped output hides a failure | Grouping reduces visual weight, not information | Expand the group; the per-call outcome is retained |
| The same exploratory call repeats every turn | Caching is not what this feature does | Exploratory calls are not memoised; add a durable memory entry or a project tool if you need reuse |
| Output from an exploratory call is truncated | Snapshot and output sizes are bounded | Bounds are deliberate: raise the bound rather than asking for unbounded output |

### Exploratory calls are observations, not approvals

An exploratory call has no side effect to approve, which is why it can run
freely inside a phase while a write cannot. But the path and network boundaries
still apply, so "exploratory" means *read-mostly*, not *unrestricted*.

### Presentation is not policy

Every setting in this area changes how calls are displayed or bounded, never
whether a call is permitted. If you need a call to be refused, that is a
capability or mode decision — see the [security model](security.md) — not a
display setting.
