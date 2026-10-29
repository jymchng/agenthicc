---
title: "SUBAGENT-09: Observability and the transcript/event contract"
status: Proposed
version: 0.1.0
date: 2026-10-29
scope: "one documented, versioned event and transcript schema shared by kernel events, durable transcripts and the TUI rails, with uniform redaction, visible truncation, per-pool replay and the derived counters that diagnose stuck or over-budget pools — never the panel's information architecture, a usage meter, or a change to what a result is"
area: A-9
related_prds:
  - AGX-08
  - PRD-124
  - PRD-157
  - SUBAGENT-06
  - SUBAGENT-07
  - SUBAGENT-08
tags:
  - subagents
  - observability
  - transcripts
  - events
  - redaction
---

# SUBAGENT-09: Observability and the transcript/event contract

## 1. Executive summary

A subagent pool reports everything it does, four times, in four vocabularies that
no document relates. Five kernel event types
(`src/agenthicc/kernel/events.py:31-35`), three scroll-buffer kinds
(`src/agenthicc/subagents/pool.py:1297-1306`, `:1343-1356`, `:1388-1397`), eight
transcript kinds (`src/agenthicc/subagents/transcripts.py:48-61`) and six panel
status strings (`pool.py:227`) describe the same lifecycle. They disagree about
which identifiers they carry, they disagree about whether their text is redacted,
and only the journal's records carry a `schema_version` at all.

**This PRD makes one contract out of the four.** One event record shape with a
version and a correlation set; one lifecycle vocabulary that the other three sink
types map onto rather than reinvent; one redaction guarantee applied at every
sink's boundary instead of two of them; truncation that says it happened;
per-pool replay from durable records; and the handful of derived counters that
answer "is this pool stuck" and "is this pool over budget" without anyone reading
a JSONL file by hand.

It formalises AGX-08 phase 1's surface — the panel, the per-worker transcripts and
the signal they read — rather than redesigning it, and it names the fields that
SUBAGENT-06, -07 and -08 need carried so each of those PRDs stops promising fields
into a schema nobody owns.

## 2. Problem statement

All claims verified at commit `603d5598726db70f0eeab5d0c217047050ad8668`
(2026-10-29); `src/` and `tests/` have not moved since the report's snapshot
`f843040` (§3 re-checks it).

**Four vocabularies, no relation.** A pool emits kernel events through
`_emit_pool_started` / `_emit_worker_started` / `_emit_worker_done` /
`_emit_pool_completed` (`pool.py:1480-1560`), whose types are declared in the
kernel's projection set (`src/agenthicc/kernel/events.py:31-35`); scroll events
through `_append_scroll_event` (`pool.py:1343-1356`, `:1388-1397`); transcript
lines through `_record` (`:479-545`); and panel state through the
`SubagentPoolState` snapshot (`:1281` → `_publish`, `:1411`). The transcript's
eight kinds are the only closed set with a validator (`transcripts.py:48-61`,
`from_mapping` at `:89-107`). Nothing maps one vocabulary onto another: a
`SubagentFailed` kernel event, a `subagent_worker_done` scroll event, a
`finished` transcript line and a `"failed"` panel row are four names for one
transition, and no document says so.

**Correlation is inconsistent across sinks.** Kernel payloads carry `pool_id`
and `task_id` (`pool.py:1532-1533`, `:1512-1513`). Scroll payloads carry neither:
`subagent_worker_done` has `label`, `ok`, `error`, `duration_ms`, `summary`,
`tool_calls`, `changed_paths`, `done` and `total` (`:1345-1355`) — no pool id and
no worker id — and `subagent_pool_started` carries `total`, a per-worker
`{label, type}` list and `timeout_s` (`:1297-1306`), also without its own
`pool_id`. Transcript lines carry `kind`, `at`, `text`, `tool`, `ok` and
`dropped` (`transcripts.py:63-107`); the pool and worker are implied by the file
path. So the rails cannot be joined to the kernel stream, and a transcript cannot
identify itself if it is copied out of its directory.

**Redaction covers two sinks and not the others.** The pool redacts panel
activity on the way in — `_redact` at `pool.py:482`, `:499`, `:507`, `:536`,
defined at `:1446-1454` — and the transcript writer redacts again at write time
(`transcripts.py:209-217`). The two event paths do not: `result.text` is placed
into the scroll payload at `pool.py:1350` and into the kernel payload at `:1536`
after nothing but a slice. A worker that pastes a `.env` file into its final
answer therefore masks it in the panel and in its transcript and broadcasts it in
an event payload. Whether that is acceptable is a decision nobody has made; it is
currently an accident of which line called `_redact`.

