---
title: "PRD-145: CLI Skill Installation"
status: Implemented
version: 1.0.0
created: 2026-07-24
related_prds:
  - PRD-22   # Skill discovery and lazy loading
  - PRD-104  # Default skill bootstrap
  - PRD-139  # Product expansion
tags:
  - cli
  - skills
  - extensions
  - download
---

# PRD-145 — CLI Skill Installation

## Summary

Provide a non-interactive way to add a validated `SKILL.md` skill to the
current project or the user-global skill directory:

```text
agenthicc skills add SOURCE [--project | --global] [--name NAME]
```

Project scope is the default and writes `.agenthicc/skills/<slug>/SKILL.md`.
`--global` writes `~/.agenthicc/skills/<slug>/SKILL.md`; `--project` makes the
default explicit. Existing skill directories are never overwritten.

## Source and safety contract

- HTTPS URLs must point to `SKILL.md`; GitHub `/tree/<revision>/<skill>` and
  `/blob/<revision>/<skill>/SKILL.md` links are normalized to raw content URLs.
- Local skill directories/files are supported for offline and automation use.
- Content is limited to 1 MiB and must be UTF-8.
- The existing skill parser validates metadata before installation.
- Installation stages the file inside the selected skill root and atomically
  moves it into the canonical kebab-case directory.
- No overwrite or dependency installation is performed.

## Verification

`tests/unit/test_skill_installer.py` covers both scopes, discovery, remote URL
normalization, malformed sources, overwrite protection, parser flags, and CLI
errors. Manual CLI help verification and the repository static/full test gates
are recorded with the implementation.
