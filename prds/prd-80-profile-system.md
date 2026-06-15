# PRD-80 — Profile System: Organisation-Level Policy Enforcement

## Background

Different organisations deploying agenthicc have different security and
compliance requirements.  A multinational corporation may need to prohibit
`--dangerously-skip-permissions` entirely; a financial institution may need to
force Guard mode for all users; a regulated environment may need to lock the
LLM provider to an approved endpoint.

None of these constraints can be expressed through the existing config stack
(`agenthicc.toml`, env vars, `--set`) because those are user-controlled.
There is currently no mechanism by which an organisation can enforce
restrictions that users cannot override.

This PRD introduces **Profiles** — an organisation-level policy layer that
sits above the entire user-controlled config stack and enforces restrictions
unconditionally.

---

## Core insight: Profiles are a bracket around the config stack

The existing config stack has one direction of precedence (low → high, user
always wins).  A Profile must enforce in **both** directions simultaneously:

```
Profile defaults    ← org provides starting values (user CAN override)
  └─ user TOML
       └─ project TOML
            └─ env vars
                 └─ --set
                      └─ CLIFlags
Profile policy      ← org enforces restrictions (user CANNOT override)
```

This "sandwich" model mirrors macOS MDM profiles and Windows Group Policy:

- **Defaults** (bottom of stack): org provides sensible starting values that
  users can freely override.
- **Policy** (top of stack): org enforces restrictions that no user action —
  not `--set`, not env vars, not `CLIFlags` — can bypass.

---

## Profile anatomy

```toml
# /etc/agenthicc/profile.toml  (system-distributed by IT)

[meta]
name        = "ACME Corp Security Profile"
version     = "1.0.0"
enforced_by = "IT Security Team <security@acme.corp>"

# ── Org-provided defaults (user CAN override) ─────────────────────────────────
[defaults.execution]
provider = "anthropic"

[defaults.behaviour]
confirm_exits = true

# ── Enforced policy (user CANNOT override anything below) ─────────────────────

[policy.flags]
# CLI flags that are completely unavailable in this profile.
# parse_cli() exits with an error if a disabled flag is passed.
disabled = ["dangerously_skip_permissions"]

[policy.modes]
# Restricts which RuntimeModes are available.
# Shift+Tab will only cycle through these.
allowed = ["Auto", "Plan", "Ask", "Review"]   # Guard and Debug removed

# Force a specific starting mode (user can still cycle within `allowed`).
# Empty string means no forced mode.
forced = ""

[policy.approvals]
# When true, ApprovalGate ignores CLIFlags.dangerously_skip_permissions entirely.
# The flag is also listed under [policy.flags].disabled, but this is the
# runtime enforcement layer — belt and suspenders.
skip_permissions_allowed = false

[policy.plugins]
allow_project_cli_plugins = true    # .agenthicc/cli/*.py
allow_user_cli_plugins    = false   # ~/.agenthicc/cli/*.py  ← org locks this

# When true, a project command with the same path as a user-global command
# is a hard error rather than a warning.  Prevents projects from silently
# overriding tools the user trusts from their personal global config.
strict_cli_shadow = true

[policy.features]
allow_headless = false              # no headless mode in this org

[policy.config]
# Config keys that are locked — user --set and env vars cannot override these.
# Applied as the LAST merge step in load_config(), winning over everything.
[policy.config.security]
sandbox_mode = true
```

---

## Data model

### `ProfileMeta`

```python
@dataclass(frozen=True)
class ProfileMeta:
    name:        str = "Default"
    version:     str = ""
    enforced_by: str = ""
```

### `ProfilePolicy`

```python
@dataclass(frozen=True)
class ProfilePolicy:
    """Org-enforced restrictions.  Users cannot override any of these."""

    # CLI flag restrictions
    disabled_flags:            frozenset[str] = frozenset()
    # e.g. frozenset({"dangerously_skip_permissions"})

    # Mode restrictions
    allowed_modes:             frozenset[str] | None = None   # None = unrestricted
    forced_mode:               str = ""                       # "" = not forced

    # Approval restrictions
    skip_permissions_allowed:  bool = True   # False → cli_flags ignored entirely

    # Plugin restrictions
    allow_project_cli_plugins: bool = True
    allow_user_cli_plugins:    bool = True
    strict_cli_shadow:         bool = False
    # When True, a project command with the same path as a user-global command
    # is a hard error instead of a startup warning.

    # Feature restrictions
    allow_headless:            bool = True

    # Locked config values (deep-merged LAST in load_config, after --set)
    locked_config:             dict[str, Any] = field(default_factory=dict)
```

