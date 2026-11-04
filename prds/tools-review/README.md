# Tool robustness and effectiveness (TOOLR series)

A review of every tool agenthicc offers its agents, and the PRDs that act on it.
Start with [TOOLR-00](TOOLR-00-tool-review.md): the inventory, the method, each
finding with its evidence (several reproduced against the shipped tools), and
the routing table below.

| PRD | Title | Priority |
|---|---|---|
| [TOOLR-00](TOOLR-00-tool-review.md) | Review of every tool agenthicc gives its agents | — |
| [TOOLR-01](TOOLR-01-lossless-file-edits.md) | Lossless, exact and atomic file edits | P0 |
| [TOOLR-02](TOOLR-02-command-execution-hygiene.md) | Command execution that is isolated, bounded and runs the project's own interpreter | P0 |
| [TOOLR-03](TOOLR-03-git-tool-correctness.md) | Git tools that are non-interactive, bounded and exact | P1 |
| [TOOLR-04](TOOLR-04-one-tool-result-contract.md) | One result and error contract, enforced across the whole tool surface | P1 |
| [TOOLR-05](TOOLR-05-network-tools-under-one-policy.md) | Every network tool under one destination policy | P0 |
| [TOOLR-06](TOOLR-06-mcp-results-and-schemas.md) | MCP results, schemas and reconnection | P1 |
| [TOOLR-07](TOOLR-07-gates-fail-closed.md) | Capability and approval gates fail closed | P0 |
| [TOOLR-08](TOOLR-08-tool-surface-curation.md) | A smaller, accurate tool surface | P2 |
| [TOOLR-09](TOOLR-09-tool-io-off-the-event-loop.md) | Tool I/O off the event loop, and a race-free file cache | P1 |
| [TOOLR-10](TOOLR-10-search-and-read-effectiveness.md) | Search and read tools the model can use precisely | P2 |

Recommended order:
1. TOOLR-07, which is small and closes a gate bypass.
2. TOOLR-01, TOOLR-02 and TOOLR-05, which fix data loss, a broken tool and
   boundary gaps.
3. TOOLR-04, whose contract test guards the rest.
4. TOOLR-03, TOOLR-06 and TOOLR-09.
5. TOOLR-08 and TOOLR-10.

Discovery cost is owned by
[TUIR-01/02](../tui-responsiveness/README.md).

## Delivery log

| Wave | Branch | PRDs |
|---|---|---|
| 1 | `tools-wave-1-gates-and-edits` | TOOLR-07, TOOLR-01 |
| 2 | `tools-wave-2-exec-and-git` | TOOLR-02, TOOLR-03 |
| 3 | `tools-wave-3-network-and-mcp` | TOOLR-05, TOOLR-06 |
| 4 | `tools-wave-4-discovery-off-loop` | TOOLR-09 (with TUIR-01, TUIR-02) |
| 5 | `tools-wave-5-journal-and-lag` | TUIR-03, TUIR-04 (event-loop budget the TOOLR-09 work is held to) |
| 6 | `tools-wave-6-result-contract` | TOOLR-04 (with TUIR-05) |
