# Reliable command execution and services

PRD-151 gives `run_bash`, `run_command`, and their Lauren-ai wrappers one
authoritative execution contract. A command result is successful only when the
process exited with code `0`; a returned Python mapping is not itself proof of
success.

## Finite builds and tests

Use a finite command for builds, tests, migrations, and generators:

```json
{
  "argv": ["npx", "next", "build"],
  "cwd": "website",
  "timeout": 300
}
```

`timeout` is wall-clock seconds and accepts fractional values. `0` means no
deadline for that operation; the owning turn or session can still cancel it.
Negative values, infinity, NaN, and non-numeric strings are rejected before a
process is spawned. The result includes `state`, `returncode`, `stdout`,
`stderr`, `cleanup_result`, and the effective `deadline` owner. The states
`exited`, `failed`, `timed_out`, `cancelled`, `spawn_failed`, and `rejected`
are distinct. Build artifacts such as `.next` never substitute for the exit
result.

`cwd` is resolved before spawn and `env` is a string-only environment overlay:

```json
{
  "command": "npm run build",
  "cwd": "website",
  "env": {"NODE_ENV": "production"},
  "timeout": 300
}
```

The exact command/argv is retained in structured results while display
previews and persisted metadata redact credential-shaped values.

## Development servers

Development servers are services, not finite commands. Declare ownership and,
when possible, a readiness probe:

```json
{
  "command": "npm run dev",
  "cwd": "website",
  "background": true,
  "lifecycle": "service",
  "label": "website preview",
  "readiness": {
    "url": "http://127.0.0.1:3000",
    "timeout": 30
  }
}
```

The result returns a `terminal_id` and keeps the owned process group alive.
Readiness is reported only after an HTTP, TCP, or explicit output-marker probe
succeeds. Without a probe, readiness is `null`; output such as “started” is
not inferred as authoritative. A readiness observer timeout returns
`starting_timeout` and does not stop the service.

Use `inspect_terminal`, `wait_terminal`, and `wait_terminal_ready` for agent
control. In the TUI, `/ps` inspects owned terminals, `/stop <terminal-id>`
stops one exact process group, and `/stop all --confirm` stops all visible
terminals. `Esc` remains the cancellation control for the terminal currently
being awaited. Session shutdown cleans up owned descendants.

## Workflow phases

Workflow authors can require command outcomes explicitly:

```python
PhaseSpec(
    name="build",
    terminal_wait_policy="foreground",
    command_lifecycle="oneshot",
    require_successful_commands=True,
)
```

For a preview service:

```python
PhaseSpec(
    name="preview",
    terminal_wait_policy="background",
    command_lifecycle="service",
    require_readiness=True,
)
```

The runner consumes structured outcomes, not human-readable output. Failed,
timed-out, cancelled, rejected, or orphaned commands stop the phase before its
`next` transition. Service handles and readiness evidence remain in phase
metadata for inspection and resume.

## Security and limits

Shell execution still requires the existing execute capability and workspace
policy. Readiness probes default to loopback addresses. Output is bounded and
redacted before rendering and persistence. Agenthicc never adopts arbitrary
host PIDs or sends command data to analytics or advertising services.
