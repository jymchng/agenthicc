---
title: "TOOLR-08: A smaller, accurate tool surface"
status: Proposed
version: 1.0.0
date: 2026-11-04
scope: "which tools a turn is offered: duplicates, tools that cannot work in this environment, capability tags that misclassify read-only tools, and the per-request cost of the catalog"
related_prds: [TOOLR-00, TOOLR-02, TOOLR-04, PRD-125, PRD-133]
tags: [tools, effectiveness, context-budget]
---

# TOOLR-08: A smaller, accurate tool surface

## 1. Problem

TOOLR-00 E8, E9 and N4. Every parent turn is offered 60 built-in tools:
26,772 schema characters, about 6,700 tokens, on every provider request,
before workflow, subagent, browser and MCP tools. More tools of overlapping
purpose also make tool choice worse.

- **Duplicates.** `shell` and `run_bash` are the same function with different
  docstrings (2,586 characters together). `grep_file` overlaps `grep_files`
  with a `path` that names a file. `file_exists` overlaps `get_file_info`.
- **Tools that cannot work here.** The nine Outlook tools are offered on every
  platform. On Linux without `MSGRAPH_TOKEN`, every call fails, and the model
  only learns this by trying.
- **Misclassified capabilities.** `inspect_terminal`, `wait_terminal` and
  `wait_terminal_ready` observe a terminal the agent already started, yet they
  are tagged `execute`. Plan mode blocks them, and Safe mode asks for approval
  to *read* a terminal's output.
- **No signal of what is unavailable.** A tool that is configured off is
  simply absent, so the model cannot tell "not installed" from "denied by
  mode".

## 2. Goals

1. **Remove duplicates.**
   - `shell` is deleted; `run_bash` is the one shell tool.
   - `grep_file` folds into `grep_files`, where a `path` naming a file searches
     that file with `context_lines` (TOOLR-10).
   - `file_exists` folds into `get_file_info`, which returns `exists: false`
     with `ok: true` for a missing path.

   The removed names are deleted, not aliased (no backwards-compat shims), and
   subagent types, workflow phase allow-lists and docs are updated in the same
   change.
2. **Offer only what can work.** Each tool group declares an `available()`
   predicate, evaluated once per session:
   - Outlook: Windows with `pywin32`, or `MSGRAPH_TOKEN` set;
   - `web_search`: a Brave key;
   - browser groups: their existing health checks.

   Unavailable groups are left out of the catalog. The system prompt's tool
   section lists them in one line with the reason, so the model can tell the
   user what to configure.
3. **Accurate capabilities.** Terminal observation tools carry `read`; only
   `stop_terminal` keeps `execute`. A test pins every built-in tool's
   capability set against a reviewed table, so a change is deliberate.
4. **A catalog budget.** A test measures the built-in catalog's schema size
   and fails above a ratcheted ceiling. That ceiling is set to the size after
   this PRD and recorded beside the AGX-14 performance budgets, so growth
   needs an explicit decision.
5. **Shorter descriptions where safe.** The three execution tools share one
   description fragment for `background`, `lifecycle` and `readiness`, instead
   of repeating it in each schema.

## 3. Non-goals

- Dynamic, per-message tool selection or retrieval. This is a static curation;
  retrieval is a separate design.
- Changing workflow-phase tool sets beyond renames.

## 4. Acceptance criteria

1. On Linux without `MSGRAPH_TOKEN`, no Outlook tool is in the provider
   request, and the tool section says "Outlook: unavailable (set
   MSGRAPH_TOKEN)".
2. `shell`, `grep_file` and `file_exists` appear nowhere in `src/`, `docs/` or
   `llms-full.txt`. `grep_files` on a file path returns per-line matches with
   context.
3. In Plan mode, `inspect_terminal` on an owned terminal succeeds without an
   approval prompt.
4. The built-in catalog is at least 15 % smaller than 26,772 characters, and
   the ratchet test pins the new size.