### `Profile`

```python
@dataclass(frozen=True)
class Profile:
    meta:     ProfileMeta    = field(default_factory=ProfileMeta)
    defaults: dict[str, Any] = field(default_factory=dict)   # fed into load_config
    policy:   ProfilePolicy  = field(default_factory=ProfilePolicy)

# Sentinel for "no profile active" — all restrictions lifted.
NO_PROFILE = Profile()
```

---

## Profile discovery

`load_profile()` tries candidates in priority order and returns the first
one found.  It runs before `load_config()` and before `parse_cli()`.

```
Priority (1 = highest authority):

1. AGENTHICC_PROFILE=<path>             env var — explicit path override
2. AGENTHICC_PROFILE_URL=<url>          env var — remote fetch (cached, fail-closed)
3. /etc/agenthicc/profile.toml          Linux/macOS system-wide (MDM/Puppet/Ansible)
4. %PROGRAMDATA%\agenthicc\profile.toml Windows system-wide (Group Policy)
5. ~/.agenthicc/profile.toml            User's own personal profile
6. No profile active                    Returns NO_PROFILE
```

### Remote profile caching

When `AGENTHICC_PROFILE_URL` is set the profile is fetched and cached at
`~/.agenthicc/profile_cache.toml` with a configurable TTL
(`AGENTHICC_PROFILE_CACHE_TTL_SECONDS`, default 3600).

- On fetch failure: use the cached version if available.
- No cache + fetch failure: **exit with an error** (fail-closed — if the org
  requires a profile, running without it is not acceptable).

---

## Enforcement points

Each policy field maps to exactly one enforcement location.  No cross-cutting
logic; each is a single `if profile.policy.X` guard.

| Policy field | Enforced in | Mechanism |
|---|---|---|
| `disabled_flags` | `parse_cli()` | Exits with error before building `CLIContext` |
| `allowed_modes` | `ModeManager.__init__()` and `cycle()` | Filters the registry; cycling wraps within the allowed set only |
| `forced_mode` | `tui_session.py` startup | `mode_manager.set_by_name(profile.policy.forced_mode)` |
| `skip_permissions_allowed = False` | `ApprovalGate.before_tool_call()` | Ignores `cli_flags.dangerously_skip_permissions`; approval always required |
| `allow_project_cli_plugins = False` | `_discover_directory()` | Skips `.agenthicc/cli/` entirely |
| `allow_user_cli_plugins = False` | `_discover_directory()` | Skips `~/.agenthicc/cli/` entirely |
| `strict_cli_shadow = True` | `_discover()` conflict detection | Turns the user↔project shadow warning into a hard error that exits before the session starts |
| `allow_headless = False` | `main()` | Exits with error when `--headless` is passed |
| `locked_config` | `load_config()` | Applied as the absolute last `deep_merge` step, after `--set` |

---

## `load_config()` — the sandwich

```python
def load_config(
    ...
    profile: Profile | None = None,
) -> AgenthiccConfig:
    merged: dict[str, Any] = {}

    # Profile defaults (below user config — user can override)
    if profile and profile.defaults:
        merged = deep_merge(merged, profile.defaults)

    # Normal layers (unchanged)
    merged = deep_merge(merged, user_toml)
    merged = deep_merge(merged, project_toml)
    if env_overrides:
        merged = _apply_env_overrides(merged)
    if cli_overrides:
        merged = _apply_cli_overrides(merged, cli_overrides)

    # Profile locked config (above everything — user cannot override)
    if profile and profile.policy.locked_config:
        merged = deep_merge(merged, profile.policy.locked_config)

    return _dict_to_config(merged)
```

---

## The complete precedence stack with Profiles

```
─────────────────────────────────────────────────────────────────────
Profile.defaults        org-provided starting values      (lowest)
User TOML               ~/.agenthicc/agenthicc.toml
Project TOML            .agenthicc/agenthicc.toml
Env vars                AGENTHICC_*
--set flags             CLI arg
CLIFlags                --dangerously-skip-permissions, etc.
Profile.policy          org-enforced restrictions         (highest)
─────────────────────────────────────────────────────────────────────
```

One invariant: **Profile.policy always wins.**

---

## `AppState` and `CLIContext` gain `profile`

