# Agenthicc Skills

Skills are self-contained reference guides designed for AI assistant consumption.
Each skill lives in its own directory as a `SKILL.md` file with YAML frontmatter
describing what the skill covers. Skills are intentionally dense — they pack
everything an AI needs to write correct code in a single readable document.

## How to use skills

Point your AI assistant at the skill file for the task at hand:

```
Read /path/to/agenthicc/skills/running-the-tui/SKILL.md before helping me use the TUI.
```

Or reference by name in your project instructions / system prompt:

```
When the user asks about the TUI, consult the skill: running-the-tui
```

## Available skills

| Skill | File | Tags | Summary |
|---|---|---|---|
| `running-the-tui` | [skills/running-the-tui/SKILL.md](running-the-tui/SKILL.md) | tui, terminal, prompt_toolkit | Full guide to the interactive TUI: layout, key bindings, slash commands, HITL approval, headless mode |
| `writing-agents` | [skills/writing-agents/SKILL.md](writing-agents/SKILL.md) | agents, comm-tools, signalbus | How to write agents wired to the kernel via CommunicationTools and SignalBus |
| `extending-with-hooks` | [skills/extending-with-hooks/SKILL.md](extending-with-hooks/SKILL.md) | hooks, lifecycle, recovery | Implementing and registering LifecycleHooks for audit, rate limiting, and recovery |
| `using-memory` | [skills/using-memory/SKILL.md](using-memory/SKILL.md) | memory, sqlite, artifacts | Three-tier memory architecture: session LRU, project SQLite, artifact sharing |
| `headless-api` | [skills/headless-api/SKILL.md](headless-api/SKILL.md) | api, rest, websocket, fastapi | REST and WebSocket API: endpoints, auth, Python client examples |
| `testing-agenthicc` | [skills/testing-agenthicc/SKILL.md](testing-agenthicc/SKILL.md) | testing, pytest, fixtures | Fixtures, EventBusTestHarness, MockTransport, drain() timing, AgentRunner in tests |

## Skill format

Each `SKILL.md` starts with YAML frontmatter:

```yaml
---
skill: <slug>
version: <semver>
tags: [tag1, tag2]
summary: One-sentence description.
---
```

Followed by:

1. **When to use this skill** — preconditions and use cases
2. **Core concepts** — key types and signatures
3. **Complete examples** — runnable code blocks
4. **Common errors** — pitfalls and fixes
5. **Key points** — quick-reference bullets
