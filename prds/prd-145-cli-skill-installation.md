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

Provide a non-interactive way to add validated `SKILL.md` skills from local
paths, direct files, and generic GitHub repository sources to the current
project or the user-global skill directory:

```text
agenthicc skills add SOURCE [--project | --global] [--name NAME]
                       [--skill NAME[,NAME]] [--all]
```

Project scope is the default and writes `.agenthicc/skills/<slug>/`. `--global`
writes `~/.agenthicc/skills/<slug>/`; `--project` makes the default explicit.
Repository sources install all discovered skills by default; `--skill` selects
specific names and `--all` makes that full set explicit. Existing skill
directories are never overwritten.

## Source and safety contract

- Direct HTTPS URLs must point to `SKILL.md`; GitHub `/tree/<revision>/<skill>`
  and `/blob/<revision>/<skill>/SKILL.md` links remain supported.
- GitHub repository URLs, `.git` URLs, `owner/repo` shorthand, and local
  repository roots are cloned/scanned for valid `SKILL.md` directories.
- Conventional skill containers are prioritized and duplicate skill names are
  installed once. Companion files in a selected skill directory are retained.
- Local skill directories/files are supported for offline and automation use.
- Content is limited to 1 MiB and must be UTF-8.
- The existing skill parser validates metadata before installation.
- Installation stages each file or directory inside the selected skill root and
  atomically moves it into the canonical kebab-case directory.
- No overwrite or dependency installation is performed.

## Verification

`tests/unit/test_skill_installer.py` covers both scopes, direct and repository
discovery, GitHub URL normalization, selection/deduplication, companion-file
copying, malformed sources, overwrite protection, parser flags, and CLI errors.
Manual CLI help verification and the repository static/full test gates are
recorded with the implementation.
