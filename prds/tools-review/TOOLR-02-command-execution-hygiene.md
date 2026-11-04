---
title: "TOOLR-02: Command execution that is isolated, bounded and runs the project's own interpreter"
status: Proposed
version: 1.0.0
date: 2026-11-04
scope: "run_bash/shell, run_command, run_python, run_python_expr and run_tests: stdin, output capture, environment, resource limits, interpreter selection and test reporting"
related_prds: [TOOLR-00, TOOLR-04, TOOLR-08, PRD-149, PRD-151]
tags: [tools, exec, robustness, security]
---

# TOOLR-02: Command execution hygiene

## 1. Problem

TOOLR-00 E1–E7. The execution tools have careful process-group cleanup, but
everything around the child process is loose.

- **`run_tests` does not work (verified).** It always adds `--json-report
  --json-report-file=…` (`tools/exec/__init__.py:720`). `pytest-json-report`
  is not a dependency, so pytest exits 4 with "unrecognized arguments" and the
  tool returns `passed=None, failed=None`. The regex fallback never runs,
  because there is no summary to parse.
- **The wrong interpreter.** `run_python`, `run_python_expr` and `run_tests`
  run `sys.executable`, which is agenthicc's interpreter. When agenthicc is
  installed with `uv tool`, `pipx` or in its own venv, the project's
  dependencies are not importable. Its tests then fail with `ImportError`, or
  run against the wrong library versions.
- **Inherited stdin (verified).** `_run_proc` and `create_subprocess_shell`
  pass no `stdin`, so the child shares agenthicc's stdin. Under the TUI that
  is the raw-mode terminal. A command that reads stdin — a `read`, an
  interactive installer, `python` waiting for input, a pager — steals the
  user's keystrokes or hangs until the timeout.
- **Unbounded capture (verified).** `communicate()` buffers all output before
  `_truncate` keeps 64 KB. A command printing 300 MB took the process to
  881 MB RSS. A verbose build or a runaway loop can exhaust memory before its
  timeout.
- **Head-only truncation.** `_truncate` keeps the first 64 KB. Test failure
  summaries, compiler errors and tracebacks are at the end.
- **Secrets in the environment.** `effective_env = {**os.environ, **env}`
  hands every child `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `MSGRAPH_TOKEN` and
  the rest. Output redaction catches only patterns it recognises, and the
  child can send the values anywhere.
- **Inert limits.** `[security] sandbox_mode`, `max_tool_cpu_seconds` and
  `max_tool_memory_mb` are parsed nowhere, and `tools.sandbox.ResourceLimits`
  is never applied. CLAUDE.md calls an inert configuration field a bug.

## 2. Goals

1. **No shared stdin.** Every child gets `stdin=DEVNULL`, unless a tool
   explicitly takes input (`run_command(..., stdin="text")`, delivered once
   and then closed). Children also get `TERM=dumb`, `PAGER=cat`,
   `GIT_PAGER=cat`, `GIT_TERMINAL_PROMPT=0` and `CI=1`, so tools do not open
   pagers or prompts.
2. **Streaming, bounded capture.** stdout and stderr are read incrementally:
   - each stream keeps a head (16 KB) and a rolling tail (48 KB) buffer, so
     memory per command is O(64 KB) regardless of output size;
   - the result reports `stdout_bytes` and `stderr_bytes`, and when cut
     includes `"…[N bytes omitted]…"` between the head and the tail;
   - a stream past `exec.max_output_bytes` (default 64 MB) is stopped, with
     `termination_reason="output_limit"`, so a runaway writer cannot run until
     its timeout.
3. **A scrubbed environment.** The child environment starts from
   `os.environ` minus a deny-list:
   - provider API keys;
   - `*_TOKEN`, `*_SECRET`, `*_PASSWORD` and `*_API_KEY`;
   - agenthicc's own `AGENTHICC_*` control variables.

   `exec.env_passthrough` (a list of names) re-admits a variable for projects
   that need one. The model's `env` overlay may add variables, never re-admit
   a denied one.
4. **The project's interpreter.** One resolver, `project_python(cwd)`, picks,
   in order:
   1. an explicit `exec.python`;
   2. the active `VIRTUAL_ENV`;
   3. `.venv/bin/python` (or `Scripts\python.exe`) at or above the cwd, within
      the workspace;
   4. `uv run python` when a `pyproject.toml` with a `uv.lock` is present;
   5. otherwise `python3` on `PATH`, and `sys.executable` only as a last
      resort.

   `run_python`, `run_python_expr` and `run_tests` use it, and the result
   names the interpreter used.
5. **`run_tests` that reports without plugins.**
   - pytest runs with its built-in `--junitxml=<tmp>`, and passed, failed,
     errors and skipped counts come from the XML, plus the first 20 failures'
     node id and message.
   - unittest is parsed from its summary line.
   - A missing pytest returns `error_code="framework_unavailable"`, not a
     usage error.
   - Extra `args` may not override the report flag.
6. **Real resource limits, or none claimed.**
   - On POSIX, `max_tool_cpu_seconds` and `max_tool_memory_mb` become
     `RLIMIT_CPU` and `RLIMIT_AS`, set in the child before `exec`, through
     `preexec_fn` or a small `prlimit` wrapper.
   - On Windows, a job object enforces the same limits.
   - `sandbox_mode=true` runs the command under the existing Bubblewrap
     isolation used for worktree environments (`worktrees/environment.py`),
     with the workspace writable and the rest of the filesystem read-only.
     When `bwrap` is missing it fails closed.
   - Keys that stay unimplemented are deleted from the config schema rather
     than kept as decoration.

## 3. Non-goals

- Parsing shell command strings for path policy (the `cwd` contract stands).
- Changing background terminals (PRD-149/151), except that they share goals
  1–3.

## 4. Acceptance criteria

1. In a project without `pytest-json-report`, `run_tests` on one passing and
   one failing test reports `passed=1, failed=1` and the failing node id.
2. With a project `.venv` containing a package agenthicc lacks, `run_python`
   imports it, and the result names `.venv/bin/python`.
3. `run_bash("head -c 40")`, run with data on the parent's stdin, returns
   empty stdout immediately.
4. `run_bash("yes | head -c 300000000")` keeps agenthicc's peak RSS growth
   under 50 MB. Its result includes the head, the tail and
   `stdout_bytes=300000000`.
5. A failing command whose error is in the last line of 1 MB of output shows
   that line in the result.
6. `run_bash("env")` contains no `ANTHROPIC_API_KEY`, even when the parent has
   one, unless it is listed in `exec.env_passthrough`.
7. With `max_tool_memory_mb=64`, a command allocating 512 MB fails with a
   memory error. With `sandbox_mode=true` and no `bwrap`, every command fails
   closed with `error_code="sandbox_unavailable"`.
