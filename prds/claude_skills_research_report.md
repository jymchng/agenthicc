# Research Report: Claude Code Skills

## Executive Summary

Skills are Claude Code's extensibility mechanism that allows users to create custom commands and reusable workflows. They follow the [Agent Skills open standard](https://agentskills.io) and work across multiple AI development tools.

---

## 1. What Are Skills?

Skills are essentially **custom commands with instructions** that can be:
- Stored as markdown files with optional configuration
- Invoked directly using `/skill-name` syntax
- Triggered automatically by Claude when relevant
- Shared across projects and teams

### Core Benefits
- **Modularity**: Break complex workflows into reusable components
- **Efficiency**: Load only when needed (lazy loading)
- **Collaboration**: Share workflows via the open Agent Skills standard
- **Flexibility**: Combine instructions, templates, and supporting files

---

## 2. File Structure

A skill is organized as a directory containing:

```
skill-name/
  ├── SKILL.md      # Main instructions (required)
  ├── template.md   # Template for Claude to fill in (optional)
  ├── sample.md     # Example output (optional)
  ├── reference.md  # Detailed reference (optional - loaded when needed)
  └── helper.py     # Utility scripts (optional)
```

### Key File Purposes

| File | Required | Purpose |
|------|----------|---------|
| `SKILL.md` | Yes | Contains main instructions and frontmatter configuration |
| `template.md` | No | Template for Claude to fill in structured outputs |
| `sample.md` | No | Example output showing expected format |
| `reference.md` | No | Detailed reference loaded on demand |
| `helper.py` | No | Utility scripts Claude can execute |

---

## 3. Skill Levels & Precedence

Skills exist at multiple levels with an override hierarchy:

```
Enterprise → Personal → Project
```

| Level | Location | Precedence |
|-------|----------|------------|
| Enterprise | Organization-wide | Highest |
| Personal | `~/.claude/skills/` | Medium |
| Project | `.claude/skills/` | Lowest |

**Plugin skills** use namespaces and cannot conflict with other levels.

---

## 4. Bundled Skills

Claude Code includes built-in skills available in every session:

| Skill | Purpose |
|-------|---------|
| `/code-review` | Review code for issues and improvements |
| `/debug` | Debug issues in your code |
| `/batch` | Process multiple items in batches |
| `/loop` | Run iterative operations |
| `/claude-api` | Use Claude's API |

### Special App Skills
Three skills work together for development workflows:
- **Build skill**: Packages the project
- **Run skill**: Launches the app
- **Verify skill**: Confirms changes against running app

*Requires Claude Code v2.1.145 or later*

---

## 5. Configuration (Frontmatter)

Skills support YAML frontmatter at the top of `SKILL.md`:

```yaml
---
name: "Display Name"
description: "What the skill does and when to use it"
author: "username"
tags: ["python", "testing"]
suggestedTopics: ["code review", "pull request"]
disallowAutoTriggering: false
tools: ["Read", "Write", "Bash"]
disabledTools: ["DangerouslyLogout"]
maxTurnDepth: 10
model: "claude-3-5-sonnet-20241022"
---

# Skill content starts here...
```

### Frontmatter Reference

| Field | Type | Purpose |
|-------|------|---------|
| `name` | string | Display name in skill listings (defaults to directory name) |
| `description` | string | What the skill does (shown in listings, max 1,536 chars) |
| `author` | string | Skill creator |
| `tags` | list | Categories for organizing skills |
| `suggestedTopics` | list | Trigger phrases for auto-invocation |
| `disallowAutoTriggering` | bool | Prevent automatic loading |
| `tools` | list | Allowed tools when skill is active |
| `disabledTools` | list | Blocked tools during execution |
| `maxTurnDepth` | int | Max conversation turns for autonomous skills |
| `model` | string | Override model for this skill |
| `patterns` | list | Glob patterns for auto-loading |
| `files` | object | File access configuration |

---

## 6. Dynamic Context Injection

Skills can include dynamic values using substitution syntax:

| Placeholder | Purpose |
|-------------|---------|
| `{0}` | First argument |
| `{1}` | Second argument |
| `{session}` | Current session ID |
| `{effort}` | Current effort level |

### Example
```markdown
Analyze the changes in {0} and provide a summary.
Session: {session} (Effort: {effort})
```

