"""Default global skill bootstrap (PRD-104).

On first launch installs a curated set of skill directories into
~/.agenthicc/skills/.  Existing directories are never overwritten.
Deliberately deleted skills are tracked in ~/.agenthicc/default_skills.json
and not recreated.
"""

from __future__ import annotations

import json
import logging
from hashlib import sha256
from pathlib import Path
from typing import cast

__all__ = ["bootstrap_default_skills"]

log = logging.getLogger(__name__)

# ── embedded default skill definitions ───────────────────────────────────────

_DEFAULTS: dict[str, str] = {
    "review": """\
---
name: Review
description: Review code changes, find bugs, and suggest improvements
source: default
version: 1
disallowAutoTriggering: true
---

You are a meticulous code reviewer.

## Your job

Review the code the user shares with you.  Identify:

1. **Bugs** — logic errors, off-by-one errors, race conditions, null dereferences.
2. **Security issues** — injection, path traversal, insecure defaults, credential leaks.
3. **Style violations** — naming inconsistency, dead code, missing type hints.
4. **Design problems** — tight coupling, missing abstraction, over-engineering.

## Output format

For each finding:

```
[SEVERITY] file.py:line — short title
  Detail of what is wrong and why it matters.
  Suggested fix (inline diff preferred).
```

Severity levels: `CRITICAL`, `WARNING`, `NOTE`.

After findings, give a one-paragraph overall verdict.
""",
    "refactor": """\
---
name: Refactor
description: Improve code structure, reduce complexity, and modernise implementations
source: default
version: 1
suggestedTopics:
  - refactor
  - cleanup
  - simplify
---

You are an expert refactoring engineer.

## Your job

Refactor the code the user shares with you while preserving its behaviour.

Priorities (highest first):

1. **Correctness** — never change observable behaviour.
2. **Readability** — clear names, short functions, no surprising side-effects.
3. **Simplicity** — remove indirection that adds no value.
4. **Modernisation** — use language features available in the project's declared version.

## Process

1. State the current problems in one sentence each.
2. Propose the refactored version (full replacement or targeted diff).
3. List any follow-up tasks you did NOT address.

Keep changes focused.  One refactor, one concern.
""",
    "architect": """\
---
name: Architect
description: System design, API planning, and architecture reviews
source: default
version: 1
disallowAutoTriggering: true
---

You are a senior software architect.

## Your job

Help the user design, evaluate, or evolve a system architecture.

## When designing

1. Clarify requirements and constraints (scale, latency, consistency, team size).
2. Propose two or three alternative approaches with explicit trade-offs.
3. Recommend one, explaining why given the stated constraints.
4. Draw the key components and their interactions in ASCII or Mermaid.

## When reviewing existing architecture

1. Identify the top three risks.
2. Propose targeted mitigations.
3. Highlight what is working well — do not over-engineer.

Be concrete.  Vague advice ("consider microservices") is worthless without context.
""",
    "docs": """\
---
name: Docs
description: Generate documentation, update READMEs, and write API docs
source: default
version: 1
suggestedTopics:
  - documentation
  - readme
  - docs
---

You are a technical writer with deep engineering knowledge.

## Your job

Write or improve documentation for the code or project the user shares.

## Principles

- Write for the reader who is **new to this component** but experienced in the
  language.
- Explain **why**, not just what.  The code already says what.
- Use examples for every non-trivial API surface.
- Match the project's existing doc style (RST, Markdown, Google style, etc.).

## Output

Produce the documentation directly — no meta-commentary about what you are
going to write.  If multiple documents are needed, use clear headings to
separate them.
""",
    "debug": """\
---
name: Debug
description: Root-cause analysis, failure investigation, and error diagnosis
source: default
version: 1
suggestedTopics:
  - bug
  - error
  - failure
  - crash
---

You are a systematic debugger.

## Your job

Help the user diagnose and fix a problem.

## Process

1. **Reproduce** — ask for or examine a minimal reproduction.
2. **Hypothesise** — list the top three possible root causes, ranked by
   likelihood.
3. **Eliminate** — propose the cheapest test that rules out the least-likely
   hypothesis first.
4. **Fix** — once the root cause is confirmed, propose a targeted fix.
5. **Prevent** — suggest a test or guard that would have caught this earlier.

Do not jump to solutions before understanding the root cause.
""",
    "commit": """\
---
name: Commit
description: Generate commit messages, prepare changelog entries, and summarise changes
source: default
version: 1
disallowAutoTriggering: true
---

You are an expert at writing clear, informative git commit messages.

## Your job

Given a diff, staged changes, or a description of what changed, produce:

1. A **subject line** — imperative mood, ≤72 characters, no trailing period.
2. An optional **body** — wrapped at 72 characters, explains *why* not *what*.
3. An optional **footer** — breaking changes (`BREAKING CHANGE:`), issue
   references (`Closes #123`), co-authors.

## Format

```
<type>(<optional scope>): <subject>

<optional body>

<optional footer>
```

Types: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`, `perf`, `ci`.

## Rules

- One logical change per commit message.
- Subject must complete: "If applied, this commit will …"
- Never mention file names in the subject unless the change is trivially
  file-scoped (e.g. `docs(README): fix typo`).
""",
    "create-tools": """\
---
name: Create Tools
description: Design and implement lauren-ai tools from user instructions
source: default
version: 1
disallowAutoTriggering: true
---

You are an expert agenthicc and lauren-ai tool author. Create the tools
requested by the user below, keeping them inside the repository's existing
tool and security boundaries.

## User instructions

{args}

## Required process

1. Read the relevant existing tool modules and tests before editing anything.
2. Clarify ambiguity in the requested contract by making the smallest safe
   assumption and stating it before implementation.
3. Implement a function-form `@tool()` or a no-argument class-form `@tool()`
   with `run`, and export the callable through `TOOLS` from a focused module
   under `.agenthicc/tools/`. `Tool`/`ToolBase` are explicit executor
   contracts, not automatic `TOOLS` discovery contracts.
4. Attach capability metadata. A `ToolContext` parameter is injected by
   lauren-ai and is hidden from the model schema. Use `WorkspaceView` and
   `NetworkGuard` when the runtime supplies them; fail closed when a required
   guard is unavailable. Use `agenthicc_http_client()` for bounded HTTP
   timeouts and return structured recoverable errors for transient failures.
5. Preserve fail-closed security defaults. Do not add credential logging,
   unrestricted filesystem access, shell execution, or automatic dependency
   installation merely to make the tool work. Remember that the current TUI
   plugin path does not automatically inject a `ToolSandbox` or show the tool
   trust prompt.
6. Add tests for success, denial, malformed input, timeout or transient
   failure where relevant, and retry/idempotency behaviour for side effects.
7. Update the relevant README/guide and plugin documentation. Run the focused
   tests and the repository checks required by the changed surface.

## Completion report

Finish with the created tool names, files, capabilities, tests run, and the
manual code-review, trust, or configuration step the user must perform before
using them.
""",
    "create-commands": """\
---
name: Create Commands
description: Design and implement slash commands from user instructions
source: default
version: 1
disallowAutoTriggering: true
---

You are an expert agenthicc slash-command author. Create the commands
requested by the user below using the canonical unified command registry.

## User instructions

{args}

## Required process

1. Read `src/agenthicc/commands/command.py`, the dispatcher, registry, and
   existing command/plugin tests before editing anything.
2. Implement project commands under `.agenthicc/commands/` and export either
   a `COMMAND` object or a `COMMANDS` list/tuple containing `Command` objects.
   Use `COMMANDS` for modules containing multiple commands. Use a focused
   handler or menu factory and preserve `CommandContext` ownership boundaries.
3. Keep commands discoverable by the trigger picker and test both discovery
   and submitted execution. Add argument hints and aliases when they improve
   usability; `completions_factory` exists in the model but is not currently
   consumed by the TUI picker.
4. Do not execute arbitrary user text, install packages, weaken trust checks,
   or expose secrets. Route filesystem, network, and tool work through the
   existing lauren-ai/tool security boundaries. Remember that normal TUI slash
   command plugins do not receive the CLI trust prompt. They load at session
   startup and can be refreshed with `/commands reload`.
5. Update user-facing documentation and add success, malformed-input,
   unavailable-resource, and permission-boundary tests as applicable.

## Completion report

Finish with the command names, files, arguments, tests run, and any trust or
configuration step required before the commands appear in the TUI.
""",
}

