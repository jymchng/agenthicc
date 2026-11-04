# TUI responsiveness while subagents run (TUIR series)

The study, and the PRDs that fix what it found. Start with
[TUIR-00](TUIR-00-study-tui-unresponsive-with-subagents.md).

| PRD | Title | Fixes | Priority |
|---|---|---|---|
| [TUIR-00](TUIR-00-study-tui-unresponsive-with-subagents.md) | Study: why the TUI stops responding while subagents run | — (evidence, method, ranking) | — |
| [TUIR-01](TUIR-01-discovered-path-authorization-off-the-loop.md) | Discovered-path authorization: one cheap decision per kept result, off the event loop | Root cause R1 (≈99 % of measured stall) | P0 |
| [TUIR-02](TUIR-02-bounded-filesystem-discovery.md) | Bounded filesystem discovery: prune before walking, filter before authorizing, share a limiter | R2, R3 (walk cost, GIL contention across workers) | P0 |
| [TUIR-03](TUIR-03-journal-work-off-the-loop.md) | Journal work off the event loop: an incremental subagent projection and a durable writer thread | R4, R5 (O(journal) folds, synchronous fsync) | P1 |
| [TUIR-04](TUIR-04-event-loop-lag-budget.md) | An event-loop lag budget: a benchmark gate and a live watchdog | Regression prevention for R1–R5 | P1 |
| [TUIR-05](TUIR-05-attention-badge-says-what-it-counts.md) | The attention badge says what it counts | F1 (misleading "◆ 7 need you" beside a subagent pool) | P2 |

Order: TUIR-04's benchmark lands first (it is the failing test), then TUIR-01
and TUIR-02 together, then TUIR-03, then TUIR-05.

## Delivery log

| Wave | Branch | PRDs |
|---|---|---|
| tools 4 | `tools-wave-4-discovery-off-loop` | TUIR-01, TUIR-02 |
| tools 5 | `tools-wave-5-journal-and-lag` | TUIR-03, TUIR-04 |
