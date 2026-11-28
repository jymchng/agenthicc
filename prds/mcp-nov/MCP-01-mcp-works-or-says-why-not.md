---
title: "MCP-01: MCP works out of the box, or says exactly why it does not"
status: Implemented
version: 1.0.0
date: 2026-11-28
priority: P0
related_prds:
  - MCP-00
  - MCP-03
  - MCP-07
  - prd-172
  - prd-191
  - TOOLR-06
---

# MCP-01 — MCP works out of the box, or says exactly why it does not

## Incident

On 2026-11-28 an operator ran a goal flow that depends on an MCP server named
`opengame` (a `$opengame` skill). Four tools ran, nothing changed, and the
operator interrupted it after 45 seconds. The only sign of the cause was a
one-line notice and, behind `/mcp reload`, this status:

```text
! MCP server 'opengame' is unavailable; continuing without its tools.   /mcp
"status": "failed",  "tool_count": 0,  "transport": "stdio",
"last_error": "lauren_mcp is not installed. Install it with: pip install lauren-mcp"
```

Nothing was wrong with the server or its configuration. The MCP client library
was missing from the environment agenthicc ran in. The agent kept going without
the tools it had been asked to use.

## Evidence

Every row names the file that proves it. Line numbers are at `main` `7174c3a`.

| # | What is true today | Where |
|---|---|---|
| 1 | `lauren-mcp` is in the optional `mcp` extra, not in `dependencies` and not in `dev`. | `pyproject.toml:30-33` |
| 2 | CI and every nox session install only `--extra dev`, so **no CI job ever imports `lauren_mcp`**. The project's own `.venv` raises `ModuleNotFoundError: No module named 'lauren_mcp'`. | `noxfile.py:30`; checked 2026-11-28 |
| 3 | The tests that touch MCP use stand-ins ("lauren_mcp not installed — give bridge._client raw objects"). The one real-server test is skipped unless `AGENTHICC_MCP_E2E` is set and `npx` exists. | `tests/integration/conftest.py:22-45`; `tests/e2e/test_mcp_e2e.py:1-14` |
| 4 | `mypy` ignores missing imports for `agenthicc.tools.mcp`, so the bridge is type-checked **without** the library it calls. | `pyproject.toml:136-147` |
| 5 | The import guard sets a flag; `connect()` raises `ImportError("lauren_mcp is not installed. Install it with: pip install lauren-mcp")`. That command installs into whichever interpreter `pip` points at, omits the `[http,ws]` transports the project pins, and is not the command the guide gives (`agenthicc[mcp]`). | `tools/mcp.py:66-73`, `:251-254`; `docs/guides/mcp.md:8-18` |
| 6 | The failure is a normal `failed` state, not retried (the import check runs before the retry loop), and surfaced as `MCP server 'x' is unavailable; continuing without its tools.` The cause is not in the notice. | `tools/mcp_manager.py:1045`, `tui/workspace/notice.py:7` |
| 7 | Nothing connects a skill, agent or workflow to the servers it needs, so a flow proceeds without them. (Owned by MCP-07.) | no `mcp` reference in `skills/`, `agents/` or `workflows/plugin.py` |
| 8 | `lauren-mcp` adds three small packages to a base install: itself, `httpx-sse` and `websockets`. `httpx`, `anyio` and `lauren` are already present. | `uv.lock` (`lauren-mcp 1.2.1`) |
| 9 | The specification's current revision is `2026-07-28`, and its compatibility matrix says a legacy client against a modern-only server **fails** with no fall-forward. `lauren-mcp`'s client is legacy-era, so the same one-line "unavailable" would be the only symptom of that failure too (MCP-13). | MCP specification, Versioning and Compatibility; `lauren-mcp` `_mcp_version.py` |
| 10 | `tools/__init__.py` exports MCP names lazily and the session manager is built only when `[[tools.mcp_servers]]` is non-empty, so a base dependency would not enter the first-prompt module budget of a project with no servers. | `tools/__init__.py:95-107`; `runners/tui_session.py:1416` |

