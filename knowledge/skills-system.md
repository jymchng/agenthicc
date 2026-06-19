# Skills System

End users can place skill files in `./.agenthicc/skills/` and they are
automatically loaded into agenthicc at session startup.

---

## Skill file format

Each skill is a **subdirectory** under the skills directory, not a single file:

```
.agenthicc/skills/
  deploy/
    SKILL.md           ← required
    reference.md       ← optional — injected via {reference} placeholder
    template.md        ← optional — appended to body
```

`SKILL.md` starts with YAML frontmatter:

```yaml
---
name: Deploy
description: Deploy the application to production
author: team
tags: [ops, deploy]
suggestedTopics: [deploy, release, production, push]
disallowAutoTriggering: false
tools: [run_bash, git_push]
disabledTools: []
maxTurnDepth: 200
model: ""
---
Body markdown with optional placeholders:
  !`git log --oneline -5`   ← replaced with command stdout (15 s timeout)
  {0}, {1}                   ← positional args from /deploy arg1 arg2
  {session}                  ← current session ID
  {reference}                ← contents of reference.md
```

Source: `skills/loader.py:13–71`

---

## Load order and precedence

`discover_skills()` scans two directories in this order (`loader.py:73–88`):

1. `~/.agenthicc/skills/` — user-global (loaded first)
2. `./.agenthicc/skills/` — project-local (loaded second, **wins** on slug collision)

Both write into the same dict keyed by slug; the project write happens second
so it overwrites any user-global skill with the same slug.  Verified by
`tests/unit/test_skills_loader.py:70–83`.

---

## Runtime injection path

```
tui_session._build_session_context()         tui_session.py:176–178
  → discover_skills(project_dir=".agenthicc", user_dir="~/.agenthicc")
  → stored in SessionContext.skills           tui_session.py:308
  → registered as /{slug} slash commands     tui_session.py:245–257

On each agent turn:
  AgentTurnRunner._inject_skills()           agent_turn.py:261–273
    → find_matching_skills(user_text, skills)
         matches suggestedTopics words against user message words
    → process_skill_body(skill, args, cwd)
         runs !`cmd` placeholders, substitutes {0}…
    → self._skill_suffix = "## Skill: …\n{body}"

  AgentTurnRunner._build_agent()             agent_turn.py:291–294
    → system = base + system_prompt_suffix + skill_suffix + tool_describe
```

Skills land in the **system prompt** of the agent turn, appended after the
mode/workflow system prompt and before the tool descriptions.

---

## Two activation paths

| Path | How | Effect |
|---|---|---|
| **Auto** | User message contains a word from `suggestedTopics` | Skill body appended to system prompt silently |
| **Explicit** | User types `/{slug}` | Skill body prepended to next message as `[Skill /{slug} — execute the following instructions:]` |

`disallowAutoTriggering: true` suppresses auto-matching, leaving only the
explicit slash-command path.

---

## Constraints

| Constraint | Detail |
|---|---|
| Missing `SKILL.md` | Skill silently skipped (`loader.py:44–46`) |
| Invalid YAML frontmatter | Logged at WARNING, skill skipped (`loader.py:69–71`) |
| Hidden directories | Any directory starting with `.` is excluded |
| `!`cmd`` subprocess | 15-second timeout (`runner.py:37`) |
| Body size | No explicit limit; lazy-loaded on first access (`loader.py:31–40`) |
| `reference.md` absent | `{reference}` replaced with `"[reference.md not found]"` |