**Success and failure are the same scroll event.** The kind expression at
`pool.py:1344` reads `"subagent_worker_done" if result.ok else
"subagent_worker_done"` — both branches identical. The `ok` field distinguishes
them for a reader who looks, but any consumer keying on the event name sees
success and failure as one kind. The documentation says so too, in the singular:
"`subagent_worker_done` for each worker"
(`docs/guides/subagents.md:514-515`).

**Only the journal versions itself.** Journal records write
`"schema_version": 1` per record kind (`src/agenthicc/memory/journal.py:857-895`,
`:897-925`; the file contains thirteen `schema_version` occurrences across its
record kinds). Kernel events do not: `Event.create`
(`src/agenthicc/kernel/events.py:136-150`) sets an id, a timestamp and a payload
and has no version field, and `to_dict` (`:152-160`) has nowhere to put one.
Transcript lines have no version (`TranscriptEvent.to_dict`,
`transcripts.py:76-87`), and `pool.json` has none either
(`_write_manifest`, `:219-235`). The one durable format in the subsystem that
could have drifted silently is therefore the only one that is versioned, and the
three that are actually consumed are not.

**Truncation is visible in one sink and silent in the others.** The transcript
compactor drops its oldest events behind an explicit `truncated` marker carrying
a cumulative `dropped` count (`transcripts.py:267-296`), which is a genuinely
good design. The event payloads instead slice `result.text[:_MAX_DISPLAY_RESULT_CHARS]`
(`pool.py:92`, `:1350`, `:1536`) with no marker, so a reader cannot tell a
2,000-character answer from a truncated one. The subsystem already knows the
right pattern; it applies it once.

**Transcript write failures are invisible.** `_guarded`
(`transcripts.py:202-206`) catches `OSError` and `ValueError` and returns, and
`_submit` (`:195-200`) drops writes submitted after `aclose`. The comment states
the intent — "a full disk must not fail the worker" — and the consequence is that
a disk problem produces a transcript that looks complete and is short, with no
counter, no event and no log. The panel shows a worker as `done`; the file it
points at is partial; nothing anywhere says which.

**There are no metrics at all.** `grep -rniE
"counter|metric|histogram|latency|gauge|stats" src/agenthicc/subagents/*.py`
returns one label counter inside `run()` (`pool.py:1219`) and one unrelated
prose match. The data needed is present and unused: `WorkerState.tokens`
(`pool.py:234`) is updated per turn (`:504`), `duration_ms` rides every payload,
`started_at`/`finished_at` exist per worker (`:232-233`), and `timeout_s` is
known (`types.py:31`). Nothing derives a duration distribution, a per-type
success rate, or a "this worker has been `running` for 90% of its timeout"
signal, which is the one number an operator watching a stuck pool needs.

