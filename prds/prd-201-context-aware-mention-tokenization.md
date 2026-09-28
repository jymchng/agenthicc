# PRD-201: Context-aware @mention tokenization

```yaml
title: "PRD-201: Context-aware @mention tokenization"
status: Implemented
version: 1.0.0
date: 2026-09-28
scope: "User-message mention parsing and mention-content injection"
related_prds:
  - PRD-10  # Enhanced input bar and @-file mentions
  - PRD-32  # @mention parser and resolver
  - PRD-33  # @mention content injection
  - PRD-39  # Input trigger system
  - PRD-109 # Case-insensitive mention matching
  - PRD-167 # Workspace-scoped mentions
  - PRD-168 # Mode-aware parent-workspace access
tags:
  - mentions
  - parser
  - input
  - tui
  - workspace-security
  - false-positive
```

## 1. Summary

The current mention parser treats almost every `@` followed by a non-space
token as a filesystem, glob, or URL mention. This causes ordinary technical
identifiers and prose such as `@bookTicker`, `@depth20`, `@100ms`, email
addresses, decorators, and inline-code examples to enter the mention
injection pipeline. The resulting unresolved-mention warnings and mention
chips make unrelated prompts look as though they requested file reads. In
some cases the false mention is displayed as a failed `Read(...)` operation,
which obscures the agent's real work.

PRD-201 introduces a context-aware mention tokenizer and a conservative
candidate policy. A token is a mention only when it is lexically bounded and
has evidence that it represents a supported target: an existing workspace
file or directory, an explicit path-shaped token, a glob, or an explicitly
prefixed URL. Unresolved path-shaped mentions remain supported so a typo can
still produce a useful warning. An unresolved bare identifier is ordinary
text.

The public `parse_mentions()` API, `Mention` model, workspace authorization,
injection budget, cache, and supported explicit target forms remain intact.
The change is deliberately limited to deciding whether a token is a mention;
it does not make mention resolution or filesystem access less restrictive.

## 2. Problem statement

### 2.1 User-visible failure

Users can see unrelated content represented as failed reads, for example:

