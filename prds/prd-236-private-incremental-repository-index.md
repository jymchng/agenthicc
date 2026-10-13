---
title: "PRD-236: Private Incremental Repository Index"
status: Proposed
version: 0.1.0
date: 2026-10-13
repository: jymchng/agenthicc
priority: P2
related_prds: [PRD-131, PRD-138, PRD-139, PRD-167, PRD-235]
tags: [repository-index, search, context, privacy]
---

# PRD-236 — Private Incremental Repository Index

## 1. Summary

Build a local, incremental repository map that helps agents find relevant
files, symbols, and project conventions without repeatedly enumerating the
entire tree or stuffing broad file contents into model context.

## 2. Problem and research basis

Agenthicc has file tools, mentions, memory, and caches, but no unified
repository index that answers “where is this symbol or concept?” OpenCode
provides fast file search and integrates language-server navigation; Codex
documents reusable environment/project context. Agenthicc should make an
index an optional local accelerator, not an authority that replaces reading
the current file.

Sources: [OpenCode tools](https://docs.opencode.ai/docs/tools/),
[OpenCode LSP](https://docs.opencode.ai/docs/tools/),
[Agenthicc context reuse](prd-131-context-reuse-and-knowledge-persistence.md).

## 3. Goals and non-goals

Goals: incremental file inventory, symbol/ref search when available, ranked
context snippets, source provenance, and strict privacy/workspace scoping.

Non-goals: sending repository contents to a hosted index, mandatory embeddings,
or making stale index results a substitute for reading a source file.

## 4. Requirements

1. Index only authorized roots and files allowed by `.gitignore`, Agenthicc
   exclusions, symlink policy, file-size limits, and configured secret/binary
   filters. A user must be able to inspect and clear indexed metadata.
2. Store normalized relative paths, file fingerprints, language, symbol names,
   and bounded text snippets in a project-local SQLite/index store. Do not
   duplicate complete source files or persist secret values.
3. Update incrementally from filesystem metadata/change notifications with a
   periodic reconciliation scan; detect renames/deletions and schema upgrades.
4. Offer explicit tools/commands for repo map, symbol lookup, and ranked
   context retrieval. Return path, line range, fingerprint, and reason for
   ranking so every result can be verified against current content.
5. Use lexical search as the required baseline. Semantic/vector ranking is an
   optional backend and may not send content off-device without explicit
   configuration and disclosure.
6. Invalidate entries on file change and before a snippet is used for a write
   plan; do not use stale index text as an edit target.

## 5. Acceptance criteria

- After initial scan, a one-file edit updates only affected index records and
  search returns the current path/line evidence.
- Ignored, outside-root, binary, oversized, and secret-pattern files are not
  indexed under the default policy.
- Symlink escape attempts and stale index entries cannot authorize reads or
  writes; downstream tools revalidate with WorkspaceView.
- Index corruption/rebuild and version migration preserve the project and do
  not block ordinary Agenthicc startup.
- Indexing latency, storage, and query latency are benchmarked on small,
  medium, and large repositories; indexing is cancellable and resource-bounded.

## 6. Testing and dependencies

Add temporary-repository integration tests for ignores, symlinks, rename,
delete, concurrent edits, rebuild, and data redaction. Requires PRD-167
workspace-scoped path consistency; PRD-235 can supply semantic symbols, but
lexical index operation must not depend on LSP availability.
