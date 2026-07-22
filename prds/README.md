# Product requirements index

This directory contains the project's product requirements and implementation
notes. Many documents are historical design records; verify their status
against the current source before implementing them.

## Current repository roadmap

- [PRD-138 — Repository Improvement Roadmap](prd-138-repository-improvement-roadmap.md)
- [PRD-139 — OpenCode-Inspired Product Expansion and Privacy-First Advertisements](prd-139-opencode-inspired-features-and-privacy-first-ads.md)

PRD-138 is the current cross-cutting roadmap for documentation truth,
packaging, state boundaries, security, workflow correctness, persistence,
observability, extension APIs, and release gates.

PRD-139 is the product-expansion roadmap layered on top of PRD-138. It compares
the current repository with OpenCode-inspired product surfaces and defines the
privacy, lifecycle, and rendering contract for advertisements.

## Existing PRDs

The numbered PRDs in this directory record individual feature decisions and
acceptance criteria. Use `rg '^# PRD|^#' prds -g '*.md'` to search them by title.
When a PRD is implemented, update its status and link the implementation and
verification evidence. When superseded, keep the file and add a superseded-by
link rather than deleting the design history.

## Status convention

New or revised PRDs should include:

- status (`Proposed`, `In progress`, `Implemented`, `Superseded`, or
  `Historical`);
- date and scope;
- evidence-backed problem statement;
- goals and non-goals;
- acceptance criteria;
- rollout/migration and security considerations;
- verification commands or test references;
- links to superseding or related PRDs.
