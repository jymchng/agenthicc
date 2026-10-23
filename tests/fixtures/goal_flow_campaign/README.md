# goal_flow campaign cassette

Recorded with `--record-cassette` on 2026-09-15 (session
`84348746-7ff0-43a5-b2c3-0cd9e04a9ff8`, model `deepseek-v4.1-flash`, Yolo
mode). It is replayed by `tests/integration/test_goal_flow_cassette_replay.py`.

| File | Contents |
|---|---|
| `cassette.jsonl` | 547 recorded provider responses, in request order |
| `approvals.jsonl` | 6 recorded `ask_user` answers |
| `meta.json` | Session id and recording time (the intent was not recorded) |

## Shape of the recording

- **Entries 0–33** are the tail of an earlier `goal_flow` run that the session
  resumed mid-implementation. They cannot be replayed from a fresh run, so the
  test slices them off together with approval 0, which answered the `ask_user`
  call at entry 19.
- **Entry 34** is the first request of a fresh `goal_flow` run at `CLARIFY`
  (the catalog grows from 75 to 76 tools when `complete_clarification` is
  offered). From there the run clarifies, decides ten goals, and implements and
  verifies six of them, mutating the goal list with `append_goal` and
  `insert_goal` along the way. The recording ends while goal 7 is being
  implemented.
- **Entries 0, 2, 4, 199, 339 and 499** offered no tools: they are
  auto-compaction summary requests. A hermetic replay disables compaction and
  skips them.
- **Entry 405** called `patch_file` with a `content` argument that the tool has
  never accepted (its signature last changed on 2026-07-23). It was a model
  error when recorded, and the test pins it as the only known contract
  violation.

The recorded shell commands `cd` into the recording machine's checkout and
include git commits, so the replay must stay hermetic: every side-effecting
built-in tool is stubbed with a schema-identical replacement.
