---
title: "PRD-160: Playwright Browser Agent Tools"
status: implemented
---

# PRD-160 — Playwright Browser Agent Tools

## Objective

Provide Microsoft Playwright as an optional alternative to CloakBrowser for
browser-capable agent turns and custom workflows. The alternative must preserve
the existing browser ownership boundary: the session owns the live context,
the agent receives only bounded capability-tagged tools, and generated
workflows cannot import a browser package directly.

## Configuration

`[tools].browser_backend` selects exactly one backend: `cloakbrowser`,
`playwright`, or `none`. The default remains `cloakbrowser` for backwards
compatibility. The Playwright settings live under `[tools.playwright]` and
support `browser_type` (`chromium`, `firefox`, or `webkit`), headless mode,
timeouts, page/action/output limits, persistent profiles, executable/channel
selection, `allowed_domains`, and `allow_all_domains`.

The empty allow-list denies navigation. `allow_all_domains = true` bypasses
hostname matching only; HTTP(S), configured ports, DNS rebinding, loopback,
private-address, selector, sensitive-field, output, and workspace-artifact
guards remain active.

## Tool contract

The selected backend exposes the same nine operations:

`status`, `open`, `snapshot`, `click`, `fill`, `press`, `wait_for`,
`screenshot`, and `close`.

CloakBrowser uses `cloakbrowser_*`; Playwright uses `playwright_*`. All tools
share the existing `BrowserSessionManager`, `BrowserPolicy`, artifact store,
operation-id replay, action budgets, checkpoint metadata, and workflow phase
capability gating.

## Dependency and lifecycle requirements

The base package must not import Playwright. The optional `playwright` extra
installs the Python package, while browser binaries remain an explicit operator
installation step. Missing package/runtime returns a structured browser health
error and does not prevent agenthicc startup. Live browser objects are closed
at session shutdown and never serialized into workflow checkpoints.

## Acceptance criteria

1. Selecting `playwright` exposes exactly the nine `playwright_*` tools and no
   duplicate CloakBrowser tools.
2. Playwright supports configured Chromium, Firefox, and WebKit launches and
   persistent profiles under the workspace.
3. Navigation and subresource requests are blocked outside the configured
   policy, including redirect/DNS/private-address checks.
4. Snapshots, screenshots, selectors, form values, keyboard keys, page counts,
   action counts, and artifact paths remain bounded by the shared adapter.
5. Missing optional dependencies and browser binaries are safe structured
   results; base imports and the existing CloakBrowser backend remain usable.
6. Unit, integration, and E2E-style deterministic tests cover selection,
   policy, tool capabilities, lifecycle, missing dependency, workflow
   validation, and the open/observe/interact/capture/close journey.

