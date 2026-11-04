---
title: "TOOLR-00: Review of every tool agenthicc gives its agents"
status: Study
version: 1.0.0
date: 2026-11-04
scope: "inventory, method, findings and routing for the built-in, browser, network, MCP, workflow and control-plane tools — robustness (correct, bounded, fail-closed) and effectiveness (the model can use them well)"
related_prds: [TOOLR-01, TOOLR-02, TOOLR-03, TOOLR-04, TOOLR-05, TOOLR-06, TOOLR-07, TOOLR-08, TOOLR-09, TOOLR-10, TUIR-01, TUIR-02, TUIR-03]
tags: [tools, robustness, effectiveness, review]
---

# TOOLR-00: Review of every tool agenthicc gives its agents

Evaluated at `000eb6f` (the tip of the SUBAGENT wave stack, PRs #7–#14).
Line numbers below are at that commit. Every finding marked **verified** was
reproduced by running the shipped tool; the rest are read from the code and
say so.

## 1. Inventory

| Family | Where | Tools | Exposed to |
|---|---|---|---|
| Filesystem | `tools/fs/agent_tools.py`, `tools/fs/__init__.py` | 24: `read_file`, `read_lines`, `write_file`, `append_file`, `patch_file`, `apply_diff`, `delete_file`, `move_file`, `copy_file`, `make_directory`, `touch_file`, `truncate_file`, `file_exists`, `get_file_info`, `checksum_file`, `list_directory`, `search_files`, `grep_files`, `grep_file`, `batch_read/write/delete/move/copy` | every parent turn; filtered per subagent type |
| Execution | `tools/exec/` | 10: `shell`, `run_bash`, `run_command`, `run_python`, `run_python_expr`, `run_tests`, `wait_terminal`, `inspect_terminal`, `wait_terminal_ready`, `stop_terminal` | every parent turn |
| Git | `tools/git/` | 11: `git_status`, `git_diff`, `git_log`, `git_show`, `git_blame`, `git_grep`, `git_branch`, `git_add`, `git_commit`, `git_checkout`, `git_stash` | every parent turn |
| Outlook | `tools/outlook/` | 9: mail and calendar | every parent turn, on every platform |
| Self-inspection | `tools/introspect/` | 5 docs/source tools | every parent turn |
| MCP guidance | `tools/mcp_guidance.py` | `mcp_connection_guide` | every parent turn |
| Web | `skills/web_search.py` | `web_search` (Brave), `fetch_page` | when the skill is active |
| Browser | `tools/playwright/`, `tools/cloakbrowser/` | 9 + 9 page tools | when enabled |
| MCP servers | `tools/mcp.py`, `tools/mcp_manager.py` | one adapter per remote tool | when configured |
| Workflow phase tools | `workflows/*/` | 128 `@tool()` sites, plus `memory_write/read`, `semantic_search`, `publish_artifact` | the owning workflow's phases |
| Control plane | `subagents/`, approvals, ask-user | `spawn_subagents` and its communication/control tools | parent turns (SUBAGENT series) |

The built-in catalog alone is **60 tools, 26,772 schema characters (≈6,700
tokens) on every provider request** (measured from `TOOL_META`).

## 2. Method

1. Read every built-in tool and the code it calls end to end: argument
   handling, authorization, I/O, output bounds and result shape.
2. Read the per-call pipeline: `ToolCapabilityGate`, `ApprovalGate`,
   `_ToolOutputCaptureHook` (`runners/agent_turn.py:875`), lauren-ai's
   `ToolExecutor` hook loop, and `populate_agent_tools`.
3. Run the suspicious paths against the shipped code in throwaway
   directories: the experiments in §3 marked **verified**.
4. Compare with the mature parts of the same codebase: the browser tools
   (`cloakbrowser/policy.py`, `session.py`) already have a URL policy,
   bounded snapshots and idempotent operation ids, and they are the bar the
   other families are measured against.

## 3. Findings

Severity: **S1** corrupts data, breaks a tool for its main use, or crosses a
security boundary; **S2** makes a tool unreliable or unbounded; **S3** wastes
context or misleads the model.

### Filesystem

| # | Finding | Evidence | Sev | PRD |
|---|---|---|---|---|
| F1 | `patch_file` rewrites the whole file through `read_text(errors="replace")` / `write_text`: CRLF becomes LF and every non-UTF-8 byte becomes U+FFFD | **verified**: `b'a\r\nb\xff\r\n'` → `b'A\nb\xef\xbf\xbd\n'` (`fs/__init__.py:862`) | S1 | TOOLR-01 |
| F2 | `patch_file` replaces *every* occurrence; an ambiguous anchor silently edits many sites | `original.replace(old, new)` | S1 | TOOLR-01 |
| F3 | `apply_diff` has the same lossy round trip, no fuzz/offset search, and returns the **entire new file** in its result | `fs/agent_tools.py:385` (`"result": new_content`) | S2 | TOOLR-01 |
| F4 | `touch_file` on an existing file never updates its mtime | **verified** (`open("a")` without a write, `agent_tools.py:536`) | S2 | TOOLR-01 |
| F5 | Writes are not atomic: a crash or a full disk mid-`write_text` leaves a truncated file | `WriteFileTool` (`fs/__init__.py:303`) | S2 | TOOLR-01 |
| F6 | Nothing detects a file that changed since the model read it; a write can discard a user's or another worker's concurrent edit | no read-version check anywhere | S2 | TOOLR-01 |
| F7 | `grep_file` raises on an invalid regex instead of returning an error | **verified**: `PatternError` escapes the tool | S3 | TOOLR-04 |
| F8 | `grep_file`, `apply_diff`, `checksum_file`, `truncate_file`, `touch_file` and every `batch_*` do blocking file I/O on the event loop | direct `read_text`/`read_bytes`/backend calls in `agent_tools.py` | S2 | TOOLR-09 |
| F9 | The file cache stats the file **after** reading it, so a write landing in between is cached as fresh with old content; `get_fresh`/`store` run SQLite (with a commit) on the event loop | `file_cache.py:63,89`; read from code, not reproduced locally | S2 | TOOLR-09 |
| F10 | Reads decode with `errors="replace"`: a binary file comes back as replacement-character noise instead of "binary" | `ReadFileTool`, `ReadLinesTool` | S3 | TOOLR-10 |
| F11 | `grep_files` has no case, glob-filter or context options; `read_file`/`read_lines` return no line numbers, so the model cannot cite or edit by line reliably | schemas | S3 | TOOLR-10 |
| F12 | Discovery authorizes every path on the event loop and walks ignored trees | TUIR-00 R1–R3 | S1 | TUIR-01, TUIR-02 |

### Execution

| # | Finding | Evidence | Sev | PRD |
|---|---|---|---|---|
| E1 | `run_tests` always passes `--json-report`, a plugin agenthicc does not depend on; without it pytest exits 4 with a usage error and `run_tests` reports nothing | **verified**: `state=failed, returncode=4, passed=None` (`exec/__init__.py:720`) | S1 | TOOLR-02 |
| E2 | `run_python`, `run_python_expr` and `run_tests` use `sys.executable` — agenthicc's own interpreter — so the project's virtualenv and dependencies are invisible to them | `exec/__init__.py:570,644` | S1 | TOOLR-02 |
| E3 | Child processes inherit agenthicc's stdin; in the TUI that is the raw-mode terminal, so a command that reads stdin steals keystrokes or hangs | **verified**: `head -c 40` printed the parent's stdin | S1 | TOOLR-02 |
| E4 | Output is buffered whole by `communicate()` and truncated only after exit | **verified**: 300 MB of output → 881 MB RSS for a 64 KB result | S1 | TOOLR-02 |
| E5 | Truncation keeps the head and drops the tail, where test summaries and tracebacks are | `_truncate` (`exec/__init__.py:123`) | S3 | TOOLR-02 |
| E6 | The child gets the full agenthicc environment, including provider API keys | `effective_env = {**os.environ, ...}` (`:139`) | S2 | TOOLR-02 |
| E7 | `[security] sandbox_mode`, `max_tool_cpu_seconds`, `max_tool_memory_mb` are inert, and `ResourceLimits` is never applied | grep finds no reader | S2 | TOOLR-02 |
| E8 | `shell` and `run_bash` are the same tool under two names (2,586 schema chars) | `exec/agent_tools.py` | S3 | TOOLR-08 |
| E9 | `inspect_terminal`, `wait_terminal` and `wait_terminal_ready` are tagged `execute`: Plan mode blocks a read of a running terminal, and Safe mode asks for approval | `@tool_execute` | S3 | TOOLR-08 |

### Git

| # | Finding | Evidence | Sev | PRD |
|---|---|---|---|---|
| G1 | `git_commit` returns `hash: ""` on any branch whose name contains `-` or `.`, and on a root commit | **verified** on branch `feature-x.y` (`[\w/]+` regex) | S2 | TOOLR-03 |
| G2 | `_run_git` raises `TimeoutError` out of the tool after 30 s and leaves the process running; a slow pre-commit hook kills `git_commit` this way | `git/__init__.py:74` | S2 | TOOLR-03 |
| G3 | Git runs interactively: no `GIT_TERMINAL_PROMPT=0`, editor or pager overrides, and stdin is inherited (E3) | same | S2 | TOOLR-03 |
| G4 | Ref and branch arguments are not separated from options (`git_checkout(branch="--orphan=x")`) | `git_checkout`, `git_show`, `git_diff` | S2 | TOOLR-03 |
| G5 | `git_diff` counts `+++`/`---` headers as additions/deletions; output is cut by characters with no `truncated` flag; `git_log` declares a `format` parameter it ignores | `:176-177`, `:195` | S3 | TOOLR-03 |

### Network, MCP and the pipeline

| # | Finding | Evidence | Sev | PRD |
|---|---|---|---|---|
| N1 | `fetch_page` ignores `security.network_allow_list`, follows redirects anywhere, can reach loopback, private and metadata addresses, reads the whole body into memory, and returns unbounded text | `skills/web_search.py:181`; contrast `cloakbrowser/policy.py` | S1 | TOOLR-05 |
| N2 | `agenthicc_http_client()` has no policy hook, so every caller must remember the allow-list, and none do | `tools/http.py` | S2 | TOOLR-05 |
| N3 | HTTP status errors are not "network errors", so a 404 from `fetch_page` or a 401 from Outlook raises out of the tool | `_is_network_error`, `_safe_call` | S3 | TOOLR-05 |
| N4 | The Outlook tools (9 tools, ≈1,500 tokens) are offered on Linux with no `MSGRAPH_TOKEN`, where every call fails | `outlook/agent_tools.py` | S3 | TOOLR-08 |
| M1 | MCP results are unbounded; image blocks reach the model as raw base64 text; resource blocks become `str(block)` | `mcp.py:161` | S2 | TOOLR-06 |
| M2 | MCP input schemas are passed to providers unmodified; one tool using `$ref`, `oneOf` at the top level, or a non-object root breaks the whole request | no sanitizer | S2 | TOOLR-06 |
| M3 | A server that crashes mid-session stays "not connected" until a manual reload | `call_tool` raises; no reconnect on call | S3 | TOOLR-06 |
| P1 | lauren-ai ignores an exception raised by a `before_tool_call` hook and **runs the tool**, so a bug in `ToolCapabilityGate` or `ApprovalGate` bypasses the gate | `lauren_ai/_tools/_executor.py` before-hook loop (`continue`) | S1 | TOOLR-07 |
| P2 | `populate_agent_tools` drops a tool that fails to register under `contextlib.suppress(Exception)`, with no diagnostic | `runners/tool_populator.py:41` | S2 | TOOLR-04 |
| P3 | Result shapes differ by tool: successes omit `ok` (`read_file`, `search_files`, `git_diff`, `git_log`), errors are free-text prefixes (`not_found: …`, `permission_denied: …`), and only some carry `recoverable` | across families | S3 | TOOLR-04 |
| P4 | No test walks the whole tool surface: capability metadata, parameter docs, JSON-serializable and bounded results are checked per family, if at all | test inventory | S2 | TOOLR-04 |
| P5 | Self-inspection search walks and reads agenthicc's source on the event loop | `introspect/__init__.py:751` | S3 | TOOLR-09 |

### What is already good

- Path authorization is thorough. Every adapter revalidates its canonical target
  before I/O, and symlink escapes are decided by the mode-aware policy. TUIR-01
  makes this cheaper without weakening it.
- Command cancellation and timeouts kill the whole process group, survive
  cancellation during spawn, and report a structured `CommandOutcome`.
- The browser tools are the reference design: a URL policy with DNS
  revalidation, action and page limits, bounded snapshots, redaction, and
  idempotent operation ids.

## 4. The PRDs

| PRD | Title | Fixes | Priority |
|---|---|---|---|
| [TOOLR-01](TOOLR-01-lossless-file-edits.md) | Lossless, exact and atomic file edits | F1–F6 | P0 |
| [TOOLR-02](TOOLR-02-command-execution-hygiene.md) | Command execution that is isolated, bounded and runs the project's own interpreter | E1–E7 | P0 |
| [TOOLR-03](TOOLR-03-git-tool-correctness.md) | Git tools that are non-interactive, bounded and exact | G1–G5 | P1 |
| [TOOLR-04](TOOLR-04-one-tool-result-contract.md) | One result and error contract, enforced across the whole tool surface | F7, P2–P4 | P1 |
| [TOOLR-05](TOOLR-05-network-tools-under-one-policy.md) | Every network tool under one destination policy | N1–N3 | P0 |
| [TOOLR-06](TOOLR-06-mcp-results-and-schemas.md) | MCP results, schemas and reconnection | M1–M3 | P1 |
| [TOOLR-07](TOOLR-07-gates-fail-closed.md) | Capability and approval gates fail closed | P1 | P0 |
| [TOOLR-08](TOOLR-08-tool-surface-curation.md) | A smaller, accurate tool surface | E8, E9, N4 | P2 |
| [TOOLR-09](TOOLR-09-tool-io-off-the-event-loop.md) | Tool I/O off the event loop and a race-free file cache | F8, F9, P5 | P1 |
| [TOOLR-10](TOOLR-10-search-and-read-effectiveness.md) | Search and read tools the model can use precisely | F10, F11 | P2 |

**Order:** start with TOOLR-07, which is small and closes a security hole.
Then TOOLR-01, TOOLR-02 and TOOLR-05, the data-loss, broken-tool and
boundary fixes. Then TOOLR-04, whose contract test keeps the rest honest.
Then TOOLR-03, TOOLR-06 and TOOLR-09, and finally TOOLR-08 and TOOLR-10.
TUIR-01 and TUIR-02 (`prds/tui-responsiveness/`) own discovery cost and
compose with TOOLR-09 and TOOLR-10.
