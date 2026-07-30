# Usage ledger reference

`agenthicc.runners.usage_ledger` is the canonical provider-usage boundary.

Public types:

- `UsageLedger`: owns records for one session, folds journal entries, commits
  completed calls, reconciles cumulative run totals, and emits snapshots.
- `UsageRecord`: immutable JSON-safe record for one provider model call.
- `UsageCall`: local handle returned before a request is sent.
- `UsageRunTracker`: correlates stream chunks and scoped lauren-ai lifecycle
  signals for one run.
- `UsageSnapshot`: aggregate token, cache, cost, quality, call-count, and
  durability values used by the TUI and session projections.
- `UsageValues`: validated optional provider token fields.
- `UsageQuality`, `CostStatus`, `DurabilityStatus`, `UsageSource`,
  `UsageLifecycle`, and `UsageCategory`: explicit state/provenance enums.

## Journal schema

Usage is stored in `conversation-journal.jsonl` as versioned entries:

```json
{
  "seq": 12,
  "kind": "usage_record",
  "schema_version": 1,
  "record": {
    "record_id": "run-1:1:local-id",
    "session_id": "session-id",
    "conversation_id": "session-id",
    "run_id": "run-1",
    "call_index": 1,
    "input_tokens": 123,
    "output_tokens": 45,
    "cost_usd": 0.01,
    "cost_status": "estimated",
    "source": "reconciled",
    "lifecycle": "completed"
  }
}
```

The journal's message fold ignores usage entries. The usage fold keeps the
latest valid record for each `record_id`, stops at a corrupt trailing line,
and validates non-negative integer counts and finite non-negative costs.
Usage records never contain prompts, completions, tool arguments, or secrets.
