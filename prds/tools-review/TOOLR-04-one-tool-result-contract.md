---
title: "TOOLR-04: One result and error contract, enforced across the whole tool surface"
status: Implemented with amendments (2026-11-04; tools wave 6, pending merge)
version: 1.0.0
date: 2026-11-04
scope: "the shape every tool result takes, the error-code vocabulary, how tool exceptions and registration failures surface, and one test that walks every tool agenthicc can expose"
related_prds: [TOOLR-00, TOOLR-01, TOOLR-02, TOOLR-03, TOOLR-05, TOOLR-06, SUBAGENT-09]
tags: [tools, contract, testing, effectiveness]
---

# TOOLR-04: One tool result contract

## Implementation status (2026-11-04)

Delivered in tools wave 6 (branch `tools-wave-6-result-contract`).

- **The envelope and its vocabulary (goals 1 and 2).**
  - `agenthicc.tools.errors` defines `ToolErrorCode` (a `StrEnum` of the 21
    codes) and `tool_error()`, which builds a failure with a bounded `error`
    and a default `retryable` from its code.
  - `complete_envelope()` fills in a result's missing required keys.
  - `ToolErrorKind` is deleted. `AgenthiccToolExecutor` now reports
    `ToolErrorCode` values.
  - Codes a tool used to invent are mapped to the vocabulary, with the
    specific condition kept in a `reason` field:

    | Old code | Now |
    |---|---|
    | `not_a_repository` | `not_found`, `reason: "not_a_repository"` |
    | `nothing_to_commit` | `invalid_argument`, `reason: "nothing_to_commit"` |
    | `no_match` | `not_found`, `reason: "no_match"` |
    | `remote_error` | `internal`, `reason: "remote_error"` |
    | `unsupported_content` | `undecodable`, `reason: "unsupported_content"` |
    | `pending_goals` | `conflict`, `reason: "pending_goals"` |
    | `io_error` | classified from the exception |

  - `get_file_info` and `file_exists` now return `ok: true`. `file_exists`
    reports a permission denial as `permission_denied` instead of
    `exists: false`.
  - Outlook's bare lists moved under `emails`, `folders` and `events`.
  - `recoverable` is replaced by `retryable` everywhere.
- **No escaping exceptions (goal 3).** `ToolContractHook` is the last global
  hook on every agent turn and subagent worker (`order_global_hooks`).
  - Its `on_tool_error` turns any exception into a `tool_error` result with a
    classified code; cancellation still propagates.
  - Its `after_tool_call` completes every mapping result.
  - This covers built-in, workflow, browser, web and MCP tools alike.
- **Loud registration (goal 4).**
  - `populate_agent_tools` returns `ToolRegistrationFailure`s and logs each
    one. A duplicate name names both sources.
  - The agent turn shows each failure once as a system line.
  - A worker records its failures in `SubagentResult.tool_registration_failures`
    and in its `worker_finished` / `worker_failed` event.
- **One contract test (goal 5).** `tests/unit/test_tool_contract.py` covers 93
  runtime tools: the built-ins, both browser backends, the subagent spawn and
  communication tools, and `search_web`/`fetch_page`. It checks:
  - names are provider-safe and unique;
  - descriptions and every parameter are documented (93 parameters gained
    docs);
  - schemas have an object root;
  - capability metadata is present;
  - an invalid argument, sent through lauren-ai's real executor, returns
    `invalid_argument`.

  It also checks 105 workflow phase tools statically. That check found 26
  workflow tools without capability metadata, which made them `UNDECLARED`:
  blocked in Plan, approval in Safe. They are now tagged, and the name-based
  transition fallback in `workflows/plugin.py` is deleted. A further test
  pins every literal `error_code` in `tools/`, `skills/` and `workflows/` to
  the vocabulary.

Amendments:

- **A hook, not a decorator.** Goal 3's decorator is a global hook instead.
  lauren-ai's `on_tool_error` can suppress an exception with a fallback
  result, so one hook covers tools agenthicc does not define, such as MCP
  adapters and plugin tools, without wrapping each callable.
- **Registration in tests.** Registration failures do not raise in tests.
  The contract test asserts that the session catalog registers with no
  failure, rather than switching behaviour on whether a test is running.
- **Workflow tools are checked statically.** They are built inside runners
  from run-specific state, so the contract test reads them from source rather
  than invoking each one.

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