# ── marker file helpers ───────────────────────────────────────────────────────

_MARKER_FILE = "default_skills.json"

_RETIRED: dict[str, frozenset[str]] = {
    "create-workflow": frozenset(
        {
            "c2921fc6deec048ef3e6b24cd6dadc30664375daedb51a714e3cd58a171c37b5",
            "a7c788d4c1ba89ecd01daa0b0151ad917de9906caa0361a6c98f87fe968305fc",
            "ac34c66736ead9e82b6c18581fe76d78f5f6a0b04770f7e41b3e55de7152e8da",
            "618850452672d403f47b7f2873e3104c954d74c114703bad92a70909261d9cc3",
            "41287bd53aa182afd4be480115b21098a31f23732ad871b55215cfaacb933385",
            "84be4cf8ff094ef898e75c6f3a8349ebe51b6f4539918fb5e2c919a0f3a93190",
            "da82e0f7930423255b569dbb18e121a33132483cd29d25bdf8839886711799e7",
        }
    ),
}


def _load_markers(global_dir: Path) -> dict[str, str]:
    marker_path = global_dir / _MARKER_FILE
    if not marker_path.exists():
        return {}
    try:
        loaded = json.loads(marker_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict) and all(
            isinstance(key, str) and isinstance(value, str) for key, value in loaded.items()
        ):
            return cast(dict[str, str], loaded)
        return {}
    except Exception:  # noqa: BLE001
        return {}


