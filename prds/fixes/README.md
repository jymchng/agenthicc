# Fixes (FIX series)

Defect-driven PRDs: each one starts from a reported failure that was
reproduced and measured against the shipped code, then specifies the fix. They
continue the repository-wide PRD numbering (`prd-NNN`) so they can be
cross-referenced from `../README.md` and from other series.

| PRD | Title | Reported failure | Priority |
|---|---|---|---|
| [PRD-264](prd-264-background-registry-lock-contention.md) | Bounded registry writes and a multi-process-safe session registry | `Unable to background session: OperationalError: database is locked` whenever more than one agenthicc runs | P0 |
| [PRD-265](prd-265-uiux-route-profile-phase-mismatch.md) | A validated route-profile enum for the UI/UX Pro Max workflow | `✗ ValueError: tuple.index(x): x not in tuple` after a successful `product_discovery` turn, and the paused run cannot be resumed | P1 |

## Scope of this folder

- A fix PRD carries the incident evidence (measurements, reproduction, file and
  line references), the goals and non-goals, functional and non-functional
  requirements, acceptance criteria with numbers where possible, a test plan at
  unit/integration/e2e/performance level, and a rollout/recovery path for
  existing user data.
- It does not restate a design that another PRD already owns; it links to it.
  PRD-264, for example, reuses PRD-254's schema inspection, migrations and
  cross-process maintenance lock rather than replacing them.
- Where a fix supersedes part of an earlier PRD, say so explicitly and keep the
  earlier document as design history (see the status convention in
  [`../README.md`](../README.md)).

## Order

Nothing is implemented yet. If the series grows, order by blast radius: the
failures that stop a user from working at all (lost handoff, unusable session
manager) come before those that merely make the TUI sluggish.
