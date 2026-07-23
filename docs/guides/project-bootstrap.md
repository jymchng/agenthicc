# Project bootstrap

`agenthicc init` and `/init` inspect a project locally and prepare a concise
`AGENTS.md` guidance file. The bootstrap is deterministic: it reads only
bounded, well-known manifests (`pyproject.toml`, `package.json`, `Cargo.toml`,
`go.mod`, and `Makefile`) plus top-level directory names. It does not call a
provider, run shell commands, send project data over the network, or read
arbitrary source files.

## Preview first

From the project root, run:

```bash
agenthicc init
```

The command prints a unified diff and does not write anything. If the proposal
looks correct, create a new file with:

```bash
agenthicc init --write
```

The command writes atomically to `AGENTS.md` in the current project root. If an
`AGENTS.md` already exists, the command refuses to overwrite it until the
review is explicit:

```bash
agenthicc init --write --force
```

`--force` does not bypass the project boundary, symlink checks, or the
plan-changed race check.

## TUI command

The same flow is available inside the terminal workspace:

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
