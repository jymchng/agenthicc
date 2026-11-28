---
title: "MCP-12: Configure MCP servers completely from the CLI, and bring them from other hosts"
status: Proposed
version: 1.0.0
date: 2026-11-28
priority: P2
related_prds:
  - MCP-00
  - MCP-02
  - MCP-05
  - MCP-10
  - prd-146
  - prd-172
---

# MCP-12 — Configure MCP servers completely from the CLI, and bring them from other hosts

`agenthicc mcp add` can write about a third of what a server definition supports, stores
a stdio command in a field named `url`, accepts a transport spelling the runtime does
not, and cannot read the configuration an operator already has for other hosts. The
rest has to be written by hand in TOML, which is where mistakes like the ones in MCP-02
and MCP-05 begin.

## Evidence

| # | What is true today | Where |
|---|---|---|
| 1 | `mcp add` writes only `name`, `url`, `transport`, `token` (as `${ENV}`), `auto_connect`, `reconnect_attempts` and `reconnect_delay_seconds`. | `cli/mcp_config.py:144-168`; `cli/commands/mcp.py:16-60` |
| 2 | A definition also supports `command` (argv), `cwd`, `env`, `env_vars`, `headers`, `env_headers`, `enabled`, `required`, `startup_timeout_s`, `tool_timeout_s`, `enabled_tools`, `disabled_tools`, `default_approval_mode`, `tool_approval`, `oauth` and `metadata`. None can be set from the CLI. | `tools/mcp_server_config.py:36-59` |
| 3 | For stdio, `mcp add` stores the command as a shell-style string in `url` (`_normalise_local_stdio_source`), and the runtime splits it with `shlex`. The argv form, `command`, is the documented-in-code preferred field. | `cli/mcp_config.py:179-217`; `tools/mcp_server_config.py:38,41,105-112` |
| 4 | `mcp add NAME path/to/server.py` rewrites the path to `uv run --project … lmcp run server.py --stdio`. The `lmcp` command comes from `lauren-mcp[cli]`; agenthicc depends on `lauren-mcp[http,ws]`. | `cli/mcp_config.py:195-217`; `pyproject.toml:32` |
| 5 | The CLI accepts transports `{stdio, ws, websocket, streamable, http}`; the runtime accepts `{stdio, streamable_http, sse, ws}`. (MCP-02 row 4.) | `cli/mcp_config.py:27`; `tools/mcp_server_config.py:23-30` |
| 6 | The CLI has ten subcommands: `add`, `list`, `get`, `remove`, `connect`, `disconnect`, `refresh`, `doctor`, `auth`, `logout`. There is no `import`, `export`, `enable` or `disable`. | `cli/manifest.py:222-246` |
| 7 | The configuration template's MCP example is a placeholder remote URL. It shows neither the argv form nor the secret-handling keys. | `config_template.py:169-175` |
| 8 | The guide documents `error_code: "remote_error"` for a server error, while the code returns `error_code: "internal"` with `reason: "remote_error"`. | `docs/guides/mcp.md:282`; `tools/mcp_content.py:176-184` |
| 9 | Other hosts keep MCP servers in their own files with their own shapes: a top-level `mcpServers` object is the common one; VS Code uses `servers` and Zed `context_servers`. **This comes from lauren-mcp's research and is not verified here;** the importer must be built from each host's current documentation. | `lauren-mcp` `prds/2026-11-landscape/VERSIONS-MATRIX.md` §6 (E-190…E-199) |

## Goals

1. Every field of a server definition can be set and changed from the CLI, with
   validation before anything is written.
2. stdio servers are stored as argv, never as a shell string.
3. One transport vocabulary in the CLI, the runtime, the docs and the template.
4. An operator can import servers from another host's config, and export to one, without
   retyping secrets.
5. The documentation matches the code.

## Non-goals

- A configuration GUI.
- Reading other hosts' credentials stores or OAuth tokens.
- Syncing configuration across machines.

## Design

### D1 — `mcp add` covers the model

```text
agenthicc mcp add NAME [--scope user|project]  -- COMMAND [ARG ...]        # stdio, argv after --
agenthicc mcp add NAME --url URL [--transport streamable_http|sse|ws]       # remote
    [--env KEY=VALUE] [--env-var NAME] [--cwd DIR]
    [--header NAME=ENVVAR] [--token-env ENVVAR]
    [--startup-timeout S] [--tool-timeout S]
    [--enable-tool NAME] [--disable-tool NAME]
    [--required] [--disabled] [--no-auto-connect]
    [--reconnect-attempts N] [--reconnect-delay S]
    [--capability TOOL=read,network]        # MCP-06
    [--elicitation off]                     # MCP-09 (sampling and roots are not adopted)
```

The flags are generated from the field list of `McpServerConfig`, so a new field cannot
be added without a flag (a test enumerates the dataclass fields). The command validates
with `McpServerConfig.from_dict(..., strict=True).validate()` **before** writing and
prints the stanza it will write. A literal secret (`--env` whose key looks secret, or a
token-looking value) is refused with the `--env-var` / `--header NAME=ENVVAR` form.
Stdio is always written as `command = ["…", …]`; the string-in-`url` form is no longer
produced, and existing ones keep working until a one-time `agenthicc mcp migrate`
rewrites them (a rewrite, not a shim: the loader's `url`-as-command path is then
removed).