## Goals

1. A user who installs agenthicc and configures an MCP server gets a working
   client without a second install step.
2. When MCP cannot work, every surface says **what is missing, in which
   interpreter, and the exact command that fixes it**, once, and before work that
   needs it.
3. The real client is imported and exercised by CI, so this cannot recur unseen.

## Non-goals

- Changing how servers are configured (MCP-12) or classified (MCP-06).
- Making a missing dependency a silent success. A missing client is an error.
- Bundling an MCP server.

## Design

### D1 — Make the client a base dependency (recommended)

Move `lauren-mcp[http,ws]` from the `mcp` extra into `dependencies`, with the
floor chosen by MCP-02. Keep `agenthicc[mcp]` as an empty alias for one release so
the documented command keeps working, then remove it (no shim stays: this project
is not in production).

| Option | Cost | Why not / why |
|---|---|---|
| **Base dependency (recommended)** | three small packages; no import cost until a server is configured (row 10) | MCP is presented as a built-in surface: `/mcp`, `agenthicc mcp`, the config template and the `mcp_connection_guide` tool all exist in a base install |
| Keep the extra | users hit this incident; every surface needs a failure path | acceptable only if D2–D4 are done and the extra is named in the first-run output |

The decision belongs to the maintainer; D2–D5 are required either way.

### D2 — One typed error with a cause and a fix

Replace the bare `ImportError` with `McpUnavailable(reason, package, fix)`,
raised before any connection attempt. `reason` is `dependency_missing`,
`dependency_incompatible` (MCP-02 version floor) or `protocol_era_mismatch` (the server
speaks only the current protocol revision and the installed client only the legacy one;
MCP-13 D3 supplies the text). `fix` is built from
`sys.executable`, for example
`"<sys.executable> -m pip install 'agenthicc[mcp]'"` and the `uv` equivalent
(`uv pip install --python <sys.executable> 'agenthicc[mcp]'`), so it targets the
environment agenthicc is running in, not whichever `pip` is first on `PATH`.

### D3 — Every surface carries the cause

| Surface | Today | After |
|---|---|---|
| Transcript notice | `unavailable; continuing without its tools` | adds the cause on one line: `… (lauren-mcp is not installed; run /mcp doctor)` |
| `/mcp status`, `mcp list --json` | state `failed`, `last_error` | adds `client_available: false` and `fix` |
| `agenthicc mcp doctor` | per-server | first check is the dependency; exits non-zero when it fails |
| Status line | nothing | a `mcp ✗` badge while any enabled server is `failed` or `needs_auth` |
| Headless / background worker | log line | the same notice, recorded as a session event so it shows in the session's transcript and `agenthicc agents` |

A missing client is reported **once per session**, not once per server.

### D4 — Fail at the right time

- A server with `required = true` already stops startup (`McpRequiredServerError`).
  A missing client counts as that failure.
- A server that is not required still lets the session start, and the notice
  names the cause. What a flow does when a server it depends on is unavailable is
  MCP-07's contract, not this one's.

### D5 — CI imports the real client

- Add `agenthicc[mcp]` to the `dev` extra, so `uv sync --extra dev` (CI and nox)
  installs `lauren-mcp`.
- Remove the `agenthicc.tools.mcp` entry from the mypy `ignore_missing_imports`
  override, so the bridge is checked against the library's types.
- Add one unit test that fails if `import lauren_mcp` fails in the test
  environment, so the extra cannot be dropped silently.

## Failure handling

- An environment with an old client (`dependency_incompatible`): same path, with
  the installed and required versions in the message.
- `uv sync` without extras on a checkout: `CLAUDE.md` already warns that an exact
  sync uninstalls extras. After D1 there is nothing to uninstall.

## Security

None new. The fix text names the interpreter, never a URL, and carries no
configuration values.

## Rollout and migration

