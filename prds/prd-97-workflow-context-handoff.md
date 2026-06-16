# PRD-97 — WorkflowContext: Structured Phase Handoff

## Problem

`WorkflowContext.as_system_block()` is the only mechanism for passing
information from one phase to the next (besides shared memory).  It truncates
every phase's `full_text` to 200 characters regardless of type.  A plan phase
that produces 3 000-character structured output appears to the next phase as
200 characters of raw text.

For more complex workflows — a research phase producing a JSON report, a
spec phase producing a structured outline, a multi-step pipeline — the 200-char
truncation is catastrophic.

`PhaseOutput.structured` already contains parsed data (e.g., `{"plan_text":
"..."}` for `output_schema="plan"`).  This richer representation is never used
in the context block.

## Goals

- `as_system_block()` renders `PhaseOutput.structured` fields when available,
  not raw `full_text`.
- Workflow authors can supply a `context_summary_fn` on `PhaseSpec` to control
  how their phase's output appears in subsequent phases' context.
- The default for phases with no structured output increases from 200 to 1 000
  characters.

## Design

### `PhaseSpec` addition

```python
@dataclass(frozen=True)
class PhaseSpec:
    ...
    context_summary_fn: Callable[[PhaseOutput], str] | None = None
    # When None: use structured fields if available, else truncate full_text.
```

### `WorkflowContext.as_system_block()` redesign

```python
def as_system_block(self) -> str:
    lines = ["[WORKFLOW CONTEXT]", f"Original intent: {self.intent}", "",
             "Completed phases:"]
    for name, output in self.phase_outputs.items():
        snippet = self._summarise(output)
        lines.append(f"- {name} ({output.role}): {snippet}")
    return "\n".join(lines)

@staticmethod
def _summarise(output: PhaseOutput) -> str:
    # 1. Custom summary function (workflow author controls)
    if output.context_summary_fn is not None:
        return output.context_summary_fn(output)
    # 2. Structured data fields (richer than raw text)
    if output.structured:
        s = output.structured
        if "plan_text" in s:
            return s["plan_text"]          # full plan, no truncation
        if "content" in s:
            return s["content"][:2000]
        if "text" in s:
            return s["text"][:2000]
    # 3. Raw text fallback (increased limit)
    text = output.full_text
    if len(text) > 1000:
        return text[:1000] + "…"
    return text
```

### Example: custom summary for a research phase

```python
def _research_summary(output: PhaseOutput) -> str:
    # Render only the "findings" key from structured output
    return output.structured.get("findings", output.full_text[:500])

PhaseSpec(
    name="research",
    output_schema="research_report",
    context_summary_fn=_research_summary,
    ...
)
```

## File changes

| File | Change |
|---|---|
| `workflow/plugin.py` | Add `context_summary_fn` to `PhaseSpec`; rewrite `WorkflowContext.as_system_block()` and `_summarise()` |

## Acceptance criteria

- [ ] A plan phase with `output_schema="plan"` contributes its full `plan_text` to subsequent phases' context.
- [ ] A phase with no structured output contributes up to 1 000 characters (was 200).
- [ ] A phase with `context_summary_fn` uses that function's output in the context block.
- [ ] All existing tests pass; `test_workflow_plugin.py` assertions updated for new limits.
