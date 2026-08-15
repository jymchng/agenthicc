# Security

agenthicc's safety model: approval gates, modes, and sandboxing.

## Approval gates

Tools carry a **risk level**; the auto-approve policy gates them:

- **Low** — always auto-approved (read-only, non-destructive)
- **Medium** — auto-approved unless strict mode is on
- **High / Critical** — always gated to the user

Approval requests surface as inline prompts in the TUI. You can approve,
reject, or (for background sessions) approve/reject via
`agenthicc jobs approve|reject`.

## Modes as a security control

- **Safe** — read-only tools run directly; mutations/commands/network need
  approval.
- **Plan** — hard-blocks mutations even with
  `--dangerously-skip-permissions`.
- **Yolo** — tools run without per-action approval.

## --dangerously-skip-permissions

```bash
agenthicc --dangerously-skip-permissions
```

Disables ALL tool approval prompts for the session. Overrides Safe-mode
approval requirements; Plan mode still hard-blocks side effects. Intentionally
not settable in `agenthicc.toml`.

## Sandboxing and guards

- **Path guards** — filesystem tools reject traversal and absolute escapes
  unless allowed.
- **Network guards** — URL/HTTP tools enforce allow/deny policies.
- **Approval contract** — every state-changing tool declares its risk; the
  gate enforces it.
- **Timeout/retry** — HTTP safety helpers bound resource use.
- **Secret handling** — keys via `api_key_env` / `--set-secret`, never in
  config files.

## Trust

`agenthicc trust` manages trusted plugins/tools (allow/deny at the plugin
layer).

## Related

- [Modes →](05-modes.md)
- [Tools →](09-tools.md)
