# Project bootstrap

`agenthicc init` creates the minimal project scaffold. `/init` remains the
interactive guidance-preview command. The CLI bootstrap is deterministic: it reads only
bounded, well-known manifests (`pyproject.toml`, `package.json`, `Cargo.toml`,
`go.mod`, and `Makefile`) plus top-level directory names. It does not call a
provider, run shell commands, send project data over the network, or read
arbitrary source files.

## Initialize a project

From the project root, run:

```bash
agenthicc init
```

This creates:

- an empty `AGENTS.md` at the project root;
- the `.agenthicc/` directory; and
- `.agenthicc/.agenthicc.toml`, an exhaustive configuration template in which
  every section and assignment is commented out.

Because the template contains comments only, initialization does not enable a
provider, tool, plugin, browser, storage backend, or other setting. Uncomment
the options you need after reviewing them. The template is generated from the
typed settings model and includes dynamic examples for provider profiles,
MCP servers, agents, workflows, hooks, context windows, and storage mounts.

Initialization is idempotent and preserves existing files. Use `--force` only
when you explicitly want to replace both scaffold files:

```bash
agenthicc init --force
```

`--write` is retained as a compatibility alias and is no longer required.
Symlink targets and non-regular files are rejected.

The separate command below still creates the legacy active configuration path
for compatibility with existing projects:

```bash
agenthicc config init
```

## TUI guidance command

The separate guidance flow is available inside the terminal workspace:

```text
/init
/init write
/init write --force
```

`/init` previews only. Existing user-authored content is preserved. agenthicc
updates only the section between these markers:

```markdown
<!-- agenthicc:init:start -->
...
<!-- agenthicc:init:end -->
```

You can freely edit the rest of `AGENTS.md`; a later bootstrap refresh replaces
only the managed section.

## Generated guidance

The managed section records:

- project name and detected primary stack;
- top-level layout and known manifests;
- test directories and existing guidance files;
- conservative verification commands inferred from the manifests;
- baseline agenthicc rules for reading tests, preserving user changes, staying
  inside the workspace, protecting secrets, and running focused checks.

The generated file is a starting point, not an authoritative replacement for
project-specific engineering guidance. Review it before committing it to the
repository.

## Try it

`agenthicc init` writes project scaffolding, so preview its contract before
running it:

```bash
PYTHONPATH=src python -m agenthicc init --help
```

```text
usage: agenthicc init [-h] [--write] [--force]
```

The same project layout can be validated without writing anything:

```bash
PYTHONPATH=src python -m agenthicc config validate
```

```text
Configuration is valid: legacy execution settings (anthropic/deepseek-v4.1-flash)
```

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `agenthicc init` wrote nothing | `--write` was not passed | Re-run with `--write`; without it the command is a dry run |
| `init` refuses to overwrite an existing file | `--force` is required to replace files | Inspect the existing file before passing `--force` |
| The project tool or command directory is ignored | Discovery runs after the first TUI frame | Confirm the path is `.agenthicc/tools/` or `.agenthicc/commands/`, then reload in-session |
| A generated config is not picked up | A different config path is being resolved | Check the `path` field from `agenthicc mcp list --json` and compare it with where you wrote the file |
| Scaffolded instructions are not respected | They are project instructions, not policy | Instructions can be ignored by a model; capability metadata and mode filters are the enforcement layer |
| Committing the bootstrap is tempting but risky | It may include machine-specific paths or a trust manifest | Review `.agenthicc/` before committing; never commit secrets or an unreviewed hash |
| The project has no memory after a fresh clone | Project memory lives in the project directory and is not versioned | Recreate it, or keep only intentional, non-sensitive entries under version control |
| A bootstrap skill is missing | Bootstrap skills are installed from the skill bootstrap table | Check whether `[skills] install_default_skills` was set to `false` |
| Bootstrap wrote into your home instead of the project | A scope flag or `[skills] default_skill_directory` | Check the resolved directory before installing; `""` means `~/.agenthicc/skills` |

### Initiating is not the same as trusting

`init` scaffolds files; it does not vouch for them. Project-local tools,
commands, workflows, and skills are Python code, and loading them is code
execution. Read what was generated, then load it.

### Do not scaffold into a dirty tree

`init` writes files that discovery will later import. Scaffold first, review,
and commit — committing generated scaffolding together with unrelated
in-progress edits is how an unreviewed extension reaches a shared branch.
