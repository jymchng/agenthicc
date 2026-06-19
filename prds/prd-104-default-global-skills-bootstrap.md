# PRD-104 — Default Global Skills Bootstrap

## Summary

Agenthicc should ship with a curated set of built-in skills that are automatically installed into the user's global skill directory (`~/.agenthicc/skills/`) during first-time setup.

These skills behave exactly like user-created skills and can be inspected, modified, disabled, deleted, or overridden by the user.

The goal is to provide useful functionality immediately after installation while preserving the existing skill system as the single source of truth.

---

## Motivation

Today, a fresh agenthicc installation starts with no available skills.

Users must manually create skills before they can benefit from:

* Code review workflows
* Refactoring workflows
* Architecture planning
* Documentation generation
* Bug investigation
* Commit generation
* Release preparation

This creates unnecessary friction and makes the skill system less discoverable.

Providing a small set of default skills improves:

* First-run experience
* Skill discoverability
* Workflow adoption
* User onboarding

without introducing any special-case runtime behavior.

---

## Design Principles

### 1. Skills remain files

Default skills are not embedded into prompts.

They are materialized as ordinary skill directories:

```text
~/.agenthicc/skills/
├── review/
├── refactor/
├── architect/
├── docs/
├── commit/
└── debug/
```

Each contains:

```text
SKILL.md
reference.md (optional)
template.md (optional)
```

The runtime loads them through the existing skill discovery pipeline.

No special code path exists for default skills.

---

### 2. User ownership

After installation:

* Users may edit skills.
* Users may rename skills.
* Users may delete skills.
* Users may disable skills.
* Users may override skills.

Agenthicc never rewrites user-modified skill files.

---

### 3. Project skills still win

Discovery order remains:

```text
builtin bootstrap → user-global → project-local
```

Runtime precedence remains:

```text
project-local
    ↓
user-global
    ↓
builtins
```

A project skill with the same slug overrides the default skill.

---

## Bootstrap Lifecycle

### First launch

When agenthicc starts:

1. Resolve global skill directory.

```text
~/.agenthicc/skills/
```

2. Create directory if missing.

3. Install missing default skills.

4. Continue normal startup.

Example:

```text
Installed 6 default skills.
```

---

### Subsequent launches

If a skill already exists:

```text
~/.agenthicc/skills/review/
```

the installer skips it.

No overwrite occurs.

---

### Deleted skills

If the user deletes:

```text
~/.agenthicc/skills/review/
```

agenthicc treats that as intentional.

The skill is NOT automatically recreated.

A deletion marker is stored:

```text
~/.agenthicc/default_skills.json
```

Example:

```json
{
  "review": "deleted",
  "architect": "installed",
  "docs": "installed"
}
```

This prevents unwanted resurrection of removed skills.

---

## Built-in Skill Catalog

### review

Purpose:

* Review changes
* Find bugs
* Suggest improvements

Command:

```text
/review
```

Auto-trigger:

No.

---

### refactor

Purpose:

* Improve code structure
* Reduce complexity
* Modernize implementations

Command:

```text
/refactor
```

Auto-trigger topics:

```yaml
suggestedTopics:
  - refactor
  - cleanup
  - simplify
```

---

### architect

Purpose:

* System design
* API planning
* Architecture reviews

Command:

```text
/architect
```

Auto-trigger:

No.

---

### docs

Purpose:

* Documentation generation
* README updates
* API documentation

Command:

```text
/docs
```

Auto-trigger topics:

```yaml
suggestedTopics:
  - documentation
  - readme
  - docs
```

---

### debug

Purpose:

* Root-cause analysis
* Failure investigation
* Error diagnosis

Command:

```text
/debug
```

Auto-trigger topics:

```yaml
suggestedTopics:
  - bug
  - error
  - failure
  - crash
```

---

### commit

Purpose:

* Generate commit messages
* Prepare changelog entries
* Summarize changes

Command:

```text
/commit
```

Auto-trigger:

No.

---

## Skill Source Tracking

Each bootstrapped skill receives metadata:

```yaml
source: default
version: 1
```

inside SKILL.md frontmatter.

Example:

```yaml
---
name: Review
description: Review code changes
source: default
version: 1
---
```

This metadata is informational only.

Runtime behavior is unchanged.

---

## Updating Default Skills

### Existing users

Default skills are never silently modified.

If agenthicc v1.5 ships a newer review skill:

```yaml
version: 2
```

existing users continue using their current copy.

---

### Upgrade command

A future command may be added:

```text
/skills update-defaults
```

or

```bash
agenthicc skills update-defaults
```

which:

* shows diffs
* allows selective upgrades
* preserves user ownership

---

## Configuration

### Disable bootstrap

```toml
[skills]
install_default_skills = false
```

No skills are installed automatically.

---

### Custom default location

```toml
[skills]
default_skill_directory = "~/.agenthicc/skills"
```

Defaults to the current global skill directory.

---

## Startup Messages

### Fresh install

```text
Installed 6 default skills.
Loaded 6 skill(s) from ~/.agenthicc/skills
```

### Existing installation

```text
Loaded 6 skill(s) from ~/.agenthicc/skills
```

### User removed a skill

```text
Default skill 'review' intentionally removed.
Skipping reinstall.
```

(DEBUG level only.)

---

## Acceptance Criteria

| #  | Requirement                                                       |
| -- | ----------------------------------------------------------------- |
| 1  | First launch creates `~/.agenthicc/skills/` if missing.           |
| 2  | Missing default skills are installed automatically.               |
| 3  | Installed skills use the normal skill directory format.           |
| 4  | Runtime loads default skills through existing skill discovery.    |
| 5  | No special runtime behavior exists for default skills.            |
| 6  | Existing skills are never overwritten.                            |
| 7  | Deleted default skills are not recreated automatically.           |
| 8  | Project-local skills continue to override global skills.          |
| 9  | `/skills` lists default skills exactly like user-created skills.  |
| 10 | Default skills can be modified by the user.                       |
| 11 | Default skills can be deleted by the user.                        |
| 12 | `install_default_skills = false` disables bootstrap entirely.     |
| 13 | Startup logs clearly indicate when default skills were installed. |

---

## Non-Goals

The following are explicitly out of scope:

* Hidden built-in prompts
* Special runtime treatment for default skills
* Automatic updates of user-modified skills
* Remote skill downloads
* Marketplace integration
* Cloud synchronization

Default skills are ordinary skills that happen to be created automatically on first install.
