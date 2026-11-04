---
title: "TOOLR-04: One result and error contract, enforced across the whole tool surface"
status: Proposed
version: 1.0.0
date: 2026-11-04
scope: "the shape every tool result takes, the error-code vocabulary, how tool exceptions and registration failures surface, and one test that walks every tool agenthicc can expose"
related_prds: [TOOLR-00, TOOLR-01, TOOLR-02, TOOLR-03, TOOLR-05, TOOLR-06, SUBAGENT-09]
tags: [tools, contract, testing, effectiveness]
---

# TOOLR-04: One tool result contract

## 1. Problem

TOOLR-00 F7 and P2–P4.

- **Shapes differ by tool.**
  - Some successes carry no `ok`: `read_file`, `read_lines`, `search_files`,
    `list_directory`, `get_file_info`, `git_diff`, `git_log` and `git_status`.
    Others do.
  - `file_exists` answers `exists: false` to a *permission denial*.
  - Errors are free text with ad-hoc prefixes (`not_found: x`,
    `permission_denied: …`, `old_content not found in …`). Only some tools add
    `code` or `recoverable`.

  The model has to learn each tool's dialect. Subagent success checks
  (`result.get("ok") is False`) also miss errors that are reported without
  `ok`.
- **Exceptions escape.** An invalid regex in `grep_file` raises `PatternError`
  (verified). The same happens for:
  - a git timeout (`TimeoutError`);
  - an HTTP 404 in `fetch_page`;
  - a non-list `args` in `run_tests` (`ValueError`).

  lauren-ai turns these into a generic error string, and the TUI shows a
  traceback-shaped line.
- **Silent registration loss.** `populate_agent_tools`
  (`runners/tool_populator.py:41`) wraps each registration in
  `contextlib.suppress(Exception)`. A tool with a bad schema or a duplicate
  name disappears from the worker, with no log and no diagnostic.
- **No surface-wide test.** Capability metadata, parameter docs,
  serializability and output bounds are tested per family. The 128 workflow
  tools and the MCP adapters are not covered by any shared check.

## 2. Goals

1. **The envelope.** Every tool result is a JSON object with:

   ```text
   ok: bool                       always present
   error_code: str                present iff ok is false (vocabulary below)
   error: str                     human-readable, ≤ 1,000 chars, redacted
   retryable: bool                present iff ok is false
   truncated: bool                present iff any field was cut
   …tool-specific fields
   ```

   Lists returned bare today, such as `list_folders` and `calendar_events`,
   move under a named key.
2. **One vocabulary.** The error codes are defined once in
   `agenthicc.tools.errors.ToolErrorCode` (a `StrEnum`):
   `invalid_argument`, `not_found`, `already_exists`, `permission_denied`,
   `outside_workspace`, `approval_denied`, `blocked_by_mode`, `ambiguous_match`,
   `stale_file`, `undecodable`, `too_large`, `timeout`, `cancelled`,
   `output_limit`, `network`, `http_status`, `unavailable`, `conflict`,
   `framework_unavailable`, `sandbox_unavailable`, `internal`.

   The existing `ToolErrorKind` in `tools/executor.py` is replaced by it.
3. **No escaping exceptions.**
   - One decorator, `@tool_contract`, applied by the registration helpers,
     converts any exception a tool raises into the envelope.
   - Validation errors map to `invalid_argument`, `TimeoutError` to
     `timeout`, and other exceptions to `internal`, with the exception type in
     `error`.
   - Cancellation stays cancellation.
4. **Registration failures are loud.**
   - `populate_agent_tools` collects failures and raises in tests, and in
     production logs and publishes them.
   - A duplicate tool name is an error naming both sources.
   - A worker whose declared tool failed to register reports it in its result
     diagnostics.
5. **One contract test.** `tests/unit/test_tool_contract.py` enumerates
   every tool agenthicc can expose:
   - `AGENT_TOOLS`;
   - the browser factories;
   - the web skill tools;
   - every workflow's phase tools, via the workflow registry;
   - the subagent control tools;
   - an `AgenthiccMcpTool` wrapping a fake server.

   For each tool it asserts:
   - capability metadata is present;
   - the description and every parameter are documented;
   - the schema is a valid JSON-schema object with an object root;
   - the name is provider-safe and unique;
   - a call with an invalid argument returns the envelope with
     `invalid_argument` rather than raising.

## 3. Non-goals

- Changing what any tool does on success, beyond adding `ok`.
- Making provider-facing text identical across tools.

## 4. Acceptance criteria

1. The contract test enumerates at least 60 built-in and 100 workflow tools,
   and passes.
2. `grep_file(pattern="(")` returns `ok: false, error_code: "invalid_argument"`.
3. A git timeout, an HTTP 404 and a non-list `args` each return the envelope
   with `timeout`, `http_status` and `invalid_argument` respectively.
4. Registering two tools with the same provider name fails the test, naming
   both.
5. Every success result of the built-in tools has `ok: true`. A subagent's
   mutation-evidence hook counts an `ok: false` result from any of them as a
   failed call.
