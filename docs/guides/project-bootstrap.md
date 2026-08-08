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
