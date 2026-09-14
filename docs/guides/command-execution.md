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

## Try it

The headless runner is the quickest way to see the JSON-lines envelope that a
command outcome travels in, and it exits 0 with no input:

```bash
PYTHONPATH=src python -m agenthicc --headless < /dev/null
```

```text
{"status": "ready", "mode": "headless", "session_id": "4c783bb1ff244a66bf3243d28aead0da"}
```

One JSON object per line. `session_id` changes per run; the keys do not. This
is the same event stream a workflow phase consumes, which is why the runner can
insist on structured outcomes instead of parsing human-readable output.

To confirm a real terminal command end to end, use the contract helper that
derives success from the exit code rather than from the presence of a mapping:

```bash
PYTHONPATH=src python -c "
import agenthicc.tools.exec as ex
print('module:', ex.__name__)
print('outcome types:', [n for n in dir(ex) if 'Outcome' in n or 'Terminal' in n][:8])
"
```

```text
module: agenthicc.tools.exec
outcome types: ['CommandOutcome', 'InspectTerminalTool', 'StopTerminalTool', 'WaitTerminalReadinessTool', 'WaitTerminalTool']
```

In the terminal workspace the same resources are visible as slash commands:
`/ps [terminal-id] [--json]` inspects owned terminals and
`/stop [terminal-id|all] [--force]` stops one exact process group.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| A build "succeeded" but produced no artifact | The process exited non-zero while a wrapper returned a mapping | Success is derived only from exit code `0` (`CommandOutcome`). Read `state` and `returncode`, never the presence of a result object |
| A command hangs until the turn ends | `timeout` omitted, or set to `0` (no deadline) | Set a wall-clock `timeout` in seconds; `0` genuinely means no deadline and only the owning turn or session can cancel it |
| `rejected` returned before a process was spawned | A non-numeric, negative, infinite, or NaN timeout | Fix the timeout value; validation happens before spawn, so nothing ran |
| A dev server is "ready" immediately | No readiness probe was declared, so readiness is `null` | Add `readiness` (`url`, `tcp`, or an output marker). Output containing "started" is never inferred as readiness |
| `starting_timeout` and the service is gone | Confusing an observer timeout with a kill | `wait_terminal` is observational and never kills on observer timeout. `starting_timeout` does not stop the service; stop it explicitly with `/stop <terminal-id>` |
| A phase advanced despite a failed command | `require_successful_commands` not set | Set it on the `PhaseSpec`; failed, timed-out, cancelled, rejected, and orphaned outcomes then stop the phase before its `next` transition |
| A preview phase ends as soon as it starts | `terminal_wait_policy` left at the default | Use `terminal_wait_policy="background"` with `command_lifecycle="service"` and `require_readiness=True` |
| `cwd` is not what you expected | A relative `cwd` | `cwd` resolves before spawn; pass an unambiguous path and remember the process working directory of the parent is never changed |
| A credential appears in output | You read the raw result instead of the redacted preview | Display previews and persisted metadata redact credential-shaped values; keep the raw value out of logs |

### Zero exit versus service readiness

These are different contracts and the guide conflates them at your peril. A
one-shot command is successful when it exits `0`. A service has no exit to wait
for, so success is replaced by a readiness probe. Passing `lifecycle="service"`
without a probe leaves readiness unknown — `null`, not `true`.

### Cancelling the wrong thing

`Esc` cancels the terminal currently being awaited; `/stop <terminal-id>` stops
one exact owned process group. Session shutdown cleans up owned descendants.
Agenthicc never adopts arbitrary host PIDs, so a process you started outside
the session is not yours to stop through these controls.
