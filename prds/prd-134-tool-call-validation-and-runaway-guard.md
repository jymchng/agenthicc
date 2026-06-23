# PRD-134 — Tool-Call Validation & Runaway Guard

**Status:** Proposal (investigation complete; no implementation yet)
**Author:** agent investigation, 2026-06-23
**Related:** PRD-129 (idempotency/journal), PRD-133 (context-window overflow guard)

---

## 1. Symptom

Running agenthicc against `deepseek-v4-flash` via an Anthropic-style gateway
(`ANTHROPIC_BASE_URL=https://api.openmodel.ai`, `ANTHROPIC_MODEL=deepseek-v4-flash`)
produced a stream of failing tool calls whose arguments were **fragments of the
model's own generated text** (it had been asked to produce password/pattern
content):

```
✗ Read(`)
✗ Read(#@-@@@-####)
✗ Read(jK)
✗ Read(##-@@@-####)
✗ Read(ssw0rd!)
```

Two things are independently wrong here:

1. **`Read` is not an agenthicc tool.** agenthicc registers `read_file`, not
   `Read`. The model hallucinated a Claude-Code-style tool name.
2. **The arguments are garbage** — `` ` ``, `jK`, `ssw0rd!` are fragments of the
   content the model was generating, not a valid `{"path": ...}` payload.

The session did not crash. It **spun** — each garbage call errored, the model
emitted another, and the loop continued. The only bound is `max_agent_turns`
(default **200**, `config.py:123`), so a confused model can burn up to 200 turns
of API spend producing nothing.

---

## 2. Root cause

This is **not** a single bug — it is the absence of a validation/resilience
boundary. An unreliable model behind a non-Anthropic-native gateway emits
malformed tool calls, and **every layer faithfully passes the garbage through to
execution and then loops on the error**.

### 2.1 The streaming parser trusts the provider's stream structure

`_transport/_anthropic.py:710-755` builds tool calls purely from whatever the
gateway streams:

- `content_block_start` (type `tool_use`) → take `block.name` verbatim as the
  tool name (line 738).
- `content_block_delta` (`input_json_delta`) → concatenate `partial_json`
  fragments (line 722-729).

If the gateway is not a faithful Anthropic Messages implementation (openmodel.ai
fronting deepseek is **not**), these events can carry a hallucinated name
(`Read`) and non-JSON `partial_json` (text fragments). The parser has no notion
of "is this a real tool" or "is this valid JSON for that tool" — it just emits a
`ToolCallDelta`.

### 2.2 The runner turns un-parseable input into `{}` silently

`_agents/_runner.py:1163-1177` assembles the accumulated deltas:

```python
try:
    parsed_input = json.loads(input_json)
except (json.JSONDecodeError, ValueError):
    parsed_input = {}          # ← garbage silently becomes empty input
accumulated_tool_calls.append(ToolCall(tool_use_id=tid, name=name, input=parsed_input))
```

A fragment like `ssw0rd!` fails `json.loads` and is **discarded into `{}`**. The
tool call still proceeds — now with empty input — guaranteeing a downstream
error instead of a clear "you sent invalid arguments" signal.

### 2.3 The executor gives the model nothing to self-correct with

`_tools/_executor.py:307-312` and `:644`:

```python
return ToolResult.error(f"Unknown tool: '{name}'", tool_use_id=tool_use_id)
```

The error names the bad tool but **does not list the valid tools** and **does
not suggest the closest match** (`Read` → `read_file`). The model receives
`Unknown tool: 'Read'`, learns nothing actionable, and guesses again. There is
also **no validation of input against the tool's `input_schema`** anywhere in the
executor (grep for `input_schema|jsonschema|required` in `_executor.py` → only an
unrelated cache-invalidation hit). A known tool called with `{}` runs and fails
inside the tool body with a tool-specific message, not a uniform "missing
required field `path`".

### 2.4 No runaway / repeated-failure guard

The agent loop (`_agents/_runner.py:575`, `:1055`) is bounded only by
`for _turn in range(effective_config.max_turns)`. There is no tracking of
**consecutive failed tool calls**, **repeated identical calls**, or **unknown-tool
rate**. A model stuck emitting garbage runs the full 200 turns. (grep for
`consecutive|repeated|runaway|error_count` → no hits.)

### 2.5 Provider-format mismatch is the trigger, not the disease

The operational trigger is pointing the **Anthropic transport** at a
**non-Anthropic gateway**. But the architecture should be resilient to *any*
misbehaving model — a native Claude model can also hallucinate a tool name. The
fix must be model-agnostic, not "tell users to stop using openmodel.ai".

---

## 3. Architectural gaps (summary)

| # | Gap | Location | Effect |
|---|-----|----------|--------|
| G1 | No tool-name validation before dispatch | `_runner.py` assembly → `_executor.py` | hallucinated names reach execution |
| G2 | Un-parseable input silently → `{}` | `_runner.py:1169-1170` | garbage args become empty, masking the real error |
| G3 | No input-schema validation | `_executor.py` (absent) | known tool runs with invalid/empty input |
| G4 | Unknown-tool error is not actionable | `_executor.py:312,644` | model cannot self-correct |
| G5 | No runaway / repeated-failure guard | `_runner.py:575,1055` | up to 200 turns of garbage spend |
| G6 | Streaming parser trusts provider structure | `_anthropic.py:710-755` | malformed events → malformed ToolCalls |

---

## 4. Proposed architecture

A single **tool-call validation & resilience boundary** sitting between
*tool-call assembly* and *tool execution*, plus a **runaway guard** in the agent
loop. Four parts, layered cheapest-first.

### Layer A — Validate-and-explain at the assembly→execution boundary

Introduce a pure validation step in lauren-ai applied to each assembled
`ToolCall` before `_execute_tools`, with access to the registry (names +
`input_schema`s):

1. **Name resolution.** If `name` is not a registered tool:
   - Do **not** dispatch.
   - Return a structured `ToolResult.error` whose message lists the available
     tool names and the **closest fuzzy match** (`difflib.get_close_matches`),
     e.g. `Unknown tool 'Read'. Did you mean 'read_file'? Available: read_file,
     write_file, list_directory, …`.
   - This is the single highest-leverage change: it converts a dead-end error
     into a one-turn self-correction signal.

2. **Input validation.** Validate `input` against the tool's `input_schema`
   (required fields + types; stdlib-only check, no new dependency — a minimal
   required/type walk over the JSON-Schema dict the tools already expose).
   On failure return `Invalid arguments for 'read_file': missing required field
   'path'. Schema: {…}` rather than running the tool with `{}`.

3. **Well-formedness.** Distinguish "the model sent non-JSON input" (G2) from
   "empty object". Replace the silent `parsed_input = {}` with a tagged
   `_MALFORMED` sentinel so Layer A can emit *"arguments were not valid JSON"*
   instead of an indistinguishable empty-object error.

> **Design note:** Layer A is *pure* (registry + ToolCall in, ToolResult-or-OK
> out) and lives at the lauren-ai boundary so **every** transport/provider
> benefits, not just Anthropic. It does **not** mutate or "auto-correct" the
> model's call (no silent `Read`→`read_file` rewrite) — silent aliasing hides
> model errors and breaks idempotency keying (PRD-129). It only *rejects with
> guidance*.

### Layer B — Runaway / repeated-failure guard in the agent loop

Track, per agent run:

- **consecutive failed/invalid tool calls** (reset on any success), and
- **identical repeated calls** (same `canonical_tool_key(name, input)` N times).

When either crosses a threshold (default: **5** consecutive failures, configurable
via `ExecutionSettings`), stop the loop with a distinct terminal outcome —
`stop_reason = "tool_call_runaway"` — surfaced to the kernel/TUI as a clear
*"agent appears stuck emitting invalid tool calls; stopping early"* message,
instead of silently burning the remaining turns. Reuses `canonical_tool_key`
already built for PRD-129.

### Layer C — Streaming-assembly robustness

At the assembly point (`_runner.py`), drop tool calls that cannot be made
well-formed (empty name after stream end; input that is neither valid JSON nor
empty) and feed a corrective `ToolResult.error` back rather than fabricating a
`ToolCall`. This stops malformed *stream events* from ever becoming executable
calls (G6) — the complement to Layer A's registry check.

### Layer D — Provider-compat diagnostic (operational)

When Layer B trips **and** the unknown-tool rate is high, emit a one-time
diagnostic hint: *"a high rate of unknown/invalid tool calls can indicate the
endpoint is not Anthropic-compatible; if using a gateway, try `provider=openai`
or a native model."* Document in CLAUDE.md that `ANTHROPIC_BASE_URL` pointed at a
non-Anthropic gateway is unsupported/fragile.

---

## 5. Recommendation & priority

**Ship A + B together.** They are the core resilience fix and are model/provider
agnostic:

- **A** guarantees only registered, schema-valid tool calls execute, and turns
  every rejection into actionable feedback (the model self-corrects in ~1 turn).
- **B** bounds the blast radius so no misbehaving model can cost 200 turns.

**C** is a small hardening of the assembly site (do alongside A — same file).
**D** is documentation + one log line; cheap, do last.

Explicitly **rejected** alternatives:

- *Silent name aliasing* (`Read`→`read_file`): hides model defects, breaks
  idempotency keys, and is unbounded (every harness has different names).
- *Lowering `max_turns`*: punishes legitimate long sessions; doesn't address
  garbage on short ones.
- *Provider-sniffing/auto-switching transports*: too magic; surface a diagnostic
  (D) and let the user choose.

---

## 6. Phased plan

**Phase 1 — Layer A + C (lauren-ai).**
- New pure module `_tools/_validation.py`: `validate_tool_call(call, registry) ->
  ToolResult | None` (None = OK to dispatch); `suggest_tool(name, names)`;
  minimal `validate_against_schema(input, schema)`.
- Wire into `_runner.py` assembly: replace silent `{}` with `_MALFORMED`
  sentinel (C); run `validate_tool_call` before `_execute_tools` (A).
- Enrich `_executor.py` unknown-tool message as a fallback (defense in depth).
- Tests: unknown name → suggestion + tool list; non-JSON input → "invalid JSON";
  missing required field → schema error; valid call → dispatched unchanged.

**Phase 2 — Layer B (lauren-ai + agenthicc).**
- Runaway counter in the agent loop; `stop_reason = "tool_call_runaway"`.
- `ExecutionSettings.max_consecutive_tool_failures: int = 5` (config + CLI),
  threaded through `build_llm_config`/agent config.
- TUI/kernel surface the terminal outcome distinctly.
- Tests: 5 consecutive unknown-tool calls → loop stops with the new stop_reason,
  well under `max_turns`; a success resets the counter.

**Phase 3 — Layer D + docs.**
- One-time diagnostic on runaway-with-high-unknown-rate.
- CLAUDE.md note on gateway/provider compatibility.

**Phase 4 — Features PRD.**
- Add a section to `prd-68-feature-expectations.md` documenting the validation
  boundary and runaway guard as guaranteed behaviors.

---

## 7. Acceptance criteria

1. A tool call to an unregistered name is **never executed**; the model receives
   the available-tools list and the closest match in the error.
2. A tool call with non-JSON or schema-invalid input is **never executed**; the
   error states *what* was wrong (invalid JSON vs. missing field), not a bare
   downstream failure.
3. N consecutive failed/invalid tool calls (default 5) **stop the agent loop**
   with `stop_reason = "tool_call_runaway"` — provably ≤ N+1 turns, not 200.
4. A legitimate session (valid tool calls interleaved with the occasional
   recoverable error) is **unaffected** — the counter resets on success.
5. No silent name rewriting; idempotency keys (PRD-129) unchanged.
6. `mypy`, `ruff`, and the full suite pass; no `typing.Any` in new files.

---

## 8. Evidence index

| Claim | File:line |
|-------|-----------|
| `max_agent_turns` default 200 | `agenthicc/config.py:123,576` |
| Streaming parser takes `block.name` verbatim | `lauren_ai/_transport/_anthropic.py:738` |
| `input_json_delta` concatenated as-is | `_anthropic.py:722-729` |
| Empty-name suppression / non-streaming fallback | `_anthropic.py:750-792` |
| Un-parseable input → `{}` silently | `lauren_ai/_agents/_runner.py:1163-1177` |
| Bare "Unknown tool" error, no suggestions | `lauren_ai/_tools/_executor.py:307-312,644` |
| No input-schema validation in executor | `_executor.py` (grep: only cache hit) |
| Loop bound only by `max_turns` | `_runner.py:575,1055` |
| No consecutive/repeated-failure guard | `_runner.py` (grep: no hits) |
