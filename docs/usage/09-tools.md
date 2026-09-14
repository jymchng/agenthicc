# Tools

agenthicc ships class-based tools with capability metadata, workspace and
network boundaries, and bounded output. Everything a tool may do is declared
through `ToolCapability` (see [Security](10-security.md)).

## Built-in tools

The filesystem group (14):

```text
append_file  copy_file  delete_file  file_exists  get_file_info
grep_files   list_directory  make_directory  move_file  patch_file
read_file    read_lines  search_files  write_file
```

The git group (11):

```text
 git_add  git_blame  git_branch  git_checkout  git_commit  git_diff
git_grep  git_log   git_show   git_stash    git_status
```

!!! note "There is no `git_push` tool"
    The git surface is read-plus-local-mutation. Pushing is not exposed as a
    tool; a network push is a `NETWORK` capability decision, and agenthicc
    does not ship one by default.

Also available: command/terminal execution with guards and deadlines, tools
contributed by connected MCP servers, browser tools (CloakBrowser or
Playwright), Outlook and document-introspection tools, and project-defined
tools discovered from `.agenthicc/tools/`.

## What is enforced on every call

- **Capability** — declared per tool; the active mode gates it.
- **Path** — workspace resolution with traversal and symlink escape
  prevention, revalidated before I/O.
- **Network** — allow-list checks against exact hostnames and subdomains.
- **Approval** — capability decisions route through the session approval
  service and its TUI overlays.
- **Deadline and cleanup** — command execution derives success only from a
  zero exit and records the deadline owner.
- **Bounded output** — results are capped so the transcript cannot flood.

## MCP tools

Connect servers with `agenthicc mcp` (or the `/mcp` TUI command). Their tools
join the same registry and inherit the same capability, path, and approval
gates as built-ins. See [Connecting MCP servers](../guides/mcp.md).

## Project tools

Tools are class-based: implement the tool contract with an input/output
schema, add capability metadata, and register it. Project tools are discovered
from `.agenthicc/tools/` after the first TUI frame.

!!! warning "A project tool is code execution"
    Review project tool files before use. The discovery path imports them
    without a trust prompt, and `agenthicc trust cli` covers `.agenthicc/cli/`,
    not `.agenthicc/tools/`.

## Next

- [Security](10-security.md)
- [User-defined tools](../guides/tools.md) — full authoring reference
