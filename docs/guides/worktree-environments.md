# Reproducible worker worktree environments

Parallel workers can use a project-owned manifest to prepare fresh Git
worktrees and expose reviewed, reusable commands such as tests and lint. The
manifest is executable project data: it does not grant permission by itself.

## Manifest

Create `.agenthicc/environment.toml` at the repository root:

```toml
version = 1

[[setup]]
id = "install"
command = "uv sync --extra dev"
cwd = "."
timeout_s = 900
network = "requires_network"
expected_outputs = [".venv"]

[actions.tests]
command = "uv run pytest -q"
cwd = "."
timeout_s = 600
network = "none_declared"

[actions.lint]
command = "uv run ruff check src/ tests/"
cwd = "."
timeout_s = 120
network = "none_declared"
```

The v1 schema accepts `version = 1`, an optional `setup` array, an optional
`actions` table, and an optional `caches` array. Each setup/action command
supports:

| Field | Meaning |
|---|---|
| `id` | Setup-step identifier; optional, defaults to `setup_N`. Action names are their IDs. |
| `command` | Shell command executed in the worktree. |
| `cwd` | Worktree-relative directory, default `.`. |
| `timeout_s` | Positive wall-clock limit, default 600 seconds, maximum 3600. |
| `env` | Names of required process environment variables explicitly passed to the command. |
| `secret_env` | Subset of `env` whose values must be redacted from captured output. Names that look credential-related are also redacted automatically. |
| `expected_outputs` | Worktree-relative paths required after a successful command. |
| `platforms` | Optional platform names; commands are skipped when none match the current platform. |
| `network` | `none_declared` is enforced with a network namespace; `may_access_network` (default) and `requires_network` allow network access. |

The parser rejects unsupported keys, invalid identifiers, absolute paths,
invalid timeouts, malformed environment declarations, and unsafe cache
policies. Symlink-resolved `cwd` and expected-output paths must remain inside
the assigned worktree.

On Linux, execution requires `bubblewrap` (`bwrap`). Agenthicc fails closed if
it is unavailable or cannot create the isolation namespace; it never silently
falls back to an unrestricted shell. Install the distro package (for example,
`sudo apt install bubblewrap` on Debian/Ubuntu). Other operating systems
currently report `sandbox_unavailable` rather than running project commands
without filesystem isolation.

## Trust and first execution

Before setup or an action executes, Agenthicc checks that the current manifest
digest has been trusted for this Git repository or that this invocation has
explicit approval. A changed manifest has a different digest and requires a
new review.

For a persistent repository-level trust decision, first inspect the preview:

```bash
agenthicc worktrees trust .
```

This command only displays commands, working directories, network
declarations, timeouts, and the SHA-256 digest. To trust exactly that digest:

```bash
agenthicc worktrees trust . --confirm
```

Trust is stored in `~/.agenthicc/environment-trust.json` and keyed by the
repository's shared Git directory, so it applies to its linked worker
worktrees. The digest is rechecked on every run. For a one-time decision,
`agenthicc worktrees dispatch ORCHESTRATION_ID TASK_ID --approve` prints the
exact planned worker workspace and runs setup once. The shared
`spawn_worker_agents` tool fails closed when setup is not trusted; its error
includes the review preview and durable orchestration/task IDs so the user can
approve dispatch explicitly or trust the digest and retry.

## Running actions

The agents hub's detail view exposes actions for worker sessions: press `x`,
select an action with ↑/↓ and Enter, review its exact command/cwd/network
declaration/timeout, then press `y` to approve and run it. `Esc` exits the
list or preview without running anything. CLI equivalents are:

```bash
agenthicc worktrees actions WORKER_SESSION_ID
agenthicc worktrees action WORKER_SESSION_ID tests
agenthicc worktrees action WORKER_SESSION_ID tests --approve
agenthicc worktrees setup WORKER_SESSION_ID --approve
```

An untrusted action prints the review preview and exits without execution;
repeat with `--approve` for one-time approval, or trust the manifest digest.
`agenthicc agents actions` and `agenthicc agents action` are aliases. Outputs
shown to the operator are bounded and redacted; text output marks when capture
was truncated. Durable worker provenance
stores the manifest digest, command IDs, timestamps, result and exit code, but
not command output, the full environment, or secret values. At most 64
bounded receipts are retained per worker session.

## Cache policy

No cache is shared implicitly. A cache entry has `name`, `env_var`, and a
relative `path`, with optional `sharing`:

```toml
[[caches]]
name = "uv"
env_var = "UV_CACHE_DIR"
path = "uv"
sharing = "isolated"
```

`isolated` (the default) gives each worktree its own directory under
`~/.agenthicc/worktree-caches/`. `immutable_shared` reuses a declared existing
cache under the same user-owned cache root. `mutable_shared` is rejected
unless `isolation_strategy = "exclusive_lock"` is declared; Agenthicc then
serializes commands that use the cache with an OS advisory lock. Shared paths
cannot escape the cache root.

## Security and operational boundaries

- Commands run with the current user's OS permissions, inside the worker
  worktree. Bubblewrap mounts the host filesystem read-only and grants write
  access only to the selected worktree, its declared cache directories, and a
  private temporary directory. The worker path must also be verified as the
  registered Git worktree/branch before an action is offered. Trust is
  explicit consent to execute the reviewed manifest; it is not a container or
  privilege boundary against kernel vulnerabilities.
- `network = "none_declared"` runs in a network namespace with no external
  interfaces. `may_access_network` and `requires_network` permit network
  access; the labels are review declarations, not a domain allowlist.
- The filesystem is isolated for writes, not reads: trusted commands may be
  able to read files that the invoking user can read. Do not approve commands
  that exfiltrate secrets.
- Ordinary non-credential environment variables are inherited for toolchain
  compatibility. Credential-looking ambient names are filtered; explicitly
  declared `env` values are supplied, and configured secret values are
  redacted from output. Do not pass credentials to scripts that do not need
  them.
- Output capture is bounded. Timeouts, TUI/task cancellation, and Ctrl-C
  terminate the isolated command process group. A command that exits while
  leaving background children also has those children terminated before its
  cache locks are released; use the background-terminal/service lifecycle for
  persistent processes.
- Setup runs only in the worker worktree. The worktree's manifest digest must
  match the repository manifest shown during preflight; missing or divergent
  manifests leave dispatch pending and preserve the worktree for diagnosis.
- Caches are outside Git worktrees, and shared mutable caches are serialized.
  This does not isolate unrelated external resources such as fixed localhost
  ports, shared databases, Docker daemons, or remote services.

See [parallel workers](parallel-workers.md) for worker lifecycle, review,
integration, and recovery.
