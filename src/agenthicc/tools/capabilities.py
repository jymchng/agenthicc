# No "from __future__ import annotations" — set_metadata runs at import time.
"""Tool capability taxonomy and pre-built capability decorators (PRD-76).

Usage::

    from agenthicc.tools.capabilities import tool_read, tool_write

    @tool_write
    @tool()
    async def write_file(path: str, content: str) -> dict:
        ...

Decorator order does not matter — @set_metadata and @tool() write to different
attributes and never interfere.  Conventional style is @set_metadata above @tool().
"""

from enum import Enum

from lauren_ai._tools import TOOL_METADATA as _TOOL_METADATA, set_metadata

__all__ = [
    "CAPABILITIES_KEY",
    "PRESENTATION_KEY",
    "ToolCapability",
    "get_tool_capabilities",
    "classify_tool_capabilities",
    "is_exploratory_tool",
    "tool_read",
    "tool_write",
    "tool_execute",
    "tool_git_read",
    "tool_git_write",
    "tool_network",
    "tool_search",
    "tool_read_search",
    "tool_network_read",
    "tool_network_write",
    "tool_network_search",
    "tool_control",
    "tool_exploratory",
]

#: Metadata key used by ToolCapabilityGate to look up capabilities.
CAPABILITIES_KEY = "capabilities"

# Presentation metadata is deliberately separate from ``CAPABILITIES_KEY``.
# Capability values participate in mode and workflow permission filtering;
# presentation tags must never change which tools can execute.
PRESENTATION_KEY = "presentation"


class ToolCapability(str, Enum):
    """Named capability tags attached to @tool()-decorated functions.

    Inherits str so frozenset members serialise as plain strings and compare
    correctly against RuntimeMode.blocked_capabilities.
    """

    READ = "read"  # reads files / data — no persistent side effects
    WRITE = "write"  # creates, modifies, or deletes files / data
    EXECUTE = "execute"  # runs shell commands or arbitrary code
    GIT_READ = "git_read"  # reads git history, diffs, status, blame
    GIT_WRITE = "git_write"  # modifies git state (add, commit, checkout, stash)
    NETWORK = "network"  # makes outbound network calls (email, REST API, etc.)
    SEARCH = "search"  # searches content without state changes
    CONTROL = "control"  # advances an internal workflow/session state machine
    UNDECLARED = "undeclared"  # internal fail-closed marker for missing metadata


# ── Single-capability decorators ──────────────────────────────────────────────

tool_read = set_metadata(CAPABILITIES_KEY, frozenset({ToolCapability.READ}))
tool_write = set_metadata(CAPABILITIES_KEY, frozenset({ToolCapability.WRITE}))
tool_execute = set_metadata(CAPABILITIES_KEY, frozenset({ToolCapability.EXECUTE}))
tool_git_read = set_metadata(CAPABILITIES_KEY, frozenset({ToolCapability.GIT_READ}))
tool_git_write = set_metadata(CAPABILITIES_KEY, frozenset({ToolCapability.GIT_WRITE}))
tool_network = set_metadata(CAPABILITIES_KEY, frozenset({ToolCapability.NETWORK}))
tool_search = set_metadata(CAPABILITIES_KEY, frozenset({ToolCapability.SEARCH}))

# ── Common multi-capability combinations ─────────────────────────────────────

tool_read_search = set_metadata(
    CAPABILITIES_KEY,
    frozenset({ToolCapability.READ, ToolCapability.SEARCH}),
)
tool_network_read = set_metadata(
    CAPABILITIES_KEY,
    frozenset({ToolCapability.NETWORK, ToolCapability.READ}),
)
tool_network_write = set_metadata(
    CAPABILITIES_KEY,
    frozenset({ToolCapability.NETWORK, ToolCapability.WRITE}),
)
tool_network_search = set_metadata(
    CAPABILITIES_KEY,
    frozenset({ToolCapability.NETWORK, ToolCapability.SEARCH}),
)
tool_control = set_metadata(CAPABILITIES_KEY, frozenset({ToolCapability.CONTROL}))

# Opt-in TUI presentation tag.  This decorator can be stacked with any
# security-capability decorator, for example ``@tool_exploratory`` above
# ``@tool_read_search``.
tool_exploratory = set_metadata(PRESENTATION_KEY, {"exploratory": True})


def get_tool_capabilities(tool: object) -> frozenset[ToolCapability]:
    """Return the ToolCapability frozenset stored on a @tool()-decorated function.

    Reads from __lauren_ai_tool_metadata__[CAPABILITIES_KEY], written by
    set_metadata(CAPABILITIES_KEY, ...).  An empty frozenset means that the
    tool has no declared capability; the runtime gates classify that case as
    ``ToolCapability.UNDECLARED`` and fail closed in Safe/Plan.
    """
    meta_dict: object = getattr(tool, _TOOL_METADATA, None) or {}
    if not isinstance(meta_dict, dict):
        return frozenset()
    capabilities = meta_dict.get(CAPABILITIES_KEY)
    if isinstance(capabilities, (set, frozenset)) and all(
        isinstance(capability, ToolCapability) for capability in capabilities
    ):
        return frozenset(capabilities)
    return frozenset()


def is_exploratory_tool(tool: object) -> bool:
    """Return whether *tool* opted into exploratory TUI presentation.

    Missing, malformed, or legacy metadata is deliberately false.  This is a
    presentation classification and is never consulted by the capability
    gate or workflow permission filters.
    """
    if getattr(tool, "exploratory", False) is True:
        return True
    metadata: object = getattr(tool, _TOOL_METADATA, None) or {}
    if not isinstance(metadata, dict):
        return False
    presentation = metadata.get(PRESENTATION_KEY)
    return isinstance(presentation, dict) and presentation.get("exploratory") is True


def classify_tool_capabilities(raw: object) -> frozenset[str]:
    """Normalize executor metadata with a conservative unknown-value policy.

    Tool decorators normally provide ``ToolCapability`` members.  The executor
    and a few integration boundaries also carry serialized string values, so
    valid strings are accepted.  Missing, empty, malformed, or unknown values
    become ``UNDECLARED``; a mixed valid/invalid declaration retains the valid
    values and adds that marker.  This keeps Safe approval and Plan blocking
    fail-closed for plugin and MCP metadata that cannot be trusted.
    """
    if not isinstance(raw, (set, frozenset)) or not raw:
        return frozenset({ToolCapability.UNDECLARED})

    known = {cap.value for cap in ToolCapability}
    values: set[str] = set()
    malformed = False
    for item in raw:
        if isinstance(item, ToolCapability):
            values.add(item.value)
        elif isinstance(item, str) and item in known:
            values.add(item)
        else:
            malformed = True
    if not values:
        return frozenset({ToolCapability.UNDECLARED})
    if malformed:
        values.add(ToolCapability.UNDECLARED)
    return frozenset(values)