```python
# tui/conversation_store.py
class AppState:
    conversation:     ConversationStore
    input:            InputState
    active_mode:      Signal[RuntimeMode]
    overlay:          Signal[str]
    modal_open:       Signal[bool]
    pending_approval: Signal[ApprovalRequest | None]
    cli_flags:        CLIFlags
    profile:          Profile          # NEW — frozen, set once at startup

# cli/context.py
@dataclass(frozen=True)
class CLIContext:
    ...
    profile: Profile = field(default_factory=lambda: NO_PROFILE)  # NEW
```

`parse_cli()` calls `load_profile()` first so the profile is available to all
downstream logic including flag validation.

---

## User-visible behaviour

### Startup announcement

When a non-default profile is active, it is announced once at startup:

```
ℹ  Profile: ACME Corp Security Profile v1.0  (IT Security Team)
   Restrictions: Guard mode unavailable · --dangerously-skip-permissions disabled
```

### Disabled flag error

```
$ agenthicc --dangerously-skip-permissions
agenthicc: error: --dangerously-skip-permissions is disabled by the active profile
           Profile: ACME Corp Security Profile v1.0 (IT Security Team)
           Contact your administrator if you believe this is in error.
```

### TUI footer — profile badge

```
⏵⏵ Auto  (shift+tab to cycle)  │  ctrl+j = ↵                [ACME Corp]
```

### Mode cycling

Modes not in `policy.allowed_modes` are skipped by Shift+Tab and greyed out
in the `/help` overlay.  The user cannot reach them at all.

---

## File changes

| File | Change |
|---|---|
| `profile.py` | **New** — `ProfileMeta`, `ProfilePolicy`, `Profile`, `NO_PROFILE`, `load_profile()`, discovery, remote fetch + TTL cache |
| `cli/context.py` | Add `profile: Profile` to `CLIContext` |
| `cli/parser.py` | Call `load_profile()` before building `CLIContext`; check `disabled_flags` before accepting any CLI flag |
| `config.py` | `load_config()` accepts `profile` param; applies `profile.defaults` first and `profile.policy.locked_config` last |
| `tui/conversation_store.py` | Add `profile: Profile` to `AppState` |
| `runners/tui_session.py` | Pass `profile` to `_run_tui_session()`; apply `forced_mode` at startup |
| `tui/runtime/mode_manager.py` | `build_default_registry()` and `cycle()` filter by `profile.policy.allowed_modes` |
| `tools/approval.py` (PRD-78) | `ApprovalGate` checks `profile.policy.skip_permissions_allowed` before `cli_flags` |
| `cli/registry.py` (PRD-79) | `_discover_directory()` checks `profile.policy.allow_*_cli_plugins`; `_discover()` conflict detection respects `profile.policy.strict_cli_shadow` |
| `__main__.py` | Check `profile.policy.allow_headless` before dispatching to `_run_headless()` |

---

## Acceptance criteria

- [ ] `/etc/agenthicc/profile.toml` is discovered and loaded on Linux/macOS.
- [ ] `AGENTHICC_PROFILE=/path/to/custom.toml` overrides system discovery.
- [ ] `AGENTHICC_PROFILE_URL=https://...` fetches, caches, and uses a remote profile.
- [ ] On remote fetch failure with no cache, agenthicc exits with a clear error (fail-closed).
- [ ] Startup prints the profile name, version, and a one-line restriction summary when a non-default profile is active.
- [ ] `--dangerously-skip-permissions` listed in `policy.flags.disabled` causes an error with the profile name before the session starts.
- [ ] `policy.modes.allowed = ["Auto", "Plan"]` causes Shift+Tab to cycle only between Auto and Plan; Guard and other modes are unreachable.
- [ ] `policy.modes.forced = "Plan"` starts the session in Plan mode; the user can still cycle to other `allowed` modes.
- [ ] `policy.approvals.skip_permissions_allowed = false` means `ApprovalGate` always prompts even when `--dangerously-skip-permissions` would otherwise suppress prompts.
- [ ] `policy.config.security.sandbox_mode = true` locks `sandbox_mode` to `true`; `--set security.sandbox_mode=false` has no effect.
- [ ] `policy.plugins.allow_user_cli_plugins = false` skips `~/.agenthicc/cli/` entirely.
- [ ] `policy.features.allow_headless = false` causes `agenthicc --headless` to exit with an error citing the profile.
- [ ] `defaults.execution.provider = "anthropic"` is overridable by the user via `--set execution.provider=openai`.
- [ ] TUI footer shows the profile's `meta.name` (or a truncated version) as a dim badge.
- [ ] All existing tests pass when no profile is active (`NO_PROFILE`).
