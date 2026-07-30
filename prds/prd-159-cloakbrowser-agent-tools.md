---
title: "PRD-159: Specialized CloakBrowser Agent Tools"
status: Proposed
version: 1.0.0
created: 2026-07-30
study_date: 2026-07-30
scope: "Optional, policy-gated browser automation tools for agenthicc"
related_prds:
  - PRD-100  # code_plan workflow architecture
  - PRD-108  # shared outbound HTTP policy
  - PRD-116  # WorkflowPlugin as the registry artifact
  - PRD-138  # repository improvement roadmap
  - PRD-149  # owned background terminals and lifecycle control
  - PRD-151  # command lifecycle and readiness contracts
  - PRD-154  # create_workflow architecture
  - PRD-157  # session-scoped usage accounting
  - PRD-158  # resumed TUI transcript
tags:
  - browser
  - cloakbrowser
  - playwright
  - tools
  - workflows
  - security
---

# PRD-159 — Specialized CloakBrowser Agent Tools

## 1. Executive summary

agenthicc already documents CloakBrowser in examples for `create_workflow`, but
the current source tree has no first-party CloakBrowser adapter or built-in
browser tools. A generated workflow can therefore mention CloakBrowser without
having a stable, discoverable, policy-enforced runtime surface to call.

This PRD adds an opt-in, first-party integration that exposes a small semantic
browser toolset to agent turns and workflows. The model will be able to open an
approved URL, inspect bounded page content, perform explicitly gated UI
actions, wait, capture an artifact, and close its browser session. It will not
receive raw Playwright or CDP access, arbitrary JavaScript execution, browser
launch flags, proxy credentials, cookie APIs, or CAPTCHA-solving facilities.

The primary runtime is the Python package's asynchronous Playwright-compatible
API. A separately configured loopback CDP endpoint is supported for an
operator-managed `cloakserve` process, but it is not the default. Browser
resources are session-scoped and are deliberately distinct from the provider
conversation: only bounded tool receipts enter model context, while browser
handles and live Playwright objects remain in the session runtime.

The integration is for authorized browsing, QA, accessibility checks, research,
and automation against targets the operator is permitted to access. It must not
be designed or documented as a way to bypass authorization, rate limits,
CAPTCHAs, bot controls, or a site's terms.

## 2. Evidence from the upstream project

The following facts were verified against the official CloakHQ repository on
2026-07-30. Upstream APIs and licensing can change; implementation must pin and
contract-test the selected dependency rather than relying on an unbounded
`latest` install.

* The repository describes CloakBrowser as a Python and JavaScript wrapper
  around a custom Chromium binary with a Playwright-compatible API. Its Python
  entry points include `launch_async()`, `launch_context_async()`, and
  `launch_persistent_context_async()`.
* The wrapper downloads and caches a platform-specific browser binary on first
  use, and exposes an `ensure_binary()` helper. Importing agenthicc must never
  trigger that download; setup and launch are explicit runtime operations.
* Persistent contexts retain cookies, local storage, cache, and other browser
  profile state. That makes them useful but materially increases the secret and
  cross-run data boundary, so persistence is disabled by default here.
* The Docker documentation describes `cloakserve`, a CDP server mode, and
  explicitly warns that CDP grants full browser control, including executing
  JavaScript, reading pages, and accessing files. The examples bind it to
  `127.0.0.1`; agenthicc must preserve that loopback-first posture.
* The wrapper source is MIT licensed, while the compiled browser binary has a
  separate binary license. agenthicc must not vendor the binary or imply that
  the wrapper license covers it. Installation, version, and subscription
  requirements must be visible in the integration documentation.

Primary sources:

