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
- **Lossless, atomic edits** — `write_file`, `patch_file`, `apply_diff`,
  `truncate_file` and backend writes go through a temporary file that is
  fsynced and renamed into place, keeping the file's mode. Edits preserve the
  file's encoding, byte-order mark and line endings byte for byte; a file that
  does not decode is refused rather than rewritten. `patch_file` must match
  exactly once unless told otherwise (`expected_replacements`, `replace_all`),
  and every edit refuses a file that changed since it was last read
  (`stale_file`, or pass `expected_version` / `force`).
- **Gates fail closed** — if the capability or approval check itself fails,
  the tool is not run and the model receives `error_code: "internal"`
  (`FailClosedGate`). The gates run after every other hook, so they decide on
  the input the tool will receive.

## MCP tools

Connect servers with `agenthicc mcp` (or the `/mcp` TUI command). Their tools
join the same registry and inherit the same capability, path, and approval
gates as built-ins. See [Connecting MCP servers](../guides/mcp.md).

## Project tools

Tools are class-based: implement the tool contract with an input/output
schema, add capability metadata, and register it. Project tools are discovered
from `.agenthicc/tools/` after the first TUI frame.

!!! warning "A project tool is code execution"
    A project tool file is imported only after you review it and run
    `agenthicc trust add .agenthicc/tools/<file>.py`; until then, and after any
    edit, it is skipped. Trust does not sandbox the code it admits.

## Next

- [Security](10-security.md)
- [User-defined tools](../guides/tools.md) — full authoring reference