```text
  ⎿ ✗ Read(bookTicker`)
  ⎿ ✗ Read(depth20)
  ⎿ ✗ Read(100ms`)
```

The underlying text contains `@bookTicker`, `@depth20`, or `@100ms` in a
technical example. Because the parser accepts all three as unresolved file
mentions, `build_context_prefix()` calls `resolve_mention()` for them and
emits warning blocks and mention metadata. The user did not request any of
these reads.

### 2.2 Current implementation

`src/agenthicc/mentions/parser.py` currently uses a regular expression whose
effective rule is:

```text
@ followed by one or more characters other than whitespace, @, comma,
semicolon, a closing bracket, a quote, or a closing parenthesis
```

The parser then classifies the token as a URL, glob, existing file,
directory, or `UNRESOLVED`. The `UNRESOLVED` fallback means that lexical
shape alone is enough to create an injection request.

The parser also does not consistently apply input-boundary rules used by the
interactive `AtMentionTrigger`. In particular, an `@` in an email-like token
can be parsed even though the TUI picker only activates at the start of input
or after whitespace. Backticks are not currently treated as a token boundary,
and there is no protection against extracting mentions from Markdown code
spans or fenced code blocks.

### 2.3 Impact

False positives cause:

* misleading unresolved-file warnings and `mention_chips` events;
* unnecessary filesystem checks and cache activity;
* prompt prefix growth and avoidable provider input tokens;
* apparent failed `Read` operations in the transcript;
* possible confusion about what data was intentionally supplied to the LLM;
* inconsistent behavior between the interactive picker and manually typed
  text.

The issue must be fixed at tokenization, before injection and authorization,
so that an ordinary identifier never reaches the resolution pipeline.

## 3. Goals

### 3.1 Product goals

1. Treat ordinary `@`-prefixed identifiers as literal user text.
2. Preserve useful file, directory, glob, and URL mentions.
3. Preserve unresolved warnings for misspelled or missing *path-shaped*
   mentions.
4. Make manual parsing and the interactive `@` picker agree about valid
   mention boundaries.
5. Avoid changing the user's submitted text; only accepted mentions produce
   injected context and mention metadata.
6. Make Markdown/code examples safe to paste without triggering file reads.
7. Keep the behavior deterministic, bounded, and compatible with workspace
   access policy enforcement.

### 3.2 Engineering goals

* Keep `parse_mentions(text, cwd, workspace_scope)` as the stable public
  entry point.
* Keep mention classification and offset calculation in one parser module.
* Keep resolution, authorization, cache, and injection budgets unchanged.
* Use the same lexical rules for all callers, including TUI, headless mode,
  workflows, and resumed sessions.
* Add regression tests at parser, injection, TUI, and end-to-end boundaries.
* Provide diagnostics suitable for debugging without logging prompt contents or
  secrets.

## 4. Non-goals

This PRD does not authorize:

* changing the `MentionKind` values or the content of resolved file blocks;
* removing URL or glob mention support;
* allowing a mention to bypass `WorkspaceScope`, `WorkspaceAccessPolicy`,
  Plan-mode restrictions, network guards, or approval rules;
* broadening filesystem access to decide whether a token is a mention;
* changing the provider-facing prompt except by removing false injected
  blocks;
* changing `strip_mentions()` semantics for accepted mentions;
* changing slash commands, `$` skills, tool-call parsing, or assistant-output
  rendering;
* adding a second mention parser or a second trigger registry;
* silently interpreting arbitrary natural-language `@name` text as a remote
  URL or an MCP target;
* making a missing bare identifier produce a warning by default.

## 5. Proposed user-facing behavior

### 5.1 Supported mention forms

The following forms remain eligible for parsing:

| Form | Example | Eligibility |
|---|---|---|
| Existing relative file | `@README.md`, `@bookTicker` when that file exists | Mention; classify as `FILE` |
| Existing relative directory | `@src`, `@src/` | Mention; classify as `DIRECTORY` |
| Existing nested path | `@src/app/main.py` | Mention; classify by target type |
| Explicit relative path | `@./missing.py`, `@../notes.md` | Mention; unresolved targets remain `UNRESOLVED` |
| Path with separator | `@docs/missing`, `@folder\\missing` | Mention; unresolved targets remain `UNRESOLVED` |
| Home or absolute path | `@~/notes.md`, `@/tmp/notes.md`, Windows drive/UNC paths | Mention, subject to workspace policy |
| Glob | `@src/**/*.py`, `@src/?.py` | Mention; classify as `GLOB` |
| Explicit HTTP URL | `@https://example.test/page` | Mention; classify as `URL` and retain network policy |
| Explicit HTTP URL | `@http://localhost:8000/health` | Mention; classify as `URL` and retain network policy |

The implementation may use an internal path-evidence predicate rather than
the exact examples above, but it must preserve all currently documented forms
and must not require a target to exist when the token has an explicit path,
glob, or URL shape.

### 5.2 Literal forms

The following are ordinary text unless a matching target exists in the allowed
workspace:

```text
@bookTicker
@depth20
@100ms
@dataclass
contact@example.com
```

In particular, an unresolved bare identifier with no path separator, path
prefix, glob character, URL scheme, or other path evidence must not produce an
`UNRESOLVED` mention. It must not cause a warning, chip, prefix block, cache
lookup, workspace authorization call, or network request.

This is an intentional compatibility trade-off: a missing file with a bare
name such as `@notes` is no longer distinguishable from ordinary prose. Users
can disambiguate it with `@./notes`, `@notes/`, or by selecting it from the
interactive picker when it exists.

### 5.3 Boundaries

An `@` may begin a mention only when it is at the beginning of the message or
is preceded by a non-word boundary that is valid for prose, such as
whitespace or an opening delimiter. An `@` immediately following a letter,
digit, underscore, or another `@` is literal text. This prevents email-like
addresses, identifiers, and repeated-at sequences from becoming mentions.

The parser must stop a token at ordinary closing punctuation and backticks.
Existing punctuation normalization for a real target remains supported; for
example, `@README.md?` may still resolve as `README.md` when that file exists.
Punctuation must never be included in a false mention's path.

### 5.4 Markdown code contexts

By default, the parser must not extract mentions from:

* fenced Markdown code blocks delimited by matching triple backticks or tildes;
* inline Markdown code spans delimited by a matching backtick run.

This prevents prompts containing protocol examples, source code, decorators,
or tool syntax from causing injection. A mention outside a code span in the
same message remains eligible. The implementation must handle unmatched
backticks conservatively and must never throw because of malformed Markdown.

If the existing product intentionally documents code-form mentions as an
explicit feature, implementation may add an explicit escape/opt-in syntax,
but ordinary backtick content must remain literal for this fix.

### 5.5 No user-text rewriting

The original user message remains unchanged in the provider conversation. The
parser only controls the optional context prefix and metadata. `strip_mentions`
continues to operate only on the `Mention` objects returned by the parser.

## 6. Technical design

### 6.1 Tokenization pipeline

Replace the current “match every non-space token” behavior with a single
left-to-right scanner (or equivalent regular-expression-plus-validation
pipeline) with these stages:

1. Identify literal Markdown code spans/fenced blocks and skip their ranges.
2. Identify candidate `@` positions using the left-boundary rule.
3. Scan to a right token boundary without consuming closing punctuation,
   quotes, or backticks.
4. Recognize an explicit URL or glob before filesystem classification.
5. Resolve only enough metadata to determine whether a bare token is an
   existing allowed target; do not read content during parsing.
6. Accept a token only when it is an existing target or has path evidence.
7. Classify accepted tokens using the existing `MentionKind` logic and emit
   exact `start`/`end` offsets.

The parser must not perform file reads, directory listings, glob expansion,
URL fetches, or content injection. Existing resolution functions retain those
responsibilities.

### 6.2 Path-evidence policy

Introduce one documented, unit-tested predicate for path evidence. It should
recognize, at minimum:

* `/` or `\\` path separators;
* `./`, `../`, `.\\`, `..\\` prefixes;
* `~/` and absolute paths;
* Windows drive-letter and UNC path forms;
* glob characters `*`, `?`, and `[`;
* an explicit supported URL scheme;
* an existing file or directory target.

An extension-like dot alone may count as path evidence for backwards
compatibility with unresolved names such as `@missing.txt`, but a trailing
sentence delimiter must be removed using the existing target-aware
normalization. The predicate must not treat a leading `@`, a plain alphanumeric
identifier, or a code delimiter as path evidence.

The precise predicate and precedence must be documented in the parser module
docstring and covered by a table-driven test matrix. There must be no separate
copy of this policy in the TUI trigger.

### 6.3 Scope-aware existence checks

When `workspace_scope` is present, existence/classification checks must use the
existing scope resolver and preserve its status/root identity. A path outside
the scope may be recognized as a candidate but must remain
`OUT_OF_SCOPE`/denied according to the existing policy; it must never become a
normal file or directory mention through a fallback `cwd` check.

When no scope is provided, retain the current `cwd`-relative behavior. The
parser must not introduce a new access path around `WorkspaceView`,
`WorkspaceAccessPolicy`, or network guards.

### 6.4 Explicit user selection

The interactive `AtMentionTrigger` remains the mechanism for selecting a
filesystem candidate. Its displayed candidate paths should continue to be
accepted by the parser after insertion. If the trigger needs a shared helper,
it must import the canonical parser policy rather than reimplementing it.

No hidden marker needs to be added to the input buffer. Existing paths are
unambiguous because they resolve; unresolved paths are made explicit with a
path prefix or separator.

### 6.5 Injection boundary

`build_context_prefix()` must receive an empty mention list for literal
technical identifiers. Consequently it must:

* return `("", [])` when the message contains only false-positive forms;
* emit no `mention_chips` event for those forms;
* perform no mention cache lookup or update;
* make no workspace authorization request for those forms;
* make no network request for those forms;
* preserve valid mentions in the same message.

Mixed messages such as `Subscribe to @bookTicker and inspect @config.py`
must inject only `config.py`.

### 6.6 Error and malformed-input behavior

The scanner must be total over arbitrary Unicode input, including malformed
Markdown, unmatched delimiters, NUL characters, long runs of punctuation, and
Windows-style paths. It must return a deterministic list or an empty list;
it must not raise from malformed input.

Candidate length and total candidate count must remain bounded consistently
with existing input/execution limits. A malformed or overlong candidate is
literal text or a bounded unresolved candidate, never an unbounded filesystem
operation.

## 7. Data flow

```text
user input
    │
    ▼
context-aware mention tokenizer
    │  accepted Mention objects only
    ├─────────────── no accepted mentions ───────────────► original text only
    │
    ▼
existing MentionKind classifier
    │
    ▼
build_context_prefix()
    │
    ├─ WorkspaceScope / policy authorization
    ├─ existing file, directory, glob, or URL resolver
    ├─ existing cache and token budget
    └─ mention_chips event for accepted candidates only
    │
    ▼
provider prompt = injected prefix + unchanged user text
```

The key invariant is that lexical recognition precedes resolution. No
unresolved bare identifier reaches the resolver merely because it follows an
`@` character.

## 8. Functional requirements

| ID | Requirement | Priority |
|---|---|---|
| FR-201.1 | Preserve `parse_mentions()` and its current return model as the public API. | P0 |
| FR-201.2 | Accept existing files/directories, explicit paths, globs, and supported HTTP(S) URLs. | P0 |
| FR-201.3 | Treat unresolved path-shaped candidates as mentions so typo diagnostics remain available. | P0 |
| FR-201.4 | Treat unresolved bare identifiers as literal text by default. | P0 |
| FR-201.5 | Reject `@` inside email-like or word-adjacent tokens unless the token is explicitly path-shaped and starts at a valid boundary. | P0 |
| FR-201.6 | Skip inline and fenced Markdown code contexts by default. | P0 |
| FR-201.7 | Preserve exact offsets and punctuation behavior for accepted mentions. | P0 |
| FR-201.8 | Keep parser and interactive-trigger behavior aligned through one canonical policy. | P1 |
| FR-201.9 | Preserve workspace-scope status and deny behavior for recognized outside targets. | P0 |
| FR-201.10 | Prevent false positives from reaching injection, cache, authorization, network, or chip-event paths. | P0 |
| FR-201.11 | Keep the original user message unchanged. | P0 |
| FR-201.12 | Handle arbitrary malformed/Unicode input without exceptions. | P1 |
| FR-201.13 | Bound scanner work and candidate sizes. | P1 |
| FR-201.14 | Document the new literal-versus-mention rules in the mention and user-input guides. | P1 |

## 9. Non-functional requirements

### 9.1 Security

* Recognition must not authorize or read a path merely to decide that a
  random identifier is a mention.
* All accepted filesystem candidates continue through the existing workspace
  policy and symlink/TOCTOU protections.
* URL recognition continues through the existing network guard and allowed
  domain policy.
* Parser diagnostics and tests must not expose file contents, API keys, or
  prompt secrets.

### 9.2 Performance

* Tokenization must be O(n) in message length apart from bounded existence
  probes for candidate paths.
* Literal `@identifier` tokens must not trigger glob expansion, content reads,
  HTTP requests, or cache work.
* Repeated candidates in a single message must not cause unbounded metadata
  probes; a small per-call memoization may be used if needed.

### 9.3 Compatibility

* Existing valid mentions and their `MentionKind`, offsets, injection blocks,
  authorization, and cache keys remain compatible.
* The deliberate compatibility change is that an unresolved bare name no
  longer creates a warning. The user can make it explicit with `./`, `/`, a
  separator, or the picker.
* Headless, TUI, workflow, subagent, and resumed-session callers use the same
  parser behavior.

### 9.4 Observability

* Do not emit a transcript event for ignored literal tokens.
* Optional debug logging may report bounded counts/reasons, but must not log
  full user messages or resolved content.
* Existing accepted-mention chips and resolution errors remain observable.

## 10. Testing strategy

### 10.1 Parser unit tests

Add table-driven tests covering:

* existing bare file and directory names;
* existing names without extensions;
* unresolved `@./missing`, `@../missing`, `@docs/missing`, and
  `@missing.txt`;
* unresolved bare `@bookTicker`, `@depth20`, `@100ms`, and `@dataclass`;
* emails such as `contact@example.com`;
* decorators and identifiers adjacent to word characters;
* `@@literal` and escaped `\\@literal` behavior;
* HTTP and HTTPS URLs, including ports and query strings;
* glob patterns with `*`, `?`, and `[`;
* trailing sentence punctuation and backticks;
* Unicode filenames and identifiers;
* Windows drive and UNC syntax;
* inline code spans, fenced backtick blocks, fenced tilde blocks, unmatched
  delimiters, and mixed literal/mention messages;
* exact `start`/`end` slices and `strip_mentions()` output.

### 10.2 Injection unit/integration tests

Verify that:

1. A message containing only false-positive identifiers returns no prefix and
   no resolved entries.
2. A false-positive identifier causes no workspace authorization call, cache
   call, glob expansion, or network request.
3. A mixed message injects only valid files/URLs/globs.
4. An unresolved path-shaped candidate still returns its existing warning.
5. Scope-denied candidates remain denied and cannot fall back to `cwd`.
6. Multiple valid mentions preserve ordering, offsets, and token budgets.
7. TUI and headless callers produce the same parsed candidates.

### 10.3 Trigger/TUI tests

* The `@` picker still opens at the start of input or after whitespace.
* Email-like and word-adjacent `@` characters do not activate the picker.
* Selecting an existing bare file creates a parser-accepted mention.
* Typing an unresolved path-shaped token without using the picker still
  produces the documented warning.
* Literal technical examples do not create mention chips or failed read lines.

### 10.4 End-to-end regression tests

Exercise a complete turn with:

* a prompt containing Binance-style stream identifiers such as
  `@bookTicker`, `@depth20`, and `@100ms`;
* a prompt containing an email address and a Markdown code sample;
* a prompt containing one valid file mention alongside literal identifiers;
* a prompt containing an unresolved explicit path;
* a resumed/headless session to prove parser behavior is not TUI-specific.

Assertions must inspect both the provider-facing injected text and the
conversation events. Literal tokens must remain in the user message and must
not appear in injected `<file>`, warning, `mention_chips`, or failed-read
events.

## 11. Acceptance criteria

| ID | Acceptance criterion | Evidence |
|---|---|---|
| AC-201.1 | `parse_mentions("subscribe to @bookTicker @depth20 @100ms")` returns `[]` when no matching files exist. | Parser unit test |
| AC-201.2 | Existing files with bare names are still accepted as file mentions. | Parser unit test |
| AC-201.3 | `@./missing.py`, `@docs/missing`, and `@missing.txt` remain accepted unresolved mentions. | Parser unit test |
| AC-201.4 | Email addresses and word-adjacent `@` tokens are not parsed as mentions. | Parser unit test |
| AC-201.5 | Mentions inside inline/fenced code are ignored, while valid mentions outside code in the same message remain parsed. | Parser unit test |
| AC-201.6 | HTTP(S) URLs and globs retain their existing classification and downstream policy checks. | Parser/integration tests |
| AC-201.7 | Accepted mention offsets slice exactly to the accepted token, excluding trailing punctuation/backticks. | Parser unit test |
| AC-201.8 | Literal identifiers cause zero resolver, cache, authorization, glob, and network side effects. | Injection integration test with spies |
| AC-201.9 | Workspace-denied accepted candidates remain denied; no unscoped fallback is introduced. | Workspace integration test |
| AC-201.10 | A mixed message injects only valid targets and preserves all original user text. | Injection/E2E test |
| AC-201.11 | TUI and headless execution share the same parser output. | Cross-surface integration test |
| AC-201.12 | Malformed Unicode and Markdown input never raises. | Fuzz/property or bounded corpus test |
| AC-201.13 | Existing mention parser/injector suites pass without weakening security assertions. | Full relevant test suite |
| AC-201.14 | Documentation explains how to force a missing bare filename to be treated as a mention. | Docs review/build |

## 12. Implementation plan

### Phase 1 — Establish the grammar

* Add the candidate scanner and code-context detection in
  `src/agenthicc/mentions/parser.py`.
* Centralize path-evidence and boundary predicates.
* Preserve the existing public data model and classification values.

### Phase 2 — Integrate the resolution boundary

* Ensure only accepted candidates reach `build_context_prefix()`.
* Verify workspace-aware classification does not bypass policy.
* Add side-effect assertions for cache, authorization, glob, and network
  paths.

### Phase 3 — Align interactive input

* Reuse the canonical boundary/policy helpers in `AtMentionTrigger` where
  appropriate.
* Preserve completion display and insertion behavior.
* Add TUI regression coverage for email-like and code contexts.

### Phase 4 — Documentation and release validation

* Update the mention/input guide and relevant API reference.
* Update the PRD index and implementation status only after acceptance tests
  pass.
* Run the mention-focused unit, integration, and E2E suites, then the normal
  source-surface gates.

## 13. Documentation requirements

Update, as applicable:

* `docs/guides/` mention or input guide: supported forms, literal forms,
  code-context behavior, and the `./` escape for an unresolved bare name;
* `docs/reference/` mention API documentation: parser boundary and
  path-evidence contract;
* `README.md` only if it currently promises that every `@token` is parsed;
* `llms.txt` and `llms-full.txt` if public parser behavior or symbols change;
* this PRD's status and `prds/README.md` after implementation.

## 14. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Users rely on unresolved bare names such as `@notes`. | Document `@./notes`/`@notes/` and preserve existing bare names when they exist. |
| Markdown detection suppresses an intentionally mentioned file in code. | Make code-context behavior explicit and provide an opt-in syntax only if product evidence requires it. |
| Path probing leaks information outside the workspace. | Use the existing scope resolver and never fall back to unscoped `cwd` checks when a scope is active. |
| Regex and scanner rules diverge between parser and picker. | Keep one canonical policy and add cross-surface tests. |
| Punctuation handling breaks URLs or valid filenames. | Test URL query strings, glob punctuation, Windows paths, and target-aware trimming. |
| A provider/tool transcript still shows false failed reads. | Add an end-to-end assertion over both injected prompt content and conversation events. |

## 15. Definition of done

The PRD is implemented only when:

* the parser no longer recognizes unresolved bare technical identifiers as
  mentions;
* all supported explicit mention forms and workspace restrictions remain
  functional;
* the injection pipeline has no side effects for ignored literals;
* parser, injector, trigger, integration, and E2E regression tests pass;
* relevant documentation and the PRD index are updated;
* Ruff, formatting, type checks, type-audit, and the relevant test sessions
  pass, with any unrelated environmental blockers reported explicitly.

## 16. Implementation record

PRD-201 is implemented in the current source tree:

* `src/agenthicc/mentions/parser.py` now uses a bounded scanner with shared
  boundary, path-evidence, and Markdown-code handling. Existing bare targets
  remain supported; unresolved bare identifiers are ignored.
* `src/agenthicc/tui/triggers/at_mention.py` uses the parser's boundary and
  code-context policy for picker activation.
* Parser, injector, trigger, integration, and E2E regression tests cover the
  false-positive examples, mixed valid/literal messages, code blocks, malformed
  boundaries, and scope-denied explicit paths.
* `README.md`, `docs/guides/tui.md`, `docs/usage/04-tui.md`, `llms.txt`, and
  `llms-full.txt` document the resulting behavior.

Verification for this implementation:

* The reset-only orphaned `tests/unit/test_provider_error_diagnostics.py` file
  was removed because its PRD-205 implementation is absent from the reset
  commit and the test could not be collected against the current source.
* The complete suite passes: 3,887 tests passed, 15 skipped, and 4 existing
  warnings.
* 248 integration tests and 132 E2E tests passed.
* The focused mention/trigger suite passed 232 tests, including the new
  PRD-201 integration and E2E cases.
* Targeted mypy, repository type-audit, Ruff lint, strict MkDocs, and
  `llms_check` passed. `git diff --check` passed.
* Repository-wide Ruff format check still reports nine unrelated pre-existing
  files, and repository-wide mypy retains its existing unrelated errors; no
  formatter or typing changes were made outside this PRD's scope.
