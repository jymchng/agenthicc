# Troubleshooting

Work outside-in: reproduce, read the structured error, then narrow. Most
problems here are configuration or expectation mismatches rather than defects.

## Start with these three

```bash
agenthicc --version           # is the CLI reachable at all?
agenthicc config validate     # is the configuration accepted?
agenthicc mcp doctor --json   # is every configured MCP server healthy?
```

All three are read-only. `mcp doctor` prints `[]` when no servers are
configured, which is success, not silence.

!!! warning "There is no `agenthicc doctor`"
    Diagnostics are subcommand-scoped. For provider or configuration problems
    use `agenthicc config validate`; for MCP use `agenthicc mcp doctor [NAME]`.
    A bare `agenthicc doctor` fails with `invalid choice: 'doctor'`.

For a clean error without the TUI, run headless:

```bash
agenthicc --headless --workflow code_plan < /dev/null
```

## Provider authentication failures

**Symptom:** the provider rejects the request with an auth error.

**Fix:**

```bash
agenthicc config show               # inspect the resolved provider
```

Check that the `api_key_env` name in your profile matches the variable you
actually exported, and that the variable is present in *this* shell. Use
`--set-secret execution.api_key_env=ANTHROPIC_API_KEY` to point at an env var
without writing the key to disk.

## Model not found

**Symptom:** the provider rejects the model id.

**Fix:** override for one run with `--set execution.model=<model>`, then fix the
profile's `model` in config once you know the right id. Confirm what resolved
with `agenthicc config show`.

## Rate limits

**Symptom:** `429` responses.

**Fix:** transient requests use a one-hour timeout by default; the active stream
is retried with exponential backoff and a provider `retry_after` hint is
honored. Tune it in the execution settings:

```toml
[execution]
timeout_s = 3600
transport_max_retries = 10
transport_retry_base_delay_s = 1.0
llm_sdk_max_retries = 2
```

!!! danger "`timeout_s` is the provider timeout"
    It governs the LLM request. `turn_timeout_s` is the separate turn deadline.

## Configuration is rejected

**Symptom:** agenthicc refuses to start, or a value is silently ignored.

**Fix:**

```bash
agenthicc config validate
```

The validator names the offending key. Common causes: an unknown `provider`, an
invalid `model`, a profile missing `base_url`/`api_key_env`, or a section
misspelled — notably `[behavior]` instead of the real `[behaviour]`.

## My change had no effect

**Symptom:** an edit to `agenthicc.toml` or `--set` does not apply.

**Fix:** confirm which file was actually read. Precedence is defaults → user
`~/.agenthicc/agenthicc.toml` → project `.agenthicc/agenthicc.toml` →
`AGENTHICC_*` env vars → `--set`. The project file wins over the user file, and
a `--config` path replaces the search entirely. See
[Configuration](02-configuration.md).

## Session resume problems

**Symptom:** `--resume <id>` shows nothing, or the wrong transcript.

**Fix:**

```bash
agenthicc sessions list          # confirm the id
agenthicc sessions show <id>     # inspect the session
```

If the tail is too short, raise `[behaviour] resume_transcript_turns`. Remember
that resume replays presentation-only; the events are not re-persisted.

## MCP server problems

**Symptom:** a configured server exposes no tools, or a tool call fails.

**Fix:** work the ladder and stop at the first failure:

```bash
agenthicc mcp list --json        # is it configured, and in which file?
agenthicc mcp doctor --json      # dependency and transport health
agenthicc mcp refresh NAME       # re-read the catalogue
```

`mcp list --json` prints the `path` of the file it read — that is the single
most useful field when a server you added "is not there". A *tool* problem
cannot be fixed by editing a tool allow-list if the *connection* is failing.

!!! note "The transport-alias trap"
    The CLI accepts `stdio`, `ws`, `websocket`, `streamable`, and `http`; the
    runtime accepts `stdio`, `streamable_http`, `sse`, and `ws`. Check which
    layer produced the error before editing the config.

Note that a bare `agenthicc mcp` opens the manager UI rather than printing a
status summary — use `mcp list` or `mcp doctor` for a non-interactive answer.

## A tool or command is missing

**Symptom:** a project tool or slash command is not available.

**Fix:** project extension discovery is deferred until after the first TUI
frame. Wait for the shell phase to become ready, then `/tools reload` or
`/commands reload`, and check the phase report with `/startup`.

## TUI paste and mode issues

- Pastes stay behind a `[Pasted text #N ...]` placeholder — `Ctrl+V` reveals the
  full text, `Esc` after the `]` discards it.
- `/mode Debug` is rejected. Only Safe → Plan → Yolo and the aliases
  `Auto`/`Guard`/`Ask`/`Review` are valid.
- A still animation frame is not a hang: the frame is intentionally quiet while
  a prompt owns the terminal. Check `/status`.

## A prompt blocks an unattended run

**Symptom:** a headless run appears to hang.

**Fix:** headless denies by default, so an outside-workspace request with no
operator fails closed rather than prompting forever. Supply an explicit
scope-aware approval adapter, or run with `--dangerously-skip-permissions`
inside a sandbox you control. Plan still hard-blocks side effects.

## Related

- [Configuration →](02-configuration.md)
- [FAQ →](13-faq.md)
- Depth: [Fact base](../reference/fact-base.md) · [CLI reference](../reference/cli.md)
