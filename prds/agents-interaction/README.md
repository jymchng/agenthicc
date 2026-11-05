# Answering, resuming and typing to an agent (AGI series)

These PRDs fix three reported failures:

1. `agenthicc agents` cannot answer an agent's `ask_user` question.
2. `R`/`r` (retry/resume) end in an HTTP provider error.
3. The `ask_user` overlay's input lacks the main composer's `/` commands,
   `@` mentions and paste.

Start with the study,
[AGI-00](AGI-00-study-answering-resuming-and-composing.md). It holds the
evidence from the operator's own background store, each root cause with its
`file:line`, and the reproductions.

| PRD | Title | Fixes | Priority |
|---|---|---|---|
| [AGI-00](AGI-00-study-answering-resuming-and-composing.md) | Study: answering, resuming and typing to an agent | — (evidence, root causes, routing) | — |
| [AGI-01](AGI-01-answer-questions-in-the-agents-hub.md) | Answer an agent's questions in the hub, one at a time, by choosing or typing | 1: hand-typed JSON for 2+ questions; no options shown | P0 |
| [AGI-02](AGI-02-background-questions-wait-for-the-operator.md) | Background questions wait for the operator, can be answered late, never linger stale | 1: 300 s expiry while unattended; stale "needs you" rows | P0 |
| [AGI-03](AGI-03-background-turns-get-the-session-tools.md) | Background direct turns get the session tools, including `ask_user` | 1: background direct agents cannot ask | P1 |
| [AGI-04](AGI-04-resume-and-retry-keep-the-launch-configuration.md) | Resume and retry relaunch with the session's own launch configuration | 2: overrides and secret references dropped → 401 | P0 |
| [AGI-05](AGI-05-one-composer-for-every-answer.md) | One composer for every answer | 3: `@` and paste dropped; no `/` picker | P1 |

Order:
1. AGI-04, which is small and makes every failed session resumable.
2. AGI-05 with AGI-01; the hub form uses the embedded composer.
3. AGI-02, then AGI-03.

The study also records a 19.6 GB background event log (§3.4). It is not a
cause of these failures and is left for a store-compaction PRD.

## Delivery log

| Wave | Branch | PRDs |
|---|---|---|
| 1 | `agi-wave-1-resume-launch-spec` | AGI-00 (study), AGI-04 |
| 2 | `agi-wave-2-answer-composer` | AGI-05, AGI-01 |