---

## 7. Advanced Features

### File Access Control
```yaml
files:
  when: "added"    # "added", "present", or "none"
  trust: true      # Trust skill for file access
```

### Glob-Based Activation
```yaml
patterns:
  - "*.py"
  - "src/**/*.ts"
  - "package.json"
```

### Supporting Files Reference
```yaml
references:
  - file: "reference.md"
    reason: "Detailed API documentation"
  - file: "helper.py"
    reason: "Utility script for processing"
```

---

## 8. Creating a Skill - Step-by-Step

### 1. Directory Structure
```
.claude/skills/deploy/
├── SKILL.md
├── template.md
└── sample.md
```

### 2. SKILL.md Content
```markdown
---
name: "Deploy Application"
description: "Deploy the application to production with testing and verification"
tools: ["Bash", "Read", "Write"]
---

# Deploy Application

Deploy this application to production following these steps:

1. Run tests to verify code quality
2. Build the project
3. Deploy to production server
4. Verify the deployment

Use the template.md for structured output format.
```

### 3. Template (template.md)
```markdown
## Deployment Report

**Application**: {{application_name}}
**Version**: {{version}}
**Deployed by**: {{author}}
**Timestamp**: {{timestamp}}

### Steps Completed
- [ ] Tests passed
- [ ] Build successful
- [ ] Deployed to server
- [ ] Health checks passed

### Notes
{{notes}}
```

---

## 9. Lifecycle & Discovery

### Automatic Discovery
1. Claude scans `.claude/skills/` directories automatically
2. Works from parent and nested directories (monorepo support)
3. Nested skills loaded on demand when working with files in subdirectories

### Hot Reloading
- Changes to skill files take effect within the current session
- **Exception**: Creating a new top-level `skills` directory requires restarting Claude Code

---

## 10. Skill Invocation

### By User
```
/skill-name              # Basic invocation
/skill-name arg1 arg2    # With arguments
```

### By Claude
- Claude loads skills automatically based on:
  - Description matching context
  - File patterns matching current files
  - `suggestedTopics` keywords

### In Subagents
Skills can run in isolated subagents:
```
> skill-name          # Runs in subagent
>> skill-name         # Runs inline
```

---

## 11. Content Best Practices

1. **Keep it concise** - Every line costs tokens when loaded
2. **State what to do** - Not how or why
3. **Include examples** - Show the expected format
4. **Use templates** - For structured outputs
5. **Move reference docs** to separate files under 500 lines

---

## 12. Troubleshooting

| Issue | Solution |
|-------|----------|
| Skill not triggering | Check description includes trigger keywords |
| Skill triggers too often | Use `disallowAutoTriggering: true` |
| Description cut short | Keep under 1,536 characters total |
| Skill not found | Check file naming matches command |

---

## 13. Related Standards

- **[Agent Skills Standard](https://agentskills.io)** - Open standard for AI agent skills
- **MCP (Model Context Protocol)** - Extends tool capabilities
- **Claude Code CLI** - `claude code` command for running skills

---

## 14. Example: Skills in Practice

### Example 1: Build & Package Skill
```json
{
  "name": "Build & Package",
  "description": "Build and package Python projects",
  "commands": [
    {"command": "python -m build", "description": "Build wheel and sdist"},
    {"command": "twine upload dist/*", "description": "Upload to PyPI"}
  ]
}
```

### Example 2: Testing Workflow Skill
```json
{
  "name": "Testing Workflow",
  "description": "Run and manage tests with pytest",
  "commands": [
    {"command": "python -m pytest {path} -v", "description": "Run pytest with verbose output"}
  ]
}
```

---

## Conclusion

Skills represent a powerful and flexible way to extend Claude Code's capabilities. They bridge the gap between built-in commands and custom workflows, enabling:

- **Personal productivity** through custom shortcuts
- **Team collaboration** via shareable workflows
- **Project consistency** through version-controlled skills
- **Tool integration** with external systems (via MCP and hooks)

The lazy-loading nature of skills makes them efficient for long reference material, while the standardized format ensures compatibility across AI development tools.

---

**Report Generated**: 2024
**Reference**: [Claude Code Skills Documentation](https://code.claude.com/docs/en/skills)