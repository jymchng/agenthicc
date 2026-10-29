---
title: "SUBAGENT series — final coverage and scope audit"
status: Historical
version: 0.1.0
date: 2026-10-29
scope: "the delivery audit for the SUBAGENT series: the §5.6 area-to-PRD bijection, the §6.7 mechanical and human checks, and the documentation-only scope proof, at commit 4fd8cbe"
related_prds:
  - SUBAGENT-01
  - SUBAGENT-02
  - SUBAGENT-03
  - SUBAGENT-04
  - SUBAGENT-05
  - SUBAGENT-06
  - SUBAGENT-07
  - SUBAGENT-08
  - SUBAGENT-09
  - AGX-08
tags:
  - subagents
  - audit
  - verification
---

# SUBAGENT series — final coverage and scope audit

## 1. What this record is, and what it is not

This is the delivery audit for the series: the check that
`prds/SUBAGENT-research-harness-comparison.md` §5.6 says happens "mechanically
at the end of the series", and that §6.7 says re-runs the contract's sixteen
items "against every `SUBAGENT-NN` document".

It is a **record**, not a tenth area and not a tenth PRD. Three consequences,
stated rather than implied:

- It carries **no `area:` key**. Area membership is what §5.6 makes
  authoritative, and a record that assigned itself an area would corrupt the
  bijection it exists to verify. The eight-key frontmatter of §6.2 is required
  of `SUBAGENT-NN` documents (§6.2, "Every `SUBAGENT-NN` document"); this file
  is not one, so it carries the keys that mean something for a record and omits
  `area`.
- It is **outside** the §5.6 table by construction, and the coverage check in
  §3 therefore tests `SUBAGENT-NN` filenames, not `SUBAGENT-*`. The unnumbered
  research report is excluded on the same rule, which is what §5.6 already
  assumed when it called itself a bijection over nine areas.
- The nine-section body of §6.3 does not apply to a record. It has the sections
  a record needs.

**Why not append this to the research report.** §6.8 freezes it — "This
document is `Historical` and frozen at `f843040` plus the research commits;
PRDs cite it, they do not maintain it" — and names §5.6 as the single exception.
An audit section would be a seventh top-level section in a document that
forbids sixth-section additions, so the result is recorded here instead and the
report stays frozen. §5.6 needed no edit: the table it holds proved correct, so
the one permitted exception was not exercised.

## 2. Method and reproduction

Run on 2026-10-29 at `4fd8cbe8965496c57b8f1f9316e53cbe432387e0`, with the
working tree clean.

The per-document checks implement §6.7's items 1–12 as written, in a script
whose definitions are that table and no others: frontmatter parse (delimiters
stripped first, as item 1 requires), the eight keys, `status == Proposed`, ISO
`date`, `area` against the §5.6 row, sections 1–9 plus appendix A in order,
≥3 distinct `file:line` citations and a 40-character sha in §2, every citation
resolving at the named commit and within the file's length, a command in §3,
every acceptance criterion naming a test or command, every §8 test path
existing, a relationship verb per `related_prds` entry, and appendix rows
carrying a commit or a scheme-qualified URL plus a depth label from §6.5.

Two things the audit did that the contract does not spell out, both recorded
because a silent extra check is a silent change to the standard:

- **A thirteenth check: `title` equals the H1 and begins `SUBAGENT-NN: `.** This
  is §6.2's first rule, and §6.7's item list does not enumerate it. Every
  document passes it. It is reported separately rather than folded into the
  twelve so the count stays comparable to the contract.
- **One audit-tooling defect, found and fixed before any result was recorded.**
  The first run reported item 12 failing on all nine documents with
  `depth='---'`. The cause was in the checker, not the documents: its
  table-separator filter had been written as `^\[\s\-:|\]+\|$` instead of
  `^\|[\s\-:|]+\|$`, so the `|---|---|---|---|---|` rule row was read as a data
  row. Corrected, all nine pass. This is recorded because an audit that hides
  its own first result is worth less than one that shows it was wrong.

Reproduction of the two series-level results does not need the checker:

```console
$ git rev-parse HEAD
4fd8cbe8965496c57b8f1f9316e53cbe432387e0
$ ls prds/SUBAGENT-[0-9][0-9]-*.md | wc -l    # 9 numbered PRDs
$ grep -c '^| A-' prds/SUBAGENT-research-harness-comparison.md   # 9 (§5.6 rows)
$ for f in prds/SUBAGENT-0*.md; do grep -m1 '^area:' $f; done    # A-1 … A-9
$ git diff --name-only f843040..HEAD | grep -v '^prds/'           # no output
```

**The four human items are reported as unverified, not passed.** §6.7 is
explicit that "an audit that reports a semantic judgement as a checked item is
worse than one that admits it did not check", so §5 below does exactly that and
separates what was spot-checked from what is a judgement.

## 3. Coverage: the §5.6 area-to-PRD bijection

Nine rows in the table, nine `SUBAGENT-NN` files on disk, and the two sets are
equal. Each PRD's `area:` key matches the area the table assigns to its
filename; no area has two PRDs; no `SUBAGENT-NN` document exists outside the
table; no table row lacks a file.

| Area | PRD | `area:` matches table | File exists |
|---|---|---|---|
| A-1 | `SUBAGENT-01-declarative-subagent-types.md` | yes | yes |
| A-2 | `SUBAGENT-02-per-type-model-and-context-routing.md` | yes | yes |
| A-3 | `SUBAGENT-03-structured-result-contracts.md` | yes | yes |
| A-4 | `SUBAGENT-04-nesting-depth-and-fan-out.md` | yes | yes |
| A-5 | `SUBAGENT-05-per-type-permission-narrowing.md` | yes | yes |
| A-6 | `SUBAGENT-06-pool-budgets-and-limits.md` | yes | yes |
| A-7 | `SUBAGENT-07-background-orchestration.md` | yes | yes |
| A-8 | `SUBAGENT-08-worker-lifecycle-controls.md` | yes | yes |
| A-9 | `SUBAGENT-09-observability-and-transcript-contract.md` | yes | yes |

**Verdict: the bijection holds, 9 ↔ 9.** Every written filename also matches
the provisional filename §5.6 recorded before the documents existed, so no row
needed updating and the one permitted exception stayed unused.

## 4. The twelve mechanical items, per document

All nine documents pass all twelve §6.7 mechanical items, plus the extra
`title`/H1 check of §2.

| Area | Document | §6.7 items 1–12 | +`title`/H1 | Commit resolved for citations |
|---|---|---|---|---|
| A-1 | SUBAGENT-01 | 12/12 | pass | `0115674` |
| A-2 | SUBAGENT-02 | 12/12 | pass | `4994e2a` |
| A-3 | SUBAGENT-03 | 12/12 | pass | `ffbfa0d` |
| A-4 | SUBAGENT-04 | 12/12 | pass | `0592533` |
| A-5 | SUBAGENT-05 | 12/12 | pass | `f700733` |
| A-6 | SUBAGENT-06 | 12/12 | pass | `acc7b21` |
| A-7 | SUBAGENT-07 | 12/12 | pass | `cc7b1b8` |
| A-8 | SUBAGENT-08 | 12/12 | pass | `086849b` |
| A-9 | SUBAGENT-09 | 12/12 | pass | `603d559` |

Every citation in every document resolved to a real line inside the file's
length at the named commit; the only non-agenthicc references are the
competitor paths and URLs in each appendix, each of which sits in a row
carrying its own pinned commit or scheme-qualified URL, as §6.5 requires.
Every document also names at least three distinct `file:line` citations and a
40-character sha in §2, carries a command in §3, gives every acceptance
criterion a test file or runnable command, references only §8 test paths that
exist, and gives every `related_prds` entry a relationship verb.

**Verdict: 12/12 × 9 documents, zero failures.** The contract's own pre-commit
exercise (§6.7, a synthetic compliant document and a seven-defect variant)
predicted that the wrong `area` would cascade into the §5.3 non-goal
comparison; no document here triggered it, because no `area` is wrong.

## 5. The four human items

Reported as **unverified**, with what was actually spot-checked named. These
are judgements, and calling them "passed" would be the failure mode §6.7 warns
about.

| # | Item | Status | What was spot-checked |
|---|---|---|---|
| 13 | §3's absent claims each name a pattern and a scope | unverified | Every §3 opens with scoped commands — `grep -rn … --include=*.py src`, `grep -n … src/agenthicc/subagents/`, `sed -n 'A,Bp'`, `git rev-parse HEAD` — rather than a bare "not found". Spot-checked in all nine. Whether each *claim* is fully supported is a reading judgement left to review |
| 14 | §5 non-goals match the area's "does not own" in §5.3 | unverified | §5.3 carries a "Does not own" line for every area. Spot-checked A-1 (tool behaviour → A-5, model → A-2), A-4 (the semaphore itself stays), A-6 (the *act* of aborting → A-8 via M-1) and A-9 (panel information architecture → AGX-08 phase 1); each is reflected in the matching PRD's §5. The remaining five were not compared line by line |
| 15 | `date` matches when §3's investigation ran | unverified, mechanically corroborated | All nine carry `date: 2026-10-29` and all nine §3 sections say "Run on 2026-10-29" — a nine-of-nine match, but whether the commands were truly run that day is not something a script can witness |
| 16 | §9 gives the *correct* relationship verb, not merely a verb | unverified | Item 11 proves a verb exists; correctness was sampled. A-9's AGX-08 entry ("formalises, narrows against") was read against AGX-08's own scope and holds; the other 40-odd entries were not adjudicated |

## 6. Scope: the change is documentation-only

```console
$ git diff --name-only f843040..HEAD     # run at 4fd8cbe, before this record existed
prds/README.md
prds/SUBAGENT-01-declarative-subagent-types.md
prds/SUBAGENT-02-per-type-model-and-context-routing.md
prds/SUBAGENT-03-structured-result-contracts.md
prds/SUBAGENT-04-nesting-depth-and-fan-out.md
prds/SUBAGENT-05-per-type-permission-narrowing.md
prds/SUBAGENT-06-pool-budgets-and-limits.md
prds/SUBAGENT-07-background-orchestration.md
prds/SUBAGENT-08-worker-lifecycle-controls.md
prds/SUBAGENT-09-observability-and-transcript-contract.md
prds/SUBAGENT-research-harness-comparison.md
$ git diff --stat f843040..HEAD -- src/ tests/
(empty)
$ git status --porcelain
(empty)
```

Eleven files at the audited commit, every one under `prds/`. No `src/` or
`tests/` change, no `mkdocs.yml`, no CI workflow, no packaging or build
configuration, no dependency file. `prds/` is not in the mkdocs navigation and
is not referenced by `scripts/` or `.github/`, so the change cannot affect the
strict docs build or any pipeline — the series asserts behaviour it has not
implemented, and the tree proves it did not.

Committing this record makes the set twelve: `prds/SUBAGENT-series-audit.md`
itself, plus the one README bullet that indexes it. Both are documentation under
`prds/`, so the verdict does not move, and the check above can be re-run after
the series' final commit with the same result.

**Verdict: documentation-only, 0 files outside `prds/`.**

## 7. The M-1 mirror

The M-1 boundary (§5.4) splits one decision across two documents: A-6 decides
*when* a budget is exceeded; A-8 decides *what happens to the workers*. The
second half of this audit — that `SUBAGENT-06` and `SUBAGENT-08` carry that
split in identical words, from the same citation, without either defining a
second cancellation path — is recorded here when run.

**Status: pending.**