* [CloakBrowser repository and README](https://github.com/CloakHQ/CloakBrowser)
* [CloakBrowser Python exports](https://github.com/CloakHQ/CloakBrowser/blob/main/cloakbrowser/__init__.py)
* [CloakBrowser async launch implementation](https://github.com/CloakHQ/CloakBrowser/blob/main/cloakbrowser/browser.py)
* [CloakBrowser wrapper license](https://github.com/CloakHQ/CloakBrowser/blob/main/LICENSE)
* [CloakBrowser binary license](https://github.com/CloakHQ/CloakBrowser/blob/main/BINARY-LICENSE.md)
* [CloakBrowser changelog and security history](https://github.com/CloakHQ/CloakBrowser/blob/main/CHANGELOG.md)

The README contains marketing and live detection claims. Those claims are not
agenthicc acceptance criteria: the project will test tool correctness against
local fixtures and contract fakes, not against third-party bot-detection sites.

## 3. Current-state fit and problem statement

The implementation must stay inside the current ownership boundaries:

| Concern | Existing contract to reuse |
| --- | --- |
| Agent-facing tools | `@tool()` callables or explicit `Tool`/`ToolBase` registrations |
| Capability policy | `ToolCapability`, mode filters, `ToolCapabilityGate`, approval hooks |
| Network policy | `NetworkGuard` plus shared `agenthicc_http_client()` for HTTP probes |
| Filesystem artifacts | `WorkspaceView`, not model-selected absolute paths |
| Turn construction | `AgentTurnContext` and `AgentTurnRunner` |
| Workflow injection | `WorkflowConfig`, `PhaseSpec`, and the standard turn boundary |
| Workflow authoring | `create_workflow` and its inspection tools |
| Persistence and resume | session memory, workflow checkpoint state, and session journal |

The current source search finds CloakBrowser only in authoring examples and
tests of those examples. There is no canonical client, config section, browser
session manager, tool set, or lifecycle owner. This creates four concrete
failures:

1. `create_workflow` can generate a workflow that refers to a service the
   normal tool registry cannot provide.
2. A project plugin could import Playwright/CloakBrowser directly and bypass
   the session capability, approval, network, artifact, and cleanup contracts.
3. Browser page output can be unbounded or contain prompt injection, secrets,
   or binary data that should not be copied into provider context.
4. A live browser context is not serializable. A workflow that checkpoints only
   its phase enum but assumes its old page still exists will fail after pause,
   restart, or resume.

## 4. Goals

The implementation shall:

1. Provide a typed asynchronous adapter that hides the upstream Playwright/CDP
   details from agent-facing tools.
2. Offer a small, stable tool surface for navigation, bounded inspection,
   explicitly approved interaction, waiting, screenshots, status, and cleanup.
3. Make the integration disabled by default and fail closed when it is not
   configured, unavailable, unhealthy, unauthorized, or outside its policy.
4. Enforce host, scheme, redirect, private-network, timeout, size, page-count,
   and action-count limits before a browser operation is dispatched.
5. Reuse the existing capability, approval, workspace, workflow, memory,
   usage-accounting, and cancellation contracts.
6. Keep browser sessions isolated by agenthicc session/conversation and never
   expose cookies, storage state, proxy credentials, license keys, or CDP URLs
   to the model.
7. Make browser-aware custom workflows work through the same standard
   `AgentTurnRunner` boundary as built-in workflows, including checkpoint and
   resume behavior.
8. Provide deterministic unit, integration, and opt-in browser E2E coverage
   without making CI depend on an external website or a paid CloakBrowser
   binary.

## 5. Non-goals and explicit safety boundaries

The initial implementation shall not:

* expose raw `page.evaluate`, arbitrary JavaScript, raw CDP commands, browser
  process command lines, or arbitrary Playwright objects;
* let the model choose `humanize`, fingerprint seeds, stealth flags, GeoIP,
  platform spoofing, proxy servers, extension paths, browser binaries, or
  release channels;
* solve CAPTCHAs, rotate identities to evade controls, defeat rate limits,
  harvest credentials, or automate access to targets without authorization;
* import/export cookies, headers, storage state, or persistent profiles through
  tool arguments;
* upload local files, download arbitrary files, open `file:`, `data:`,
  `javascript:`, `chrome:`, or equivalent privileged URLs;
* make third-party bot-detection scores, stealth success, or anti-bot bypass a
  product acceptance criterion;
* replace the general HTTP search/fetch tools for ordinary static pages; use
  browser tools when page execution or user-interface interaction is required.

Persistent profiles, operator-selected proxies, headed mode, and remote CDP
may be supported as deployment configuration in a later phase. They are never
model-controlled inputs.

## 6. Proposed architecture

Add a focused package below `src/agenthicc/tools/cloakbrowser/`:

```text
config.py       validated operator configuration and defaults
policy.py       URL, redirect, resource, action, and redaction policy
client.py       typed async protocol plus local and CDP implementations
session.py      session-scoped browser/context/page lifecycle manager
artifacts.py    bounded screenshot/artifact storage through WorkspaceView
errors.py       stable recoverable error categories
agent_tools.py  @tool() factories and the exported built-in tool list
```

The names are implementation guidance, not a requirement to create one class
per file. Keep the upstream dependency behind `client.py`; no other agenthicc
module may import `cloakbrowser` or Playwright directly.

### 6.1 Client and lifecycle boundary

Define a typed `CloakBrowserClient` protocol with operations equivalent to:

```text
health() -> BrowserHealth
open_page(session_id, url) -> PageState
snapshot(session_id, page_id, request) -> PageSnapshot
click(session_id, page_id, target) -> PageState
fill(session_id, page_id, target, value) -> PageState
press(session_id, page_id, key) -> PageState
wait_for(session_id, page_id, condition) -> PageState
screenshot(session_id, page_id, options) -> BrowserArtifact
close_page(session_id, page_id) -> None
close_session(session_id) -> None
```

The protocol returns typed, JSON-safe values and never returns a live browser
object. The local implementation uses the upstream asynchronous launch/context
API. The CDP implementation uses Playwright's `connect_over_cdp` only after a
validated, operator-configured loopback endpoint has passed a health check.
The agent-facing `cloakbrowser_status` tool reads cached manager health and
does not itself probe an arbitrary endpoint; endpoint health checks happen at
operator/session setup.

Each agenthicc session gets one `BrowserSessionManager`. It owns at most the
configured number of contexts/pages, serializes operations that target the same
page, and closes all contexts in a `finally` path on normal completion,
cancellation, provider failure, and TUI shutdown. Cancellation must clean up
even when the underlying awaitable raises `BaseException`. In local mode it
owns the browser process; in CDP mode it closes its pages and connection but
does not stop an operator-owned `cloakserve` process.

The manager maps the stable agenthicc `conversation_id` to an opaque browser
session handle. The conversation ID is never sent to CloakBrowser as a URL,
profile name, or page content. A workflow checkpoint stores only the opaque
session metadata needed to rehydrate, such as the selected profile policy and a
redacted last approved origin/path. It does not serialize a Playwright object,
cookies, storage state, page DOM, query strings, or credentials. On resume, the
manager recreates the browser context and returns an explicit
`browser_session_rehydrated` receipt; a stale page handle is recoverable and
must be reopened rather than silently reused.

### 6.2 Optional dependency and packaging

CloakBrowser must be an optional project extra, not a base runtime dependency.
The implementation shall add a dedicated extra to `pyproject.toml` with the
final approved upstream version pinned before release:

```toml
[project.optional-dependencies]
cloakbrowser = ["cloakbrowser==<approved-version>"]
```

The resulting installation contract is:

```bash
# Normal agenthicc installation; no CloakBrowser package or binary required.
pip install agenthicc

# Opt in only when browser tools are needed.
pip install 'agenthicc[cloakbrowser]'
# Equivalent development setup:
uv sync --extra cloakbrowser
```

The base dependency list, existing `cloud`/`dev` extras, startup import path,
and non-browser tool catalog must remain usable without this extra. The
CloakBrowser adapter must import the optional package lazily inside its local
transport and expose `dependency_missing` when the extra is absent. No
automatic package installation is permitted, including through plugin
discovery or a browser health check. If the pinned upstream release declares
additional Playwright/runtime requirements, they must be owned by this extra
or explicitly added to the same extra rather than added to the base package.

Unit and integration tests that use the fake client must run without the extra.
The real-browser contract and E2E tests must be marked as requiring the
`cloakbrowser` extra and skip with a clear diagnostic when it is unavailable.

### 6.3 Configuration

Add a nested `ToolSettings.cloakbrowser` dataclass. The exact TOML spelling may
follow current configuration conventions, but the effective shape is:

```toml
[tools.cloakbrowser]
enabled = false
transport = "local"                 # local | cdp
cdp_endpoint = "http://127.0.0.1:9222"
allowed_domains = []                 # empty means deny all navigation
headless = true
navigation_timeout_s = 15.0
action_timeout_s = 10.0
max_pages = 4
max_actions_per_turn = 20
max_snapshot_chars = 20000
max_screenshot_bytes = 10000000
allow_persistent_profiles = false
profile_root = ".agenthicc/browser-profiles"
```

Additional settings may select an operator-owned environment variable for the
CloakBrowser license key and a pinned browser/package version. Secret values
must not be written to TOML, tool output, checkpoints, event payloads, or
logs. `enabled = false`, missing dependency, missing endpoint, or invalid
configuration must leave the normal tool catalog unchanged except for a
bounded diagnostic visible through the existing tool/session diagnostics.

The implementation must not auto-install Python packages or download a browser
binary during import or session construction. Provide an explicit setup/check
path, document the optional dependency and system requirements, and make
`health` report `not_configured`, `dependency_missing`, `binary_missing`,
`unhealthy`, or `ready` without leaking paths or license details.

### 6.4 URL and resource policy

`BrowserPolicy` must validate before every navigation, not only on the first
request:

* allow only `http` and `https` URLs;
* reject credentials in URLs, fragments where policy disallows them, malformed
  hosts, non-default ports unless explicitly allowed, and control characters;
* require an exact allowed host or allowed subdomain according to the existing
  `NetworkGuard` semantics;
* resolve and reject loopback, link-local, private, reserved, multicast, and
  cloud-metadata addresses by default, including DNS-rebinding results;
* revalidate every redirect and popup target before following or opening it;
* limit response/page size, navigation time, action time, page count,
  concurrent operations, and screenshot bytes;
* default to no downloads, uploads, permission grants, popups, or cross-origin
  frame actions unless an explicit future policy enables them.

The CDP control endpoint is a separate control-plane destination. It must be
loopback-only by default, must not be accepted from the model, and must not be
treated as an allowed browsing host. A non-loopback endpoint requires an
explicit operator setting, TLS/authentication design, and a separate security
review; it is out of the initial release.

### 6.5 Artifact and output policy

All tool results use the existing `ToolResultEnvelope` semantics or an
equivalent lauren-ai JSON result. A successful result has bounded fields such
as:

```json
{
  "ok": true,
  "page_id": "opaque-page-id",
  "url": "https://approved.example/section",
  "title": "Example",
  "content": "bounded, clearly untrusted page text",
  "truncated": false,
  "artifacts": []
}
```

Failures include stable `error_kind` values (`not_configured`,
`policy_denied`, `approval_required`, `timeout`, `browser_unavailable`,
`stale_page`, `output_limit`, `cancelled`, and `execution`) plus a short
safe message. Raw exception text, cookies, authorization headers, proxy URLs,
license values, and local profile paths are never returned.

Snapshots must be normalized to bounded text/links/accessible controls rather
than returning an unbounded DOM. Page content is labelled as untrusted
external content in the tool result and system guidance so instructions
embedded in a page cannot be mistaken for agenthicc policy. Screenshots are
written through `WorkspaceView` below a session artifact directory and
returned as an opaque artifact reference with MIME type and byte count. The
model cannot select an arbitrary filesystem path.

## 7. Agent-facing tool contract

Expose the following names through a built-in `CLOAKBROWSER_AGENT_TOOLS` list.
Arguments must be typed, bounded, and intentionally narrower than Playwright.
All tools require the configured browser integration; they do not silently
fall back to shell commands or direct HTTP.

| Tool | Purpose | Capabilities / approval |
| --- | --- | --- |
| `cloakbrowser_status` | Report readiness and bounded session counts | `READ`; no approval |
| `cloakbrowser_open` | Open an approved URL in a new/reused page | `NETWORK + READ`; Safe approval for navigation |
| `cloakbrowser_snapshot` | Return bounded text, links, and accessible controls | `NETWORK + READ`; no additional approval |
| `cloakbrowser_click` | Click a bounded selector/role target | `NETWORK + WRITE`; Safe approval |
| `cloakbrowser_fill` | Fill a visible form field | `NETWORK + WRITE`; Safe approval, redacted value |
| `cloakbrowser_press` | Press an allow-listed keyboard key | `NETWORK + WRITE`; Safe approval |
| `cloakbrowser_wait_for` | Wait for a bounded selector, text, URL, or load state | `NETWORK + READ`; no approval |
| `cloakbrowser_screenshot` | Save a bounded PNG/JPEG artifact | `NETWORK + READ`; no approval |
| `cloakbrowser_close` | Close a page or the current browser session | `WRITE`; Safe approval only when it discards persistent state |

`page_id` and `session_id` are opaque handles generated by the manager. The
agent cannot supply a different conversation ID, profile directory, CDP URL,
proxy, header, cookie, or storage-state path. `fill` results and audit records
contain field metadata but never the entered value. Submit/login/payment-like
actions must either be denied by policy or require a distinct explicit
approval policy; the first release should default to deny for known sensitive
field types and navigation methods that submit forms.

Every tool call must be idempotent where practical or carry an operation ID so
transport retries cannot duplicate a click/fill/submit. The browser manager
must not retry a mutating action automatically after an uncertain timeout.

## 8. Modes, approvals, and workflow integration

The tools reuse the current capability taxonomy rather than adding a parallel
browser permission system:

* status and non-mutating inspection use `READ` (and `NETWORK` when they
  cause a remote page operation);
* navigation, clicks, fills, key presses, and session-discard operations use
  `WRITE` plus `NETWORK` where applicable;
* Plan mode therefore hard-blocks browser writes and browser network access as
  it does other side effects; Safe presents the existing approval flow; Yolo
  remains the explicit operator escape hatch;
* phase-local `allowed_capabilities` and `allowed_tool_names` still narrow
  the set, and an unconfigured browser never expands it.

`WorkflowConfig` constructs one browser manager alongside the existing session
singletons and passes the same tool factory to direct turns and workflow turns.
`AgentTurnRunner` remains the only provider/tool boundary. A generated
workflow must not instantiate CloakBrowser or Playwright itself.

`create_workflow` requires the following enhancements:

1. Its design inspection surface documents the browser tool names, capability
   requirements, configuration prerequisite, and the rule that browser access
   is optional and authorization-bound.
2. Its generated workflow template can declare a browser-capable phase using
   the ordinary `PhaseSpec` capability filter, while keeping design and
   validation phases browser-free.
3. Validation checks that a generated workflow references known tool names and
   does not import CloakBrowser/Playwright directly. It reports a repairable
   error with the canonical factory pattern.
4. The generate phase may write the workflow source but must not execute its
   browser actions. Workflow E2E tests use a fake client and a local fixture.
5. The standard workflow runner/checkpoint path persists phase state and
   browser rehydration metadata, not live browser objects. After resume, the
   first browser call must re-check health and approved URL policy.

This gives downstream custom workflows browser tools “out of the box” only
when they use the standard runner and declare the required capability. It does
not grant a generated workflow access to an unconfigured browser or bypass the
active mode/approval policy.

## 9. Data flow

```text
user intent / workflow phase
        │
        ▼
AgentTurnRunner
  (one session conversation_id, memory, usage ledger)
        │
        ▼
tool registry + phase allowlist + mode capability gate
        │
        ├── denied / approval required ──► approval service + redacted event
        │
        ▼
cloakbrowser agent tool
  validate args → BrowserPolicy → BrowserSessionManager
        │                         │
        │                         ├── local: launch_context_async()
        │                         └── CDP: loopback connect_over_cdp()
        ▼
approved browser context/page
  navigate/action → revalidate redirects/popups → bounded observation
        │
        ├── text/state receipt ──► redaction + truncation + tool result
        └── screenshot ──────────► WorkspaceView artifact + opaque reference
                                      │
                                      ▼
                         provider tool result / TUI event / journal
                         (no cookies, credentials, live handles, or raw DOM)
```

The provider conversation retains the bounded tool result in the normal
assistant/tool history. The workflow checkpoint and session journal retain
phase state, operation receipts, and safe artifact references. The browser
manager retains live contexts in memory and reconstructs them after resume.
These stores must not be collapsed into one browser-state persistence format.

## 10. Observability and failure behavior

Emit the existing tool/workflow events for start, approval, denial, success,
timeout, cancellation, and failure. Add only redacted fields: tool name,
stable error kind, duration, byte count, page count, and a trace/operation ID.
Hostnames may be omitted or hashed when the configured privacy mode requires
it. Never log full URLs containing query secrets, page content, entered values,
cookies, headers, proxy credentials, or CDP endpoints with tokens.

Failures must be explicit and recoverable where retry is safe:

* missing dependency/binary or unhealthy endpoint → `browser_unavailable`;
* denied host/scheme/private address/redirect → `policy_denied`;
* Safe-mode gate → `approval_required` and no browser action;
* timeout → `timeout`, with no automatic retry for uncertain mutation;
* cancellation → close the affected page/context and return `cancelled`;
* stale handle after resume → `stale_page` with a reopen suggestion;
* output cap → return the bounded prefix and `truncated=true`, never silently
  grow the conversation.

## 11. Acceptance criteria

### Configuration and lifecycle

1. A base `pip install agenthicc` does not install or import CloakBrowser, while
   `pip install 'agenthicc[cloakbrowser]'` installs the dedicated optional
   extra; existing base installations remain functional.
2. The default configuration exposes no CloakBrowser tools and makes no binary,
   network, license, or browser-process request.
3. An enabled local installation reports `ready` only after the dependency and
   binary health checks pass; imports remain side-effect free.
4. When the optional CDP phase is enabled, a configured loopback endpoint is
   health-checked, connected, and disconnected through the manager; a
   non-loopback endpoint is rejected by default.
5. Normal completion, provider failure, cancellation, and TUI shutdown close
   all owned pages, contexts, Playwright handles, and child processes.
6. Two concurrent agenthicc conversations cannot address one another's page
   handles, profiles, cookies, or artifacts.

### Policy and tools

7. Each tool has provider-visible schema, capability metadata, bounded inputs,
   and deterministic structured results.
8. Disallowed schemes, hosts, private/IP-literal destinations, DNS-rebinding
   results, redirects, popups, and forbidden ports are denied before navigation.
9. `open`, `snapshot`, `click`, `fill`, `press`, `wait_for`,
   `screenshot`, and `close` work against a fake client with the exact
   result/error contract.
10. Safe mode requests approval before navigation and browser writes; Plan mode
   blocks them; Yolo is the only mode that skips those gates.
11. `fill` never emits its value in a tool result, TUI event, journal, audit
    entry, exception, or log. Cookies, storage state, license keys, and proxy
    credentials are similarly absent.
12. Snapshot and screenshot limits are enforced, page output is labelled as
    untrusted, and screenshot paths remain inside `WorkspaceView`.
13. A timed-out or cancelled mutation is not automatically repeated, and an
    operation ID prevents safe retries from duplicating an action.

### Workflows and resume

14. A custom workflow using the standard runner receives the configured
    browser tool set in a declared browser-capable phase without importing
    CloakBrowser directly.
15. `create_workflow` documents the tools and emits a valid browser-capable
    `PhaseSpec`; its design/validation phases cannot use browser tools unless
    explicitly and intentionally changed in source.
16. `create_workflow` validation rejects direct Playwright/CloakBrowser imports
    and unknown browser tool names with actionable repair guidance.
17. A checkpoint contains only serializable phase and browser metadata. Resume
    recreates the browser context or returns a clear unavailable/stale result;
    it never deserializes or silently reuses a dead browser object.
18. Browser tool calls, retries, approvals, subagents, and compaction remain
    included in the one session `UsageLedger` and preserve the session
    `conversation_id`.

### Operational and licensing

19. CI passes without a live internet destination, paid license, downloaded
    CloakBrowser binary, or third-party detection site.
20. An opt-in browser E2E job uses a local HTTP fixture and either a pinned
    installed binary or an explicitly marked environment skip; it exercises
    navigation, bounded extraction, an approved interaction, screenshot
    artifact creation, cleanup, and resume.
21. Documentation explains optional installation, supported platforms, binary
    licensing, version pinning, authorized-use expectations, CDP loopback
    requirements, and the difference between static HTTP tools and browser
    tools.

## 12. Test strategy

### Unit tests

Add focused tests for:

* configuration parsing, defaults, invalid values, secret-source selection,
  disabled behavior, and no import-time side effects;
* URL normalization, scheme/host/port checks, IP/DNS/redirect/popup policy,
  size/time/page/action limits, and redaction;
* client protocol adapters using a fake Playwright/CloakBrowser object,
  including async cancellation and cleanup on `BaseException`;
* session isolation, opaque handle validation, stale handles, idempotency, and
  checkpoint serialization/rehydration metadata;
* exact tool schemas, capability metadata, approval requirements, result
  envelopes, truncation, artifact boundaries, and error kinds.

### Integration tests

Use the real `AgenthiccToolExecutor`, capability gate, approval service,
`WorkflowConfig`, `AgentTurnRunner` registry, `WorkspaceView`, and an
in-memory or temporary fake browser client. Cover:

* tool discovery when enabled and absence when disabled;
* Safe/Plan/Yolo behavior and phase-local capability narrowing;
* a real `EventProcessor` and session journal receiving redacted tool events;
* session/workflow construction sharing one browser manager, conversation ID,
  usage ledger, and cleanup owner;
* `create_workflow` inspection, generated browser phase metadata, validation,
  and rejection of direct upstream imports;
* checkpoint save, process-local manager teardown, resume, and rehydration.

### E2E tests

Use a local fixture server with deterministic pages containing ordinary text,
links, a form, a redirect, a popup attempt, and a prompt-injection-shaped
string. Drive a `MockTransport` through the real agent turn and workflow
runner. Verify the model sees bounded page data as untrusted content, can use
the allowed tools, cannot escape the host policy, and receives an artifact
reference rather than arbitrary filesystem access. The fixture policy may
explicitly allow loopback only within this isolated test; production policy
continues to deny private and loopback destinations.

An opt-in environment test may run the pinned CloakBrowser package against the
same local fixture. It must be marked separately from the default CI matrix;
it must not test CAPTCHA or third-party anti-bot claims. A CDP-container test
may be added only when the container is explicitly present and bound to
loopback.

## 13. Rollout and implementation phases

### Phase A — safe local adapter

Implement config, policy, typed client, lifecycle manager, health/status,
`open`, `snapshot`, `wait_for`, `screenshot`, and `close`. Add
fake-client unit and integration coverage, documentation, dependency/version
pinning, and the disabled-by-default behavior.

### Phase B — approved interaction

Add `click`, `fill`, and `press`, approval/redaction/idempotency behavior,
operation limits, cancellation cleanup, and local-fixture E2E coverage.

### Phase C — workflow and resume integration

Inject the manager through `WorkflowConfig`, update `create_workflow`
inspection and validation, add checkpoint rehydration metadata, generated-
workflow examples, and the workflow integration/E2E matrix.

### Phase D — separately reviewed deployment options

Add loopback `cloakserve`/CDP support and operator-managed persistent profiles
only after the threat model, authentication, profile retention, and cleanup
tests pass. Remote CDP, arbitrary proxies, uploads, downloads, extensions,
and model-controlled stealth options remain out of scope until a new security
review and PRD.

## 14. Documentation and compatibility work

The implementation must update, in the same change:

* `docs/guides/tools.md` with installation, tool schemas, capability/approval
  behavior, bounded output, and the static HTTP versus browser distinction;
* `docs/guides/workflows.md` with a canonical browser-capable workflow and
  checkpoint/resume behavior;
* `docs/guides/security.md` with browser URL, CDP, profile, and secret rules;
* `docs/guides/configuration.md` with the `[tools.cloakbrowser]` settings;
* `README.md`, `llms.txt`, and `llms-full.txt` for user-visible/public
  symbols;
* this PRD's status and implementation evidence after completion.

The feature is additive and disabled by default. Existing tool names, workflow
phase contracts, mode names, conversation IDs, and session accounting remain
compatible. The only intentional new behavior is that an explicitly enabled
browser tool is subject to the existing capability and approval gates.

## 15. Assumptions and open decisions

The implementation may proceed with these assumptions unless maintainers
choose otherwise:

1. Python local mode is the supported first transport; Node.js and external
   remote browsers are not needed for the first release.
2. The selected CloakBrowser wrapper version is pinned in the project lockfile
   and exercised by an upstream API contract test. The binary is obtained by
   the operator from official channels and is never committed to agenthicc.
3. `allowed_domains` is an explicit operator setting and an empty list denies
   navigation. The browser cannot be used as a general internet proxy.
4. Browser content is untrusted input and is not automatically written to
   durable project memory; the user or workflow must explicitly save a bounded
   artifact.
5. Persistent browser profiles are opt-in, project-scoped, excluded from git,
   and governed by a separate retention policy. The initial default is
   ephemeral context per agenthicc session.

Before implementation, confirm:

* the supported CloakBrowser package/binary version and platform matrix for CI;
* whether a headed browser is acceptable in any supported TUI deployment;
* whether operator policy needs a first-class approval category for navigation
  and form interaction instead of reusing `NETWORK`/`WRITE`;
* whether Phase D's CDP mode is required for deployment or can remain optional.

## 16. Definition of done

This PRD is complete when all acceptance criteria are implemented, the default
test suite remains deterministic and passes, the opt-in fixture E2E is green or
explicitly skipped with a diagnosed environment reason, the security and
workflow documentation is updated, and the implementation evidence is linked
from this document and `prds/README.md`.
