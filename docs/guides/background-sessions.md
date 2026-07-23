# Background sessions

Background sessions let a workflow continue after the foreground terminal is
released. The registry and worker are local to the current user; no transcript
or lifecycle telemetry is sent to a hosted service.

## Start and detach

Start a direct turn or an existing workflow from a shell:

```bash
uv run agenthicc run --background --intent "Review the authentication flow"
uv run agenthicc run --background --workflow code_plan --intent "Plan the next release"
```

The command returns the stable session ID as soon as the worker is accepted.
Inside an active TUI session, `/bg` and `/background` are equivalent. They
preserve the current session ID and journal, then return to the shell after the
worker lease is established. Both commands appear in the slash-command picker.

## Open and inspect the manager

`agenthicc agents` and `agenthicc jobs` open the same manager TUI. The manager
shows state, workflow, project, phase, recent activity, failure information,
and approval waits. It can also be rendered safely without a TTY, which is
useful for scripts and diagnostics.

Useful keys:

| Key | Action |
|---|---|
| `↑`/`k`, `↓`/`j` | Move selection |
| `Enter` | Attach/follow the selected session |
| `r` | Refresh |
| `PageUp`/`[` and `PageDown`/`]` | Scroll the selected transcript |
| `c` | Cancel the selected worker |
| `a` | Archive a terminal session |
| `p` | Pin or unpin a session |
| `y`/`n` | Approve or reject a visible approval request |
| `Ctrl+X` | Delete after explicit confirmation |
| `t` | Include recoverable trash in the list |
| `u` | Restore a selected deleted session |
| `?` | Show help |
| `q`/`Esc` | Leave the manager without stopping workers |

Delete is deliberately two-stage. Active work is cancelled first, then only
the exact session directory and its sibling kernel journal are moved to
`~/.agenthicc/background/trash/`. The tombstone remains in the append-only
registry, so a stale worker cannot resurrect it. `u` restores the artifacts
when they are still in recoverable trash.

## Scriptable control

```bash
uv run agenthicc jobs list --json
uv run agenthicc jobs status SESSION_ID --json
uv run agenthicc jobs cancel SESSION_ID
uv run agenthicc jobs resume SESSION_ID
uv run agenthicc jobs retry SESSION_ID
uv run agenthicc jobs approve SESSION_ID
uv run agenthicc jobs archive SESSION_ID
uv run agenthicc jobs delete SESSION_ID
uv run agenthicc jobs restore SESSION_ID
```

JSON status removes the original intent and lease token and applies the same
secret-pattern redaction used by session inspection. A missing or invalid
transition is reported as a failed control operation; workers are never
implicitly relaunched after a process restart.

## Configuration

The optional `[background]` section may be placed in the project or global
`agenthicc.toml`:

```toml
[background]
enabled = true
max_workers = 2
max_workers_per_project = 2
cancel_grace_s = 5.0
stale_after_s = 30.0
wall_timeout_s = 0.0       # 0 means no wall-clock timeout
max_activity_bytes = 64000
trash_retention_days = 30
```

Defaults are conservative. Invalid values fail closed before a worker is
created. `--set background.max_workers=1` is supported for one invocation, and
`AGENTHICC_DISABLE_BACKGROUND=1` is an emergency local disable switch.

The background registry is an append-only, fsync'd JSONL event stream. It is a
derived lifecycle index; the canonical conversation, workflow, kernel, and
approval journals remain owned by their existing runtime components. A missing
worker lease becomes `orphaned` and requires an explicit resume or retry.

## Verification

Run the full repository tests plus the PRD-141 maintained-surface gate with:

```bash
uv run python -m agenthicc.background.coverage_gate
```

The gate enforces at least 90% coverage across the background package, the
background CLI commands, and the background manager workspace. The ordinary
full-package coverage report remains useful for tracking unrelated legacy and
platform-specific surfaces.
