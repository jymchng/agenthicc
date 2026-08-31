# Troubleshooting

Practical fixes for common issues.

## Provider authentication errors

**Symptom:** `401 Unauthorized` on first run.

**Fix:**

```bash
echo "${ANTHROPIC_API_KEY:+set}"   # prints "set" if present
agenthicc config show               # inspect the resolved provider
```

Check the `api_key_env` name in your provider profile matches the exported
env var. Use `--set-secret provider.api_key_env=ANTHROPIC_API_KEY` to point a
profile at an env var.

## Model not found

**Symptom:** the provider rejects the model id.

**Fix:** override per run with `--set execution.model=<model>` or fix the
profile's `model` in config. Verify with `agenthicc config show`.

## Rate limits

**Symptom:** `429` errors.

**Fix:** transient provider requests use a one-hour timeout by default. The
active stream is retried up to three times with exponential backoff, and a
provider `retry_after` hint is honored. If a provider needs a different
deadline or retry policy, configure the execution settings or active provider
profile:

```toml
[execution]
timeout_s = 3600
transport_max_retries = 3
transport_retry_base_delay_s = 1.0
llm_sdk_max_retries = 2
```

## Config validation failures

**Symptom:** agenthicc refuses to start with a config error.

**Fix:**

```bash
agenthicc config validate
```

The validator names the offending key. Common causes: unknown `provider`,
invalid `model`, or a profile missing `base_url`/`api_key_env`.

## Session resume problems

**Symptom:** `--resume <id>` shows nothing or the wrong transcript.

**Fix:**

```bash
agenthicc sessions list             # confirm the id
agenthicc sessions show <id>        # inspect the session
```

Check `resume_transcript_turns` in `[behaviour]` if the tail is too short.

## TUI paste / mode issues

- Pastes stay behind a `[Pasted text #N ...]` placeholder — `Ctrl+V` reveals,
  `Esc` after `]` discards.
- `/mode Debug` is rejected — only Safe → Plan → Yolo (+ aliases) are valid.

## MCP server issues

```bash
agenthicc mcp                        # list servers + status
agenthicc doctor                     # connectivity diagnostics
```

## Generic diagnostics

```bash
agenthicc doctor                     # environment + connectivity checks
agenthicc config validate            # config checks
agenthicc --headless --workflow code_plan  # run headless for clean errors
```

## Related

- [Configuration →](02-configuration.md)
- [FAQ →](13-faq.md)
