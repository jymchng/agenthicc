# Testing

The test suite is split by how much runtime it exercises.

## Test layers

| Layer | Location | Typical contents |
|---|---|---|
| Unit | `tests/unit/` | Reducers, config, parsers, registries, memory algorithms, key decoding |
| Integration | `tests/integration/` | Event processor, workflows, tools, memory SQLite, cassettes |
| E2E | `tests/e2e/` | Full session paths, Rich workspace, terminal/runtime behaviour |

Pytest uses `asyncio_mode = "auto"` and a 60-second default timeout. Shared
fixtures live in `tests/conftest.py`; cassette helpers live in
`tests/conftest_cassette.py` and `agenthicc.testing`.

## Kernel tests

Reducer tests should call `root_reducer(state, event)` directly and assert the
new frozen state plus effects. Processor tests must create a task for
`processor.run()` before emitting and must drain before asserting. Always cancel
and await the processor task in teardown.

## Workflow and provider tests

Use `lauren-ai` mock/recording transports rather than real provider calls.
Queue one response for every expected LLM round-trip. Exercise:

- normal phase transitions;
- rejection and retry loops;
- parallel phases and failure handling;
- context compaction and model-window limits;
- provider-step transport retry (a late failure must not roll back an earlier
  committed step);
- journal resume and idempotent tool replay.

PRD-182 fault-injection tests should distinguish a logical user turn from its
provider steps. A fake stream must fail before bytes, after partial text, and
after a prior tool exchange. Assert the exact provider message list on the
retry/follow-up request, the step receipts and folded journal, the absence of
partial text from ordinary provider messages, and the side-effecting tool's
execution count. Use a fresh journal-backed memory instance to verify the same
committed prefix survives restart.

`reconstruct_site` adds deterministic evidence-contract coverage in
`tests/unit/test_reconstruct_evidence_prd177.py`, integration coverage in
`tests/integration/test_reconstruct_site_prd177.py`, and the offline
workflow journey/performance coverage under `tests/e2e/` and
`tests/performance/`. These tests use temporary workspaces and fake providers;
they do not require Playwright, CloakBrowser, MCP, or provider credentials.
PRD-178's clean-room research contract and hard gate are covered by
`tests/unit/test_reconstruct_research_prd178.py`,
`tests/integration/test_reconstruct_research_prd178.py`, and
`tests/e2e/test_reconstruct_research_prd178.py`.
Run the focused matrix with:

```bash
uv run pytest tests/unit/test_reconstruct_evidence_prd177.py -q
uv run pytest tests/integration/test_reconstruct_site_prd177.py -q
uv run pytest tests/e2e/test_reconstruct_site_prd177.py -q
uv run pytest tests/performance/test_reconstruct_site_prd177.py -q
uv run pytest tests/unit/test_reconstruct_research_prd178.py tests/integration/test_reconstruct_research_prd178.py tests/e2e/test_reconstruct_research_prd178.py -q
```

`agenthicc.testing` provides `SessionCassette`, mock approvals, and
`run_headless_replay()` for deterministic scenarios.

Provider replay regressions should also cover reasoning-enabled
OpenAI-compatible gateways: record the assistant's optional
`reasoning_content`, replay the matching tool result, and assert that the
second request contains the exact field without rendering it as transcript
text. The PRD-190 fake-gateway tests are fully offline and do not require
Console Go credentials.

## TUI and terminal tests

Test signal and conversation mutations without a terminal. Test Rich rendering
with a captured console. Test input capability handlers with synthetic `Key`
values. Platform-specific key decoding must remain a pure function so Windows
cases can run on Linux CI. Also cover non-TTY startup and cleanup.

The removed prompt-toolkit `render_frame_ansi`/`pyte` contract is not a current
test target; new screen assertions should target `Workspace` and its actual
Rich renderables.

## Checks

```bash
uv run ruff check src/ tests/ scripts/
uv run ruff format --check src/ tests/ scripts/
uv run mypy src/agenthicc
uv run mypy tests/unit/test_kernel_event_typing.py tests/unit/test_type_audit.py
uv run python scripts/type_audit.py --check docs/reference/type-safety-baseline.json
uv run pytest tests/unit -q
uv run pytest tests/integration -q
uv run pytest tests/e2e -q
uv run pytest tests/ -q
```

Public exports also need the LLM documentation check defined in `noxfile.py`:

```bash
uv run nox -s llms_check
```

The embedded checker and Nox install paths are being made reproducible as part
of PRD-138 P0.5.

## Coverage and flake control

The `coverage` Nox session enforces a 90% repository-wide floor. Treat that as
the release gate implemented by `noxfile.py`, not as a measure of feature
completeness. Use coverage to identify untested boundaries, not as a substitute
for failure-mode tests. Avoid live network, real credentials, and
nondeterministic wall-clock assertions.

## Try it

The fastest end-to-end check that the test environment is wired correctly is a
targeted subset rather than the whole suite:

```bash
.venv/bin/python -m pytest tests/ -q -k "skill or llms"
```

```text
........................................................................ [ 66%]
....................................                                     [100%]
108 passed, 3685 deselected in 4.69s
```

Counts vary as the suite grows; `0 failed` and a nonzero `passed` are the
signals. To install the tooling in a fresh checkout:

```bash
uv sync --extra dev
```

That extra is what provides `pytest`, `pytest-asyncio`, `pytest-timeout`,
`hypothesis`, `httpx`, and the docs toolchain (`mkdocs`, `mkdocs-material`,
`pymdown-extensions`).

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Tests pass locally and fail in CI | CI runs the Nox sessions, which may add flags | Reproduce with the session, e.g. `nox -s tests` or `nox -s tests_unit` |
| An async test never runs | Missing the async plugin marker | The dev extra provides `pytest-asyncio`; keep the project's own asyncio configuration rather than adding per-file event loops |
| `ModuleNotFoundError` for an optional integration | The optional extra is not installed | Install the matching extra (`mcp`, `playwright`, `cloakbrowser`, `cloud`, `book`) or skip the focused test |
| A test hangs forever | A missing timeout | `pytest-timeout` is in the dev extra; give subprocess and terminal tests an explicit bound |
| A test depends on real network access | A live provider was called | Use cassettes and the recording approval services instead of reaching the network |
| A test asserts against the TUI's old rendered-frame contract | The `render_frame_ansi`/`pyte` contract is historical and removed | Assert on structured state (`tui/conversation_store.py`), not on rendered frames |
| Approval tests are flaky | A real approval service is being exercised | Use the recording or mock approval service so prompts are deterministic |
| Results differ between runs | A shared temporary directory or a real user cache | Use temporary homes and project directories; set `AGENTHICC_CHANGELOG_CACHE` to isolate the changelog cache |
| `llms_check` fails after a code change | A new public kernel symbol has no `### Symbol` heading | Add the heading to `llms-full.txt`, or remove the symbol from `kernel.__all__` — do not weaken the check |
| The docs build fails in strict mode | A warning is treated as an error | Fix the warning; keep `mkdocs build --strict` in the gate rather than downgrading it |

### Test the boundaries, not the happy path

The recurring failure modes in this codebase are the interesting ones: a
cancellation that left an unanswered tool call, a corrupt trailing JSONL line,
a resume that must not duplicate a side effect, and a permission decision made
by the wrong layer. Prefer a test that fails closed over one that asserts a
convenient default.

### Cassette replay is exact

Recorded approvals include the canonical target and operation, and replay
matches those fields exactly. A replayed approval for a *different* outside
target is rejected by design, so a cassette edited to broaden a grant will fail
rather than silently approve.
