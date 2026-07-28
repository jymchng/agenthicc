"""@tool() wrappers for agenthicc self-inspection — docs and source code.

These let the agent read the *current* agenthicc documentation and source instead
of relying on prose in a system prompt that goes stale as soon as the code
changes.  All five are read-only and bounded, so they stay available in
capability-restricted modes such as Plan.

NOTE: no ``from __future__ import annotations`` — @tool() inspects real annotations.
"""

from lauren_ai._tools import tool

from agenthicc.tools.capabilities import tool_read, tool_read_search

__all__ = [
    "inspect_agenthicc_source",
    "list_agenthicc_docs",
    "read_agenthicc_doc",
    "search_agenthicc_docs",
    "search_agenthicc_source",
    "INTROSPECT_AGENT_TOOLS",
]


@tool_read
@tool()
async def list_agenthicc_docs(section: str = "") -> dict[str, object]:
    """List agenthicc's own documentation: path, title, and size of every document.

    Start here when you need to know how agenthicc itself works — its
    architecture, workflows, tools, memory, security, configuration, or CLI. The
    listing includes the guides and reference pages plus `llms.txt`,
    `llms-full.txt` (the complete API reference), and `README.md`. Read one with
    read_agenthicc_doc(path), or jump straight to a phrase with
    search_agenthicc_docs(query).

    Args:
        section: Optional path prefix to filter by, e.g. "guides" or "reference".
    """
    from agenthicc.tools.introspect import docs_root, list_docs  # noqa: PLC0415

    if docs_root() is None:
        from agenthicc.tools.introspect import DOCS_DIR_ENV  # noqa: PLC0415

        return {
            "ok": False,
            "error": "No agenthicc documentation tree is available in this installation.",
            "fix": (
                f"Set {DOCS_DIR_ENV} to a directory containing index.md, or use "
                "inspect_agenthicc_source to read the code directly."
            ),
        }

    entries = list_docs(section)
    return {
        "ok": True,
        "section": section,
        "count": len(entries),
        "documents": [entry.as_dict() for entry in entries],
    }


@tool_read
@tool()
async def read_agenthicc_doc(
    path: str, start_line: int = 1, max_lines: int = 400
) -> dict[str, object]:
    """Read one agenthicc documentation page, a bounded window at a time.

    Use a path from list_agenthicc_docs, e.g. "guides/workflows.md",
    "reference/kernel.md", or "llms-full.txt". When the result reports
    truncated=True, call again with start_line=next_start_line to continue.

    Args:
        path: Document path relative to the documentation root.
        start_line: 1-based first line to return (default 1).
        max_lines: Maximum lines to return (default 400, hard cap 2000).
    """
    from agenthicc.tools.introspect import read_doc  # noqa: PLC0415

    return read_doc(path, start_line=start_line, max_lines=max_lines)


@tool_read_search
@tool()
async def search_agenthicc_docs(
    query: str, max_results: int = 40, section: str = ""
) -> dict[str, object]:
    """Search agenthicc's documentation for a phrase and return matching lines.

    Faster than reading whole pages when you know what you are looking for —
    "capability gate", "on_reject", "authoring_max_phase_turns". Each match
    reports its document path and line number, so follow up with
    read_agenthicc_doc(path, start_line=<line>).

    Args:
        query: Regular expression, or a plain phrase when it is not valid regex.
        max_results: Maximum matches to return (default 40, hard cap 200).
        section: Optional path prefix to restrict the search to.
    """
    from agenthicc.tools.introspect import search_docs  # noqa: PLC0415

    return search_docs(query, max_results=max_results, section=section)


@tool_read
@tool()
async def inspect_agenthicc_source(target: str, include_source: bool = True) -> dict[str, object]:
    """Read the source of any agenthicc module, class, function, or method.

    The authoritative answer to "what does this actually do" and "what fields
    does this really have". Accepts a module ("agenthicc.kernel.reducer"), a
    symbol ("agenthicc.workflows.plugin:PhaseSpec"), a method
    ("agenthicc.workflows.plugin:WorkflowPlugin.build_runner"), or the all-dots
    form. Private names work too. Returns the file, line range, signature,
    docstring, source, and — for a module or class — an outline of its members.

    Nothing is imported, so this is safe for modules with optional dependencies.
    Pass include_source=False for a cheap outline of a large module first.

    Args:
        target: Dotted module path, optionally with ":Symbol" or ":Class.method".
        include_source: Include the source text (default True).
    """
    from agenthicc.tools.introspect import resolve_source  # noqa: PLC0415

    return resolve_source(target, include_source=include_source)


@tool_read_search
@tool()
async def search_agenthicc_source(
    query: str, max_results: int = 40, module: str = ""
) -> dict[str, object]:
    """Search agenthicc's own source code for a pattern and return matching lines.

    Use this to locate a symbol before inspecting it, or to find every call site
    of something. Each match reports a package-relative path and line number;
    follow up with inspect_agenthicc_source to read the definition.

    Args:
        query: Regular expression, or a plain substring when it is not valid regex.
        max_results: Maximum matches to return (default 40, hard cap 200).
        module: Optional dotted module or subpackage to restrict the search to,
            e.g. "agenthicc.workflows".
    """
    from agenthicc.tools.introspect import search_source  # noqa: PLC0415

    return search_source(query, max_results=max_results, module=module)


#: All agenthicc self-inspection tools, in the order they are presented.
INTROSPECT_AGENT_TOOLS = [
    list_agenthicc_docs,
    read_agenthicc_doc,
    search_agenthicc_docs,
    inspect_agenthicc_source,
    search_agenthicc_source,
]
