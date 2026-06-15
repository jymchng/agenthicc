# PRD-72 — Mention Resolution Error Handling

## Problem

Typing `@/nonexistent/` crashes the entire agent turn before the LLM is
ever called. The failure path:

```
parse_mentions("@/prds/")
  → resolved = Path("/prds"), is_dir()=False, endswith("/")=True
  → kind = MentionKind.DIRECTORY          ← wrong: path does not exist
resolve_mention(kind=DIRECTORY)
  → _format_dir_block(Path("/prds"), "/prds/")
      → path.iterdir()  →  FileNotFoundError
  → exception propagates through build_context_prefix
  → _run_agent_turn raises
  → _agent_task_body except Exception → fail_turn()
  → AgentState.ERROR, Runtime: 00:15
```

Two bugs cooperate to produce this outcome:

**Bug 1 — `parse_mentions` misclassifies non-existent trailing-slash paths.**

```python
# mentions/parser.py line 80
elif resolved.is_dir() or path_str.endswith("/"):
    kind = MentionKind.DIRECTORY      # ← fires even when dir doesn't exist
```

A path that ends with `/` but does not exist should be `UNRESOLVED`.
`UNRESOLVED` already has a safe soft-error path in `resolve_mention` that
returns `[⚠ /prds/ not found]` as block content and never raises.

**Bug 2 — `_format_dir_block` only catches `PermissionError`.**

```python
# mentions/injector.py
except PermissionError:             # ← misses FileNotFoundError, OSError
    lines.append("[permission denied]")
```

`FileNotFoundError` is a subclass of `OSError`, not `PermissionError`.
It escapes the function and propagates all the way to `fail_turn()`.

---

## Goals

- `@/nonexistent/` never crashes a turn or involves the LLM.
- The mention produces a soft-error block (`[⚠ /prds/ not found]`) in the
  agent's context so the LLM can explain the problem to the user.
- No new files, no new abstractions.

---

## Fix — two targeted patches

### Layer 1: `mentions/parser.py` — correct DIRECTORY classification

Only classify a path as `DIRECTORY` when it actually exists on disk.

```python
# Before
elif resolved.is_dir() or path_str.endswith("/"):
    kind = MentionKind.DIRECTORY

# After
elif resolved.is_dir():
    kind = MentionKind.DIRECTORY
# Non-existent trailing-slash path falls through to UNRESOLVED
```

`UNRESOLVED` is returned by `resolve_mention` as:
```python
InjectedContent(mention=mention, block="[⚠ /prds/ not found]", error="not_found")
```
No exception, no failed turn. The LLM sees the warning in its context.

### Layer 2: `mentions/injector.py` — widen the exception catch

Belt-and-suspenders: even if a directory is deleted between parse time and
resolve time, no `OSError` subclass should escape `_format_dir_block`.

```python
# Before
except PermissionError:
    lines.append("[permission denied]")

# After
except OSError as exc:
    lines.append(f"[error reading directory: {exc}]")
```

---

## File changes

| File | Change |
|---|---|
| `mentions/parser.py` | Remove `or path_str.endswith("/")` from the DIRECTORY condition |
| `mentions/injector.py` | Widen `except PermissionError` → `except OSError` in `_format_dir_block` |

No other files change.

---

## Acceptance criteria

- [ ] `@/nonexistent/` in a message does not call `fail_turn()`.
- [ ] The turn starts normally; the LLM receives `[⚠ /nonexistent/ not found]`
      in its context.
- [ ] `@/existing-dir/` still works — directory listing is injected as before.
- [ ] A directory deleted between parse and resolve time produces an error
      block instead of raising.
- [ ] All existing tests pass.