**Replay exists per worker and not per pool.** `TranscriptTail`
(`transcripts.py:343-385`) follows one worker's file incrementally,
`read_transcript` (`:317-325`) reads one worker's file whole, `last_event`
(`:327-341`) reads the tail of one file, and `list_pools` (`:408-441`) lists
pools. There is no `replay_pool`; and there cannot be one built on the kernel
log, because all five subagent event types are declared *projection* events whose
reducer does nothing (`src/agenthicc/kernel/events.py:23-40`: "Event types that notify
subscribers but leave kernel state unchanged"). A pool's timeline is therefore
reconstructible only by merging transcripts with journal records, which nothing
does.

**The contract is prose in a user guide.** `docs/guides/subagents.md:113-121`
carries a genuinely useful three-row table separating
`SubagentResult.text` (complete, untruncated), journal records (complete,
fsync'd) and event payloads (bounded preview) — and `:512-525` lists the events
narratively. This is the closest thing to a contract in the tree, and it is a
guide: no field list, no version, no redaction guarantee, no consumer rules. A
PRD that ignores it would create a fifth description; this one adopts its
three-representation split and makes it normative.

**The tests assert the payloads they construct, not the ones the pool emits.**
`tests/unit/test_subagent_tui.py:69-70` appends
`{"total": 3}` and `{"label": "explorer #1", "ok": True}` by hand and asserts the
TUI renders them; `SubagentPoolStarted` and its four siblings appear in **no**
test (`grep -rn "SubagentPoolStarted" tests/ --include=*.py` → no output), and
`redact` appears in none of the three subagent test files. So both the event
payloads and the redaction guarantee are currently unverified.

## 3. Current-source investigation

Run on 2026-10-29 at the commit in §2.

```
$ git rev-parse HEAD
603d5598726db70f0eeab5d0c217047050ad8668
$ git diff --stat f843040..HEAD -- src/ tests/
(no output)
```

Nothing in the implementation has moved since the report's snapshot, so §1 of the
report still describes the code and every citation below was read, not inherited.

**The four vocabularies, enumerated rather than asserted.**

```
$ grep -rn "SubagentPoolStarted\|SubagentStarted\|SubagentCompleted\|SubagentFailed\|SubagentPoolCompleted" src/ --include=*.py
src/agenthicc/kernel/events.py:31:        "SubagentCompleted",
src/agenthicc/kernel/events.py:32:        "SubagentFailed",
src/agenthicc/kernel/events.py:33:        "SubagentPoolCompleted",
src/agenthicc/kernel/events.py:34:        "SubagentPoolStarted",
src/agenthicc/kernel/events.py:35:        "SubagentStarted",
src/agenthicc/subagents/pool.py:1487:                "SubagentPoolStarted",
src/agenthicc/subagents/pool.py:1511:                "SubagentStarted",
src/agenthicc/subagents/pool.py:1527:        event_type = "SubagentCompleted" if result.ok else "SubagentFailed",
src/agenthicc/subagents/pool.py:1552:                "SubagentPoolCompleted",
```

Five types, declared once as projection events and emitted from four methods. The
scroll kinds are string literals at their call sites (`pool.py:1299`, `:1344`,
`:1391`, plus the cached-resume `"system"` at `tool.py:266`), the transcript kinds
are the validated set at `transcripts.py:48-61`, and the panel statuses are the
comment on `WorkerState.status` (`pool.py:227`). Four sources of truth, none of
which references another.

**The redaction asymmetry, established by reading the call sites.**

```
$ grep -n "_redact(" src/agenthicc/subagents/pool.py
482:        target = self._pool._redact(_tool_target(tool_input))
499:        self._pool._update(self.worker_id, activity=self._pool._redact(_first_line(text)))
507:        target = self._pool._redact(_tool_target(tool_input))
536:            activity=self._pool._redact(summary),
1446:    def _redact(self, text: str) -> str:
```

Four call sites, all on the panel-activity path, plus the transcript writer's own
`_redact` at write time (`transcripts.py:209-217`). Neither event path calls it:
`pool.py:1350` and `:1536` slice `result.text` directly. This is the finding that
makes goal 6 a security goal rather than a tidiness goal.

**The identical ternary, read in place.**

```
$ awk 'NR>=1343 && NR<=1345' src/agenthicc/subagents/pool.py
                self._append_scroll_event(
                    "subagent_worker_done" if result.ok else "subagent_worker_done",
                    {
```

**The version field's absence, checked in all four places.**

```
$ grep -n "schema_version" src/agenthicc/memory/journal.py | head -3
15:    {"seq": 2, "kind": "turn_started",   "turn_id": "...", "schema_version": 2}
383:                    "schema_version": 1,
410:                "schema_version": 1,
$ grep -n "def create" -A14 src/agenthicc/kernel/events.py
136:    def create(
...
143:        return cls(
144:            event_id=uuid4().hex,
145:            event_type=event_type,
146:            timestamp=time.time(),
147:            payload=payload,
148:            source_agent_id=source_agent_id,
149:            tool_call_id=tool_call_id,
150:        )
```

The journal's own header comment shows the discipline — a version per record kind,
and it has already been bumped to `2` and `3` in places — while the kernel event
constructor has no version slot. The asymmetry is not a design decision recorded
anywhere; there is no comment on either side to that effect.

**The metrics absence proof.**

```
$ grep -rniE "counter|metric|histogram|latency|gauge|stats" src/agenthicc/subagents/*.py
src/agenthicc/subagents/pool.py:1219:        # Type-index counter for labelling: {type_name → count_so_far}
src/agenthicc/subagents/types.py:166:    "in the codebase. Actively look for counter-evidence. "
```

The pattern and the scope are the whole package, so this is a bounded claim about
the commit: the package has no counter, histogram or gauge of any kind. The
second hit is a sentence inside a system prompt.

**The test gap, measured the same way.**

```
$ grep -rn "SubagentPoolStarted" tests/ --include=*.py
(no output)
$ grep -rn "redact" tests/unit/test_subagent_panel_agx08.py \
    tests/integration/test_subagent_panel_agx08.py tests/unit/test_subagent_pool.py
(no output)
```

**Existing tests, measured on 2026-10-29.**

```
$ pytest tests/unit/test_subagent_pool.py tests/unit/test_subagent_tui.py \
         tests/unit/test_subagent_panel_agx08.py tests/integration/test_subagent_panel_agx08.py \
         tests/unit/test_subagent_pool_runtime_coverage.py -q
108 passed
```

Per file: 58, 20, 22, 5 and 3 passed. The two panel files (27 tests) are the
existing observability suite: they drive `TranscriptWriter`, `list_pools`,
`TranscriptTail` and the panel's rendering of a `SubagentPoolState`.

**Surprises, recorded rather than smoothed over.** Three, and two of them shrank
this PRD.

1. `docs/guides/subagents.md:113-121` already states the three-representation
   split that goal 8 formalises. A-9 therefore *adopts* it as normative instead of
   inventing a fifth description, and §9 has to say so.
2. The kernel already has an event-type registry with a real contract —
   `PROJECTION_EVENT_TYPES` (`src/agenthicc/kernel/events.py:23-40`) plus "every other event
   type must have a reducer handler, or the processor rejects it". That registry
   is the natural home for the version, so goal 3 extends it rather than adding a
   parallel one.
3. The transcript compactor's `truncated` marker (`transcripts.py:267-296`) is
   the pattern goal 6's truncation rule needs. This PRD generalises an existing
   local solution rather than designing a new one.

## 4. Goals

1. **One event record shape.** A single documented `SubagentEvent` carries
   `schema_version`, `event_id`, `at`, `kind`, `pool_id`, `worker_id`,
   `attempt`, `session_id`, `run_id`, `ok` and a bounded, redacted `text`. Kernel
   events, scroll events and transcript lines each project this record into their
   own serialisation instead of defining fields independently. `kind` is drawn
   from one enum that replaces the four current vocabularies.
2. **One lifecycle vocabulary, mapped.** The ten transitions a pool can undergo
   — pool opened, worker scheduled, worker started, worker text, tool started,
   tool finished, approval requested, approval resolved, worker finished, pool
   finished — are named once, and a documented table maps each to its kernel
   type, scroll kind, transcript kind and panel status. No sink invents a state;
   a sink that cannot express one says so in the table.
3. **The kernel event carries a version.** `Event.create`
   (`src/agenthicc/kernel/events.py:136-150`) gains an optional
   `schema_version: int` defaulting to `1`, and the five subagent types declare
   their version once, beside their declaration in `PROJECTION_EVENT_TYPES`
   (`src/agenthicc/kernel/events.py:23-40`), rather than hard-coding it at each
   emit site. Existing producers that omit it stay valid.
4. **Correlation is uniform.** Every subagent event — kernel, scroll and
   transcript — carries `pool_id` and `worker_id` where a worker exists, and
   `pool_id` where it does not. The scroll payload at `pool.py:1345-1355` and the
   pool-started payload at `:1297-1306` gain them; a transcript line gains them
   so a copied file is still identifiable.
5. **Success and failure become distinct scroll kinds.** `subagent_worker_done`
   splits into `subagent_worker_done` and `subagent_worker_failed`, fixing the
   identical ternary at `pool.py:1344`, with a deprecation rule for consumers
   that key on the old name.
6. **Truncation is visible.** Every bounded field carries the rule that produced
   it: a payload whose text was cut carries `truncated: true` and the original
   `text_length`, in the manner the transcript's `dropped` counter already
   establishes (`transcripts.py:267-296`). `result.text[:_MAX_DISPLAY_RESULT_CHARS]`
   (`pool.py:1350`, `:1536`) becomes the one helper that does this.
7. **Redaction is one guarantee, applied at every boundary.** Every sink redacts
   at write or emit time — kernel payload, scroll payload, transcript line, panel
   activity — through one function, and the guarantee is stated: no field
   documented as redacted may contain a value the session-export redactor would
   mask. The two unredacted event paths (`pool.py:1350`, `:1536`) are the first
   fix. Redaction is idempotent, so double application (panel then transcript) is
   defined rather than accidental.
8. **One result representation table, made normative.** The three rows of
   `docs/guides/subagents.md:113-121` — complete result, complete journal record,
   bounded preview — become part of this contract with their size policies
   stated, and the guide's prose points at the contract rather than restating it.
9. **Replay a pool, not just a worker.** `replay_pool(pool_id)` reconstructs a
   pool's ordered timeline by merging its transcript files with its journal
   records, and states its fidelity in one line: kernel events cannot contribute,
   because all five are projection-only (`src/agenthicc/kernel/events.py:23-40`). The result is
   a timeline, not a state machine — A-9 replays what happened, it does not
   re-derive the pool.
10. **Derived counters, no new meters.** From fields that already exist —
    `tokens` (`pool.py:234`, `:504`), `duration_ms`, `started_at`/`finished_at`,
    `timeout_s` — define the small set of derived values that diagnose a pool:
    per-worker elapsed and elapsed-to-timeout ratio, per-pool wall clock, per-type
    success rate, `stuck_workers` (a worker `running` past a declared fraction of
    its timeout), and `budget_ratio` consumed from SUBAGENT-06's record. A-9 names
    them and their definitions; it builds no meter and no cost accounting.
11. **A failed transcript write is reported.** A write that `_guarded` swallows
    (`transcripts.py:202-206`) increments a `transcript_write_failures` counter
    and emits one `subagent_transcript_degraded` event per pool, so a partial
    transcript is labelled partial instead of looking complete. Fail-open for the
    worker, fail-visible for the operator.
12. **AGX-08 phase 1 keeps working.** The panel keeps reading
    `SubagentPoolState`; `TranscriptTail` keeps following a file; the guide keeps
    its user-facing walkthrough. This PRD adds fields and a version to what those
    surfaces already carry.

## 5. Non-goals

The A-9 boundaries from the research report §5.3, restated as commitments.

- **The panel's information architecture and navigation.** AGX-08 phase 1
  (`Implemented (phase 1)`) shipped the panel, its focus model, the transcript
  keys and the stop action. A-9 formalises what those surfaces display and emits;
  it redesigns none of them.
- **Backgrounding a fan-out.** AGX-08 §5.6 calls this phase 2 and gates it on its
  own success criteria; SUBAGENT-07 owns it. A-9 only requires that a pool which
  outlives a turn keeps emitting into the same contract.
- **The usage meter, its units and its TUI display.** PRD-157 (`Implemented`)
  owns canonical usage accounting. A-9 defines a *ratio derived from* that
  ledger's numbers (goal 10) and never a second meter.
- **Budget limits, limit keys and the budget report.** SUBAGENT-06. A-9 consumes
  one crossing record to compute `budget_ratio` and defines no limit.
- **The semantics of a stop, a retry or a partial result.** SUBAGENT-08 owns
  those transitions; A-9 defines the envelope they are reported in, never what
  they mean.
- **Durable pool identity and result delivery.** SUBAGENT-07. A-9 requires that a
  durable pool record can carry the event version and correlation keys; it does
  not define the ledger or the delivery rule.
- **What a result is.** SUBAGENT-03 owns the structured output contract. A-9
  changes how a result is *reported* (bounded, redacted, marked when truncated)
  and never what it *contains*.
- **The kernel's event-sourced architecture, reducer or snapshot design.**
  PRD-01 owns it. A-9 adds a version field to an existing event constructor and
  respects the projection-only classification; it does not add reducers, change
  snapshotting, or make pool state derivable from the log.
- **Retention policy.** Transcripts live and die with the session directory
  (`transcripts.py:408-441` enumerates what is there); A-9 states the size bound
  that already exists (`MAX_TRANSCRIPT_BYTES`, `:42`) and does not introduce
  retention tiers, archival or deletion policy.
- **Vendor telemetry export.** No OpenTelemetry, no remote collector, no metrics
  endpoint. The counters in goal 10 are local values with declared definitions;
  exporting them is a separate decision this PRD does not take.
- **A fifth description of the events.** The guide's prose is the user-facing
  narrative and the contract is the normative schema; the contract is written so
  the guide can link to it.

## 6. Acceptance criteria

1. Every subagent emission site constructs one `SubagentEvent`, and a single
   schema test asserts its required fields, its `schema_version` and its `kind`
   enum membership: `tests/unit/test_subagent_pool.py`.
2. The lifecycle table names every kind the four sinks can produce, and a test
   fails if a sink emits a kind the table does not map:
   `tests/unit/test_subagent_pool_runtime_coverage.py`.
3. A `Subagent*` kernel event's `to_dict()` contains `schema_version == 1`, and
   `Event.create` without the argument still produces a valid record:
   `tests/unit/test_subagent_pool.py`.
4. Every subagent scroll payload and kernel payload carries `pool_id`, and every
   worker-scoped one carries `worker_id`:
   `tests/unit/test_subagent_pool.py`.
5. A transcript line carries `pool_id` and `worker_id`, so a copied `.jsonl` is
   still attributable: `tests/unit/test_subagent_panel_agx08.py`.
6. A failed worker emits `subagent_worker_failed` and a successful one emits
   `subagent_worker_done`, and the two differ only in `kind` and `ok` for an
   otherwise identical worker: `tests/unit/test_subagent_pool.py`.
7. A worker whose text exceeds the display bound produces a payload with
   `truncated: true` and `text_length` equal to the untruncated length, and a
   short one produces neither: `tests/unit/test_subagent_pool.py`.
8. A worker answer containing a known secret pattern is masked in the kernel
   payload, the scroll payload, the transcript line and the panel activity, and
   the same value is masked identically by all four:
   `tests/unit/test_subagent_pool.py`.
9. Redaction is idempotent: applying it to already-redacted text changes nothing,
   which is what makes panel-then-transcript double application safe:
   `tests/unit/test_subagent_panel_agx08.py`.
10. `replay_pool(pool_id)` returns one ordered timeline merging transcript and
    journal events for every worker, and its documented fidelity statement names
    the projection-only limitation: `tests/unit/test_subagent_panel_agx08.py`.
11. `stuck_workers` counts a worker `running` past the declared fraction of its
    timeout and does not count a `waiting` one, and the value appears on the pool
    snapshot: `tests/unit/test_subagent_pool.py`.
12. A transcript write failure increments `transcript_write_failures` and emits
    one `subagent_transcript_degraded` per pool, and the worker still completes:
    `tests/unit/test_subagent_panel_agx08.py`.
13. The panel renders a pool from a `SubagentPoolState` produced by a real pool,
    not a hand-built payload, for at least the started, running and finished
    cases: `tests/integration/test_subagent_panel_agx08.py`.
14. A pool with no failure produces byte-identical aggregate text and the same
    set of event kinds as today, plus the new fields:
    `tests/unit/test_subagent_pool_runtime_coverage.py`.

## 7. Rollout, migration and security

**Compatibility.** Every change is additive at the payload level: fields are
added, `schema_version` defaults to `1`, and `kind` keeps its existing string for
successful workers. The one behavioural change is goal 5's split — a failed
worker moves from `subagent_worker_done` to `subagent_worker_failed` — so
consumers that key on the old name need a one-line update; the TUI's own
consumers are `docs/guides/subagents.md:512-525`'s four kinds, all updated in the
same change. `SubagentPoolState` and `WorkerState` gain fields with defaults, so
`replace()` calls and positional construction keep working.

**Defaults.** No setting is required for a compliant consumer.

| Setting | Default | Meaning |
|---|---|---|
| `subagents.events.schema_version` | `1` | the version stamped on emitted records |
| `subagents.events.stuck_ratio` | `0.8` | elapsed-to-timeout fraction at which a worker counts as stuck |
| `subagents.events.preview_chars` | `2000` | the bounded preview length, replacing the literal at `pool.py:92` |

**Migration.** Existing artifacts carry no version, and the rule is explicit:
absence means version `1`, forever — never "unknown", and never a value invented
at read time. Existing `pool.json` manifests and `*.jsonl` transcripts therefore
read as version-1 artifacts and gain the correlation fields from the first line
written after the upgrade; a partially-upgraded transcript is legal, because
`from_mapping` (`transcripts.py:89-107`) already skips lines it cannot parse and
goal 4's fields are optional on read. Existing scroll events in a restored
session lack `pool_id` and are rendered exactly as they are today.

**Security.**

- **One redaction guarantee, or none.** The contract states which fields are
  redacted and requires the guarantee at every sink. Today two of four sinks are
  covered (`pool.py:482-536` plus `transcripts.py:209-217`) and the payloads at
  `pool.py:1350` and `:1536` are not; that is the highest-severity item in this
  PRD and criterion 8 is its test.
- **Bounded payloads are a leak control as well as a size control.** A 2,000
  character preview that says it is a preview prevents the failure mode where a
  reader assumes a truncated transcript means a truncated thought; the transcript
  side already made that decision (`transcripts.py:267-296`).
- **Transcripts are session-sensitive files.** They contain model output and tool
  targets, they are written under the session directory, and they are redacted
  before they reach disk (`transcripts.py:209-217`). A-9 adds no new location and
  no new export path; goal 4's identifiers must therefore be opaque ids, not
  paths or user text.
- **Fail-open for the worker, fail-visible for the operator.** A transcript write
  failure must never fail a worker (`transcripts.py:202-206` keeps that), and it
  must never be silent (goal 11 adds the counter and the event). A pool whose
  narration is incomplete is reported as degraded, which is the honest version of
  "fail-closed": do not claim a complete record you did not write.
- **Version mismatch is a read-time decision, not a crash.** A record whose
  `schema_version` is higher than the reader knows is reported as
  `unsupported_version` and skipped, so a newer agenthicc's artifacts cannot make
  an older session fail to open.

## 8. Verification

Current suite that must stay green:

```console
$ pytest tests/unit/test_subagent_pool.py -q
$ pytest tests/unit/test_subagent_tui.py -q
$ pytest tests/unit/test_subagent_panel_agx08.py -q
$ pytest tests/unit/test_subagent_pool_runtime_coverage.py -q
$ pytest tests/integration/test_subagent_panel_agx08.py -q
```

Measured on 2026-10-29 at the commit in §3: 58 passed (test_subagent_pool), 20
passed (test_subagent_tui), 22 passed (unit test_subagent_panel_agx08), 3 passed
(test_subagent_pool_runtime_coverage), 5 passed (integration
test_subagent_panel_agx08).

New cases belong in `tests/unit/test_subagent_pool.py` (the event record shape,
correlation fields, the kind split, visible truncation, the redaction guarantee
across four sinks, and `stuck_workers`), in
`tests/unit/test_subagent_panel_agx08.py` (transcript correlation fields, replay,
redaction idempotence and the degraded-write signal), in
`tests/unit/test_subagent_pool_runtime_coverage.py` (the lifecycle table's
completeness and the unchanged-kind regression), and in
`tests/integration/test_subagent_panel_agx08.py` (the panel rendering a real
pool's snapshot).

The two tests this PRD most needs are criteria 8 and 10. Criterion 8 fails today
as a matter of reading the source — `pool.py:1350` and `:1536` never call the
redactor — and criterion 10 is the boundary asserted as behaviour: replay is a
merge of durable records, because the kernel log is projection-only
(`src/agenthicc/kernel/events.py:23-40`) and cannot supply a pool's history.

## 9. Related and superseded PRDs

- **AGX-08 — formalises, narrows against.** AGX-08 is `Implemented (phase 1)` and
  owns the panel, the per-worker transcripts and the stop action. A-9 takes those
  three shipped surfaces as given and gives them one schema: it changes no
  layout, no key binding and no navigation, and it does not claim phase 2
  (background a fan-out, AGX-08 §5.6), which belongs to SUBAGENT-07. Where
  AGX-08 §5.1 and §5.3 define what the panel and a transcript *show*, this PRD
  defines what they *carry*.
- **PRD-124 — extends.** PRD-124 established the concurrent pool and the
  deliberate result boundary: the parent receives an aggregate, not a transcript
  (`docs/guides/subagents.md:113-121`). A-9 preserves that boundary exactly — it
  adds observability for people and changes nothing the parent's provider context
  contains — and it adds the version and the correlation keys that PRD-124's original
  payloads did not carry.
- **PRD-157 — unrelated to the meter, consumed.** PRD-157 (`Implemented`) owns
  canonical usage accounting and the token display. A-9 derives `budget_ratio`
  and per-worker token figures from what PRD-157 already maintains; it defines no
  unit, no cost and no second meter.
- **SUBAGENT-06 — consumes one record, narrows against.** A-6 detects a crossed
  budget, declares the limits and writes the crossing record. A-9 reads that
  record to compute `budget_ratio` and defines neither a limit nor a response; the
  M-1 boundary (research report §5.4) is untouched, because A-9 neither decides
  when a budget is exceeded nor what happens to the workers.
- **SUBAGENT-07 — consumes state, narrows against.** A-7 makes a pool outlive its
  turn and gives it a durable identity. A-9 requires that the durable record carry
  the event version and the correlation keys, and that a pool which survives a
  turn keeps emitting into this contract; it defines no ledger, no delivery rule
  and no background mode.
- **SUBAGENT-08 — supplies the envelope, narrows against.** A-8 owns what a stop,
  a cancellation, a retry and a partial result *mean*, and names the fields it
  needs exposed — `request_id`, `mode`, `source`, `reason`, `partial`,
  `undelivered_messages`. A-9 carries those fields in one record shape and, per
  A-8's own instruction, is free to rename the envelope as long as it carries
  them. A-9 defines no transition semantics and no cancellation path.

## A. Competitor evidence

Moderate, and honestly so: every harness has *an* answer to "what did my
subagents do?" and no two answer the same question. The subscription pattern is
the notable finding — opencode and Claude Code both render subagents from their
ordinary session event stream rather than a subagent-specific vocabulary, which
is the direction goal 2 takes by mapping the four local vocabularies onto one
record instead of adding a fifth. Depth labels are copied from the report's §2.1
table.

| Harness | Evidence | Depth | What it does about subagent observability | Take or reject |
|---|---|---|---|---|
| opencode | `packages/tui/src/mini/stream-v2.subagent.ts`, `packages/session-ui/src/message/agent-label.ts`, `script/drive/subagent-eviction.md`, `packages/core/test/subagent-lifetime.test.ts` @ `726cf096396d5014c3b1845e231cc01efe0421ee` | code | Observability is a *projection*: subagents render from ordinary session events, and the report records that "there is no separate subagent event vocabulary". Durability is SQLite, with an eviction drive test that starts a background subagent, evicts and rebuilds the parent, then lets the child finish | Take the subscription principle as the shape of goal 2 — one vocabulary, projected per surface — and the eviction test as the model for criterion 10's replay fidelity; reject a separate subagent-only stream, which is the four-vocabulary problem this PRD removes |
| Codex CLI | `codex-rs/protocol/src/protocol.rs:3321` @ `9cc14e79882ff8885cd5d33b4adadae014045aef` | code | `MultiAgentVersion { Disabled, V1, V2 }` travels with turn and model metadata, so the tool surface and prompt sections can change per version — a protocol that versions itself before it versions its payloads | Take the principle that a multi-agent protocol carries its version with the data (goal 1, goal 3) and the discipline of treating wire shape as versioned; reject a disabled/V1/V2 enum, because agenthicc has one implementation and needs an integer, not a migration ladder |
| DeepSeek Harness | `.agents/notes/archived/architecture/2026-07-20-canonical-tool-output-contract.md` @ `f3803c96974ad4a94195fb97e5c637e9c2a3afe3` | code + design notes | Every tool declares a canonical output; the registry snapshots it as lossless JSON, validates it, and contains renderer, projector, schema and JSON failures as `ToolOutputError` results — a failure is a typed, recorded outcome rather than an exception | Take "failures are contained *and recorded*, never thrown past the observer" as the reasoning behind goal 11's degraded-write signal and the `unsupported_version` rule in §7; reject the canonical-output machinery itself, which is SUBAGENT-03's decision about results, not A-9's about reports |
| Claude Code | `https://code.claude.com/docs/en/sub-agents` fetched 2026-10-29 | docs-only | Observability is a **tree**: nested subagents appear as a tree below the prompt input, each row marked with a `(+N)` descendant count, opening a row shows siblings and direct children with a path back to main, and a `/tasks` list retains finished rows briefly (30 seconds) | Take the tree-plus-descendant-count as the information an operator actually wants at a glance, and note that AGX-08 phase 1 already ships the flat version of this with its own bounded row retention, so the tree is an extension of shipped behaviour rather than a new surface; reject nesting-depth display here because depth itself is SUBAGENT-04's, and A-9 only has to be able to carry an `attempt` and a depth field |
| Qoder CLI | `https://docs.qoder.com/cli/subagent` fetched 2026-10-29 | docs-only | The `/agents` panel marks shadowed definitions as such, i.e. the observability surface reports definition-resolution outcomes, not only execution | Record as corroboration that a panel should surface *provenance* and not only state; it contributes no mechanism to this PRD, and no requirement rests on this row alone |
