# Tools

agenthicc ships class-based tools with capability metadata, workspace and
network boundaries, and bounded output. Everything a tool may do is declared
through `ToolCapability` (see [Security](10-security.md)).

## Built-in tools

The core filesystem tools (the group also has `apply_diff`, `checksum_file`,
`touch_file`, `truncate_file` and the `batch_*` tools):

```text
append_file  copy_file  delete_file  get_file_info  grep_files
list_directory  make_directory  move_file  patch_file  read_file
search_files  write_file
```

- `read_file` returns numbered lines (`<n>\t<line>`, numbers right-aligned to
  one width) with `total_lines` and `version`. `offset` and `limit` read a
  range; `line_numbers=false` returns the raw text. A long read stops at a
  whole line and returns `next_offset`. A file with a NUL byte in its first
  8 KB, or that is not UTF-8, returns `binary: true` with its size and MIME
  type instead of replacement characters; pass `encoding` to read it as text.
- `grep_files` searches a directory or one file. `glob` and `exclude` filter
  files; `case_sensitive` defaults to smart case (insensitive unless the
  pattern has a capital); `fixed_string` matches literally; `context_lines`
  (up to 5) adds lines around each match; `output` is `matches`, `files` or
  `count`. It always reports `total_matches` and `files_matched`, and
  `truncated` when `max_results` cut the list. When `rg` is on `PATH` it runs
  the search, over the same files and with the same result; a pattern `rg`
  cannot compile falls back to Python.
- `search_files` takes `sort: "mtime"` to list the most recently changed
  files first.
- `get_file_info` on a missing path returns `ok: true, exists: false`.
- `run_bash` is the one shell tool. The terminal observation tools
  (`inspect_terminal`, `wait_terminal`, `wait_terminal_ready`) are `read`
  capability, so Plan and Safe mode let an agent watch a terminal it started;
  `stop_terminal` is `execute`.

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

A tool group that cannot work in this environment is left out of the catalog
and named in one line of the system prompt's tool section, so the model can
tell you what to configure. The Outlook tools need `MSGRAPH_TOKEN`, or Windows
with `pywin32`; without either the section says
`Outlook / Calendar: unavailable (set MSGRAPH_TOKEN)`.

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
- **Command hygiene** — children get `/dev/null` as stdin, a scrubbed
  environment, optional CPU and memory limits, and bounded head-and-tail
  capture (see [configuration](../guides/configuration.md#tools-and-mcp)).
  `run_tests` reads pytest's built-in JUnit XML report and returns passed,
  failed, errors and skipped counts and the first 20 failures. No plugin is
  needed.
- **Non-interactive git** — git runs without a pager, editor or credential
  prompt, under a deadline that kills it. Refs and branches that look like
  options are refused. `git_commit` returns the hash from `git rev-parse HEAD`,
  and `git_diff` counts from `--numstat`.
- **Bounded discovery** — `search_files`, `grep_files` and `list_directory`
  walk once in a worker thread. They skip ignored directories before entering
  them (`.gitignore`, or `node_modules`, `.venv`, `__pycache__`… outside git;
  `include_ignored=true` bypasses this). They stop at 1,000 results, 50,000
  examined entries or 10 seconds, and say which. At most `[tools]
  max_fs_walks` walks run at once. In-scope paths need no per-file check;
  only a symlink pointing outside the workspace goes to the approval policy.
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