### D2 — Add by talking to the server

With no arguments on a terminal, `mcp add` asks transport, command or URL, environment
and scope, **connects once**, shows the tools it found (and, after MCP-06, the
capabilities each would get), and saves only on confirmation. A project-scope save ends
with the MCP-05 notice that the server needs `mcp trust NAME`.

### D3 — One vocabulary

The CLI imports the transport names and aliases from `tools/mcp_server_config.py`
(MCP-02 A3). `mcp add`, `mcp get` and `mcp doctor` print the resolved protocol next to
the name (`streamable_http → MCP 2025-03-26+ over HTTP`). `sse` is accepted but prints a
deprecation warning: the HTTP+SSE transport is Deprecated in the current MCP revision.

### D4 — Import and export

```text
agenthicc mcp import FILE [--format auto|mcpServers|vscode|zed] [--scope …] [--dry-run]
agenthicc mcp export [--format mcpServers] [NAME ...]
```

`import` maps `command` + `args` to `command` argv, `env` to `env`, `url` + `headers`
to a remote entry, and the host's type field to a transport. **Secrets are never copied
as literals:** a value that looks secret is turned into an `env_vars` / `env_headers`
reference to a variable the operator names (the command prompts, or `--secret-map
KEY=ENVVAR`). Unknown keys are listed and skipped, not guessed. `--dry-run` prints the
TOML. Imported project-scope servers are untrusted (MCP-05). `export` writes the
common `mcpServers` shape with environment references left as references. The format
table is built from each host's documentation at implementation time and each format has
a fixture from that documentation, not from this PRD.

### D5 — Everyday operations

`mcp enable NAME` and `mcp disable NAME` flip `enabled` (the most common edit), and
`mcp set NAME KEY=VALUE` changes one scalar field with the same validation as `add`.
`mcp list --json` includes `scope`, `path`, `enabled`, `required`, `trusted` and the
resolved protocol; `mcp doctor --live` adds status, `protocol_version`, capabilities and
tool counts.

### D6 — The `lmcp` shortcut

`mcp add NAME path/to/server.py` either checks that `lmcp` resolves and says how to
install `lauren-mcp[cli]` when it does not, or the `mcp` dependency set adds the `cli`
extra. The first is recommended (an explicit message costs nothing and avoids pulling a
web server into every install).

### D7 — Template and documentation

`config_template.py` shows one stdio (argv, `env_vars`) and one remote
(`env_headers`) example, with the trust note. `docs/guides/mcp.md` is corrected
(`error_code`/`reason`), the "transport-alias trap" section is replaced by the single
vocabulary, and the CLI reference lists every flag.

## Failure handling

| Case | Result |
|---|---|
| Invalid definition | nothing written; the field and rule named |
| Name already exists | refused unless `--replace` |
| Import with unknown keys | listed and skipped; exit 0 with a warning count |
| Import of a literal secret | refused until mapped to an environment variable |
| `mcp add` cannot connect (D2) | offers to save anyway, marked `auto_connect = false` |

## Security

Secrets stay in the environment (existing policy, now enforced at the command line, not
only by the validator). `import` never writes a secret literal and never reads another
host's credential store. Project-scope writes inherit MCP-05: the entry is untrusted
until the operator approves it, and the command says so.

## Rollout and migration

1. Needs MCP-02 (one vocabulary) and benefits from MCP-05/06/09 (their flags).
2. D1, D3 and D7 first; then D4, D5, D2; D6 with D1.
3. Ship `mcp migrate` in the same release as D1; the next release removes the
   `url`-as-command loader path (no dual path survives).
4. `llms-full.txt` and `CHANGELOG.md` updated in the same changes.

## Acceptance criteria

1. A unit test enumerates `dataclasses.fields(McpServerConfig)` and fails if any field
   lacks an `mcp add` flag or an explicit "not settable" reason.
2. For every flag, `mcp add … ; mcp get --json` round-trips the value through
   `McpServerConfig.from_dict(strict=True)` unchanged.
3. `mcp add db -- npx -y @acme/db` writes `command = ["npx", "-y", "@acme/db"]` and no
   `url`.
4. `mcp add --env API_KEY=abc123` is refused with the reference form.
5. `mcp import` of a fixture per supported host format produces the expected TOML under
   `--dry-run`, with secrets mapped to references and unknown keys reported.
6. `mcp migrate` rewrites every `url`-as-command stanza and a second run changes nothing.
7. `mcp add NAME server.py` without `lmcp` available fails with the install instruction.
8. `grep -n "remote_error" docs/guides/mcp.md` matches the code's `reason` field.

## Verification

CLI tests with a temporary config root; a round-trip property test (`hypothesis`, already
a dev dependency) over generated definitions; mutation: drop one flag, then the
enumeration test must fail.

## Risks

- Host config formats change. The importer is isolated per format and fixture-tested, and
  an unknown key never fails an import.
- `mcp migrate` rewrites user files. It writes a `.bak` beside the file and shows a diff
  first.