1. Land D5 first (CI installs the client) so the rest is tested against it.
2. Land D1 with D2–D4.
3. Update `docs/guides/mcp.md` "Install MCP support", `README.md`, `llms-full.txt`
   and `CHANGELOG.md` in the same change; `grep -rn "pip install lauren-mcp"
   src docs README.md llms-full.txt` returns nothing.

## Acceptance criteria

1. In an environment without `lauren-mcp`, `agenthicc mcp doctor` exits non-zero
   and prints the missing package and a command that begins with the running
   interpreter's path.
2. The transcript notice for an unavailable server contains its cause on the same
   line, and `/mcp status` shows `client_available`.
3. With D1, `uv sync` (no extras) followed by `python -c "import lauren_mcp"`
   succeeds, and `tui.first_prompt.modules` is unchanged for a project with no
   servers (`scripts/perf_budget.py --scope ci --check`).
4. CI's unit job imports `lauren_mcp`; the mypy override for `agenthicc.tools.mcp`
   is gone and `mypy src/agenthicc` passes.
5. `grep -rn "pip install lauren-mcp" src docs README.md llms-full.txt` finds
   nothing.

## Verification

- Unit: force the import to fail (monkeypatch `sys.modules`), assert
  `McpUnavailable`, the notice text, the doctor exit code and `client_available`.
- Integration: start a session with one configured server and the client hidden;
  assert exactly one notice.
- Mutation: replace the notice text with the old string; the test must fail.

## Risks

- Moving to a base dependency raises the install floor for users who never use
  MCP. Three small packages is the measured cost (row 8); the import is lazy.
- If maintainers keep the extra, the incident above remains possible and D3 is the
  only protection. That is a decision, not an oversight, and should be recorded.

## Implementation record

Implemented on `fix/mcp-01-client-availability`.

| Item | What shipped | Where |
|---|---|---|
| D1 | `lauren-mcp[http,ws]>=1.2.1,<2` is a base dependency. The `mcp` extra is **removed outright, with no empty alias**: this project is not in production and keeps no compatibility shims (the rollout note above suggested a one-release alias). The `lauren-ai[mcp]` pin it carried is gone; nothing imports it. | `pyproject.toml` |
| D2 | `McpUnavailable` carries an `McpClientStatus` (`reason` is `dependency_missing`, `dependency_incompatible` or `dependency_broken`; `protocol_era_mismatch` is MCP-13's). `fix_command()` names `sys.executable` and the `uv pip install --python` equivalent. It is raised before the retry loop. | `tools/mcp_availability.py`, `tools/mcp.py` |
| D3 | The notice names the cause and carries `Fix:` in its body, once per startup generation. `client_available`, `reason` and `fix` are on `McpServerStatus`, `/mcp status` and `mcp list --json` (as a `client` section). `mcp doctor` checks the client first and exits 1. The status line shows `mcp ✗` through `ConversationStore.mcp_attention`. | `tools/mcp_manager.py`, `cli/commands/mcp.py`, `tui/workspace/components.py` |
| D4 | A missing client is a failed server like any other, so a `required = true` server stops startup. | `tools/mcp_manager.py` |
| D5 | `dev` no longer needs the extra because the client is a base dependency, so CI imports it. The mypy override for `agenthicc.tools.mcp` is removed and `mypy src/agenthicc` passes against the library's types. `test_the_real_client_library_imports_and_meets_the_floor` fails if the import fails, and a second test keeps `REQUIREMENT` equal to `pyproject.toml`. | `tests/unit/test_mcp_availability.py` |

Against the released client the bridge now calls the real signatures: `call_tool(name,
arguments)` takes no call id, and the stdio launch passes `cwd` and `env` with no
`TypeError` fallback (which could have started the child with the parent's whole
environment; 1.2.0 added both arguments, so the floor is 1.2.1).

Not done here: the headless/background worker event (D3, last row) reuses the same
`mcp_server_failure` notice through the conversation store; a separate durable session
event for `agenthicc agents` is not added. `tui.first_prompt.modules` measured 380 on
`main` before this change (budget 377, 0% tolerance), so the budget was raised to 400 at
the maintainer's direction rather than the lazy import being the cause.
