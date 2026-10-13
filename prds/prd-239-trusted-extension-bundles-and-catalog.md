---
title: "PRD-239: Trusted Extension Bundles and Catalog"
status: Proposed
version: 0.1.0
date: 2026-10-13
repository: jymchng/agenthicc
priority: P2
related_prds: [PRD-138, PRD-139, PRD-145, PRD-147, PRD-153, PRD-174]
tags: [plugins, extensions, catalog, trust]
---

# PRD-239 — Trusted Extension Bundles and Catalog

## 1. Summary

Define a versioned, installable extension bundle that can package Agenthicc
skills, agent profiles, commands, workflows, MCP declarations, and optional
trusted hook implementations with one manifest, dependency graph, provenance,
and lifecycle. Start with local paths and Git repositories; a public
marketplace is a later decision.

## 2. Problem and research basis

Agenthicc has separate extension systems for plugins, skills, commands,
workflows, and MCP servers. Users need discovery and coordinated installation,
but a bundle must not silently inherit trust across executable components.
Claude Code and Codex plugins bundle reusable skills and MCP integrations;
both expose plugin/marketplace concepts with explicit installation and trust
boundaries.

Sources: [Claude Code plugins](https://code.claude.com/docs/en/plugins),
[Codex plugins](https://learn.chatgpt.com/docs/plugins),
[Agenthicc plugin trust roadmap](prd-138-repository-improvement-roadmap.md).

## 3. Goals and non-goals

Goals: manifest/schema, local catalog, inspect-before-install, component
permissions, dependency/version validation, disable/uninstall, and provenance.

Non-goals: public arbitrary-code marketplace, automatic update execution,
allowing a plugin to change global security policy, or treating signed as
equivalent to safe.

## 4. Requirements

1. Define a manifest with bundle ID/version, compatibility range, publisher,
   content hashes, component inventory, dependencies, requested capabilities,
   licenses, and optional signatures. Unknown manifest versions fail closed.
2. Provide `agenthicc plugins catalog/list/inspect/install/enable/disable/
   update/remove` for local directories and pinned Git revisions. Network
   catalogs require HTTPS and explicit configuration; no implicit code fetch.
3. Show the full component/capability inventory and executable files before
   installation. Skills/Markdown may be data-only; Python plugins, commands,
   and hooks require separate trust grants and are audited on content change.
4. Resolve dependencies deterministically and reject conflicts, path
   traversal, symlinks escaping the bundle, duplicate IDs, hash mismatch, and
   incompatible Agenthicc versions.
5. Install atomically into a managed extension root; maintain a lock file with
   source revision and content hashes. Rollback on partial failure and retain
   user data/config outside the bundle.
6. Disable/uninstall prevents future discovery but does not delete session
   history or silently remove files the extension created outside its root.

## 5. Acceptance criteria

- A local bundle can package a skill, agent profile, and MCP config, be
  inspected, installed, and loaded through existing registries.
- Executable components remain inactive until separately trusted; updating a
  trusted component's hash prompts for renewed review.
- A failed install leaves the prior active extension set intact and reports
  which validation step failed.
- Disabling a bundle removes its tools/commands from active discovery without
  corrupting sessions that already reference it; those sessions receive a
  clear unavailable-extension diagnostic.
- Secret values are never embedded in a bundle, lockfile, catalog response,
  or audit log.

## 6. Testing and dependencies

Test manifest schema, signatures/hashes, trust changes, dependency conflicts,
atomic install/rollback, path attacks, registry reload, and session references.
Reuse `plugins/trust.py` and existing registries; publish only after PRD-138's
extension SDK and trust contracts are reviewed.
