# code_plan session cassette

Recorded with `--record-cassette` on 2026-06-17 (session
`cd1ba304-3ea8-47c7-bada-189462799692`) while a code_plan run enhanced the
password-notification feature of a separate repository. It is replayed by
`tests/integration/test_cassette_replay.py`.

| File | Contents |
|---|---|
| `cassette.jsonl` | 76 recorded provider responses, in request order |
| `approvals.jsonl` | 2 recorded plan-review answers: a rejection ("be more extensive"), then an approval |
| `meta.json` | Session id and recording time (the intent was not recorded) |

## Shape of the recording

- **Entries 0–35: plan.** The model explores, its first plan is rejected, it
  calls `request_plan_approval` three times without a plan (entries 29, 32 and
  33), and the second complete plan is approved and finalized.
- **Entries 36–63: execute.** File edits (`write_file`, `patch_file`,
  `apply_diff`, `append_file`) and test runs (`run_tests`, `run_bash`), then
  `mark_execute_complete`.
- **Entries 64–73: review**, ending in `approve_review`; **74–75: summary**.

## Known differences from today's runtime

- **Model errors when recorded.** Entries 30 and 71 pass `start`/`end` to
  `read_file`, which has never accepted them (`read_lines` does), and entry 51
  calls `patch_file` without `path`. The test pins these three as the only
  contract violations.
- **Execution approvals.** The recording predates per-plan execute modes: its
  execute phase always ran in the unrestricted mode of the time, and its plan
  approval carries no mode. Today that approval selects the default Safe execute
  mode, so each of the 14 write and exec calls during execution raises an
  approval prompt the recording never answered. The replay's fallback approves
  them, and the test pins that count.
- **Caller mode.** The session ran in a mode that allowed the review phase's
  test runs; today that is Yolo, so the main replay uses `mode="Yolo"`. A second
  replay runs in Plan mode to prove the execute phase's mode switch still
  reaches only the replay stubs.

The recorded shell commands `cd` into the recording machine's checkout, so the
replay must stay hermetic: every side-effecting built-in tool is replaced by a
schema-identical stub inside the tool registry.