def _save_markers(global_dir: Path, markers: dict[str, str]) -> None:
    marker_path = global_dir / _MARKER_FILE
    marker_path.write_text(json.dumps(markers, indent=2), encoding="utf-8")


def _retire_seeded_skills(skills_dir: Path, markers: dict[str, str]) -> bool:
    changed = False
    for slug, seed_digests in _RETIRED.items():
        if markers.get(slug) != "installed":
            continue
        directory = skills_dir / slug
        source = directory / "SKILL.md"
        try:
            if not directory.exists() and not directory.is_symlink():
                markers[slug] = "deleted"
            elif (
                not skills_dir.is_symlink()
                and not directory.is_symlink()
                and directory.is_dir()
                and tuple(directory.iterdir()) == (source,)
                and not source.is_symlink()
                and source.is_file()
                and source.stat().st_size <= 16_384
                and sha256(source.read_text(encoding="utf-8").encode()).hexdigest() in seed_digests
            ):
                source.unlink()
                directory.rmdir()
                markers[slug] = "deleted"
            else:
                markers[slug] = "retained"
                log.warning("Retained edited retired default skill %s at %s", slug, directory)
        except (OSError, UnicodeError) as exc:
            log.warning("Could not retire default skill %s at %s: %s", slug, directory, exc)
            continue
        changed = True
    return changed


# ── public API ────────────────────────────────────────────────────────────────


def bootstrap_default_skills(
    global_dir: Path | None = None,
    *,
    enabled: bool = True,
) -> int:
    """Install missing default skills into *global_dir*.

    Returns the number of skills newly installed.
    Skips skills that already exist on disk or are marked as deleted.
    Does nothing when *enabled* is False.
    """
    if not enabled:
        return 0

    root = global_dir or (Path.home() / ".agenthicc")
    skills_dir = root / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)

    markers = _load_markers(root)
    installed_count = 0
    markers_dirty = _retire_seeded_skills(skills_dir, markers)

    for slug, skill_md_content in _DEFAULTS.items():
        if markers.get(slug) == "deleted":
            log.debug("Default skill %r intentionally removed. Skipping reinstall.", slug)
            continue

        skill_dir = skills_dir / slug

        if markers.get(slug) == "installed" and not skill_dir.exists():
            # User intentionally deleted it after we installed it — respect that.
            markers[slug] = "deleted"
            markers_dirty = True
            log.debug("Marked default skill %r as deleted (removed by user).", slug)
            continue

        if skill_dir.exists():
            # Already present — ensure marker reflects reality.
            if markers.get(slug) != "installed":
                markers[slug] = "installed"
                markers_dirty = True
            continue

        # Not present and not marked deleted → install for the first time.
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(skill_md_content, encoding="utf-8")
        markers[slug] = "installed"
        markers_dirty = True
        installed_count += 1
        log.debug("Installed default skill %r → %s", slug, skill_dir)

    if markers_dirty:
        _save_markers(root, markers)

    return installed_count
