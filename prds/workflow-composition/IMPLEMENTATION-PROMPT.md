# Prompt — implement the WFC series (workflow composition and agent self-extension)

**How to use.** Start an AI coding agent in `/root/python_projects/agenthicc` and
tell it: *"Follow `prds/workflow-composition/IMPLEMENTATION-PROMPT.md` exactly."*
Everything below the rule is the agent's instruction. It authorises the agent to
commit, push **one branch**, open **one pull request to `main`**, watch CI and
merge it — for this task only.

---

You are implementing the **WFC series**: fourteen PRDs in
`prds/workflow-composition/` that let agents enter and return from workflows,
spawn subagents that run workflows, and create workflows, tools and skills that
are trusted, reloaded and usable in the same session. The specification is
written; your job is to implement it completely, on **one branch**, with regular
commits, pushes at every stable milestone, and — when everything is done — one
pull request to `main` that you watch through CI and merge.

## 1. Definition of done

All of the following, with evidence in your final report:

1. WFC-01 … WFC-14 are implemented. Every acceptance criterion in each PRD §7
   (and WFC-13 S1–S9) passes in an automated test that you can name.
2. Every symbol a PRD supersedes is **deleted**, and the legacy-symbol searches
   in WFC-13 §8 return nothing (outside historical PRDs and `CHANGELOG.md`).
3. Each PRD's §9 documentation changes are made, `llms-full.txt` has an entry for
   every new public symbol, and the traceability table in WFC-13 §7 has no empty
   cell.
4. Locally green: ruff check + format, `mypy src/agenthicc`, the type-safety
   ratchet, `tests/unit`, `tests/integration`, `tests/e2e`, `tests/performance`,
   `nox -s llms_check`, `uv run mkdocs build --strict`.
5. One PR from the branch to `main` has a green **Required checks** job and is
   merged.

## 2. What you may and may not do

You may: create the branch below, commit to it, push it, open one PR to `main`,
watch CI, and merge that PR when **Required checks** is green.

You may not:

- push to `main` directly, force-push anything, or rewrite pushed history;
- skip hooks (`--no-verify`) or weaken a gate (a test, a ruff rule, a mypy
  setting) to make it pass;
- raise `docs/reference/type-safety-baseline.json`, a performance budget or the
  coverage floor — those need an explicit human approval label. If a change
  seems to require it, stop and report (§12);
- change branch protection, repository settings, secrets or labels, or run
  `scripts/apply_branch_protection.sh` (the owner decided `main` stays
  unprotected; CI plus local hooks are the gates);
- touch the repository owner's unrelated uncommitted work (§5);
- start another Claude session, hand work to a peer session, or use subagents.
  Do the work sequentially in this session. (If the operator who launched you
  explicitly allows subagents, use them only for the two parallel pairs in
  steps 1, 2 and 8, each in its own git worktree off this branch with disjoint
  files, and merge their results yourself.)

Report outcomes faithfully. If a test fails, say so with the output. If a step
was skipped or a criterion is not met, say that. Never claim "done" for work you
did not run.

## 3. Read first, in this order

1. `CLAUDE.md` and `AGENTS.md` — invariants, change patterns, gates, and the
   working rules for agents (hooks and pushing, subagents, replace-don't-layer).
   Read them **again after the second setup commit** in §5, which updates them.
2. `prds/workflow-composition/README.md` and `WFC-00-programme-and-study.md`
   (findings F1–F11, architecture, **§5 delivery order**, cross-cutting rules §6).
3. Each PRD in full **when its step begins**, plus every source file it names.
4. `IMPLEMENTATION-LOG.md` in the same directory, if it exists (§6.5) — it tells
   you where a previous run stopped.

## 4. Rules that apply to every change

- **Study before you implement.** Before changing anything, read the whole call
  chain it touches. PRD line numbers were true on 2026-11-22; re-verify each one,
  and if the code differs, amend the PRD (§10) instead of forcing the code to
  match. No fix based on an error message alone.
- **Replace, don't layer.** No backwards-compatibility shims, aliases, dual
  paths, `Optional` escape hatches or `# type: ignore` left over from old types.
  When a PRD supersedes a symbol, delete it, fix every caller and test, and
  `grep -rn` to prove zero hits. The project is not in production.
- **Types.** Every new or changed signature and non-trivial variable has a
  concrete type. `typing.Any` appears **zero** times in new code; use real types
  under `TYPE_CHECKING`, `object` only as a last resort, and frozen dataclasses
  or `TypedDict` for payloads. Use `X | None`, `list[T]`, `dict[str, T]`.
- **Two state containers.** Kernel `AppState` is frozen and changes only through
  events and `root_reducer`; `tui/conversation_store.AppState` is reactive
  presentation state. Never swap one for the other; the bridge lives in
  `runners/tui_session.py` and is tested (WFC-03 §5.2).
- **Pure reducer, owned resources.** `root_reducer` does no I/O. A session owns
  and closes everything it creates. File access goes through `WorkspaceView`;
  network through the shared HTTP client.
- **Security invariants of this series (never trade these away):**
  trust is recorded only inside the session's publish step (`store.commit`) and
  by the existing `agenthicc trust` CLI; no model-facing tool can reach a trust
  mutator (a static test enforces it); only bytes that passed the runner-owned
  verdict are trusted, digest-checked at commit; no authoring path writes under
  `~/.agenthicc`; child scopes only narrow their parent; trust is not permission
  — capability gate and approvals still apply on every call.
- **No new subagent limits** (WFC-04). Subagents run to completion.
- **Docs in the same change** as behaviour: the PRD's §9 guides,
  `llms-full.txt`, `docs/glossary.md` where named. If you rename or remove a
  module, search the whole repository for the old path.
- **Tests first where practical**, placed per CLAUDE.md (unit / integration /
  e2e / performance), using the shared fixtures and markers.

## 5. Setup (do this once)

The PRDs are **uncommitted** in the original checkout, and that checkout also
holds the owner's unrelated work in progress. Do not disturb it: work in a
dedicated worktree.

```bash
cd /root/python_projects/agenthicc
git status --short            # expect: M prds/README.md, ?? prds/fixes/, ?? prds/workflow-composition/
git fetch origin
git worktree add /root/python_projects/agenthicc-wfc -b feat/workflow-composition origin/main
cp -r prds/workflow-composition /root/python_projects/agenthicc-wfc/prds/
cd /root/python_projects/agenthicc-wfc
uv sync --extra dev --extra book --extra playwright --extra cloakbrowser --inexact
uv run pre-commit install --install-hooks 2>/dev/null || true   # hooks may already be installed in the shared .git
```

- **Never** run `uv sync --extra dev` without `--inexact`: it uninstalls the
  optional extras and breaks book/screenshot tests.
- The branch is `feat/workflow-composition`, created from a freshly fetched
  `origin/main`. Do **not** base it on whatever branch the original checkout has
  open.
- Linux needs `bubblewrap` (`bwrap`); it is installed here. Windows has no
  sandbox: validation must fall back to AST-only with `validation="static_only"`
  (WFC-11 §5.5), and `nox -s tests_windows` runs in CI.
- If you are already running in a clean clone with the PRDs committed, skip the
  worktree and the copy.

**First commit — the specification.** `prds/README.md` in the original checkout
mixes my WFC block with two of the owner's unrelated hunks (the removed
SUBAGENT-14/15 index lines and the "FIX series" block) and an untracked
`prds/fixes/`. None of those belong on your branch. In the worktree, take
`prds/README.md` exactly as `origin/main` has it and insert **only** the block
from the heading `### WFC series — workflow composition and agent self-extension`
through the `WFC-14` line, immediately before the `### PRD-220–239` heading (if
that anchor has moved, put it after the last series section). Then:

```bash
git add prds/workflow-composition prds/README.md
git commit -m "docs(prds): WFC series — workflow composition and agent self-extension (WFC-00…14)"
```

Run the series checks before committing: all relative links resolve, front
matter parses, no stale references. This commit is documentation only.

**Second commit — the working rules.** The owner also has uncommitted updates to
`CLAUDE.md`, `AGENTS.md`, `CONTRIBUTING.md` and `docs/contributing.md` that encode
the rules this prompt relies on (the hook and push mechanics, the exact-sync
`--inexact` trap, no branch protection, subagent invariants, replace-don't-layer,
no `Any` in new files). They apply cleanly to `origin/main`. Carry them over as a
patch — never copy the whole files — then re-read `CLAUDE.md` and `AGENTS.md`:

```bash
git -C /root/python_projects/agenthicc diff -- CLAUDE.md AGENTS.md CONTRIBUTING.md docs/contributing.md \
  > /tmp/wfc-guidance.patch                       # any scratch path outside the repository
cd /root/python_projects/agenthicc-wfc
git apply --check /tmp/wfc-guidance.patch && git apply /tmp/wfc-guidance.patch
uv run mkdocs build --strict                       # these files are on the documentation surface
git add CLAUDE.md AGENTS.md CONTRIBUTING.md docs/contributing.md
git commit -m "docs: encode agent and contributor working rules in CLAUDE.md and AGENTS.md"
```

If `git apply --check` fails because `origin/main` moved, resolve by hand: the
intent of each hunk is listed above, and a rule already present on `main` needs no
second copy. The two repository-wide docs are in scope only for these rules.

## 6. Git workflow

### 6.1 Identity

No git identity is configured. Set it in the environment for each command
(never edit git config):

```bash
export GIT_AUTHOR_NAME="Claude Code" GIT_AUTHOR_EMAIL="noreply@anthropic.com" \
       GIT_COMMITTER_NAME="Claude Code" GIT_COMMITTER_EMAIL="noreply@anthropic.com"
```

End every commit message and the PR body with any attribution lines your own
harness instructs you to add.

### 6.2 Commit regularly

Commit locally whenever a coherent slice works — a new module with its tests, a
deletion with its callers fixed, a docs update. Aim for commits a reviewer can
read: one idea each, tests included. Use the repository's conventional style:

```text
feat(workflows): <what changed, imperative, ≤ 72 chars> (WFC-01)

<2–6 lines: the why, the seam it touches, what was deleted>

PRD: prds/workflow-composition/WFC-01-workflow-call-stack.md
```

Types: `feat`, `fix`, `refactor`, `test`, `docs`, `perf`. Mark breaking changes
`feat(scope)!:` (the `create_tools` rename, removed commands). A PRD that is
fully implemented gets a final `docs(prds):` commit that adds its
**Implementation status** section (criterion → test name → evidence) and flips
its `status` to `Implemented`; do the same in the series README table.

Never commit a state that fails ruff, ruff format or mypy. A half-migrated state
(a symbol deleted but callers not yet updated) is not committable.

### 6.3 Push at milestones

A **milestone** is the end of each step in §7 (M1 … M9). Push the branch at every
milestone, and also after any large stable slice inside a step. **Stable** means:
targeted tests for the slice pass, ruff and mypy are clean, nothing is
half-migrated, and the full milestone gate (§9) has been run.

CI does not run on pushes to feature branches (only on pushes to `main`/`dev`
and on PRs), so **the pre-push hook is your gate on the branch**. It runs mypy,
the type-audit ratchet and the whole `tests/unit` suite on the working tree and
takes about 10½ minutes — longer than a foreground tool timeout. Therefore:

```bash
git push -u origin feat/workflow-composition     # run in the background, then poll until it exits
```

- Run it in the background and wait for it to finish (Monitor or equivalent).
- **Do not edit, stage or generate any file in the worktree while it runs.** The
  hook stashes unstaged files and restores them afterwards; any change fails it
  with "files were modified by this hook".
- If it fails, read the output, fix the cause with a new commit, and push again.
  Never `--no-verify`.
- A timing-sensitive failure under host load (`uptime`) is re-run in isolation
  before you treat it as a regression; say in the log that you did.

### 6.4 Never lose the working tree

If `git status` says "must be run in a work tree", run
`git config --local core.bare false` and check for a stray test
`user.name`/`user.email`. A zero-byte `.git/index.lock` with no holder (scan
`/proc/*/fd`) may be removed; verify nothing holds it first.

### 6.5 Keep a log you can resume from

Maintain `prds/workflow-composition/IMPLEMENTATION-LOG.md`: one row per step and
per PRD — status, commit hash, gates run and their result, spike outcomes,
amendments, anything skipped. Commit it with each milestone. On any restart,
read it and `git log feat/workflow-composition` first, and continue from the
first incomplete row.

## 7. The work, in order

Follow WFC-00 §5.2. Implement each PRD **fully** (every criterion, its deletions,
its docs) before moving on. Steps marked ‖ are independent; do them one after the
other.

| Step | PRDs | Milestone push after |
|---|---|---|
| 0 | Three spikes (below) | M0 — spike results committed into the PRDs |
| 1 | WFC-14 ‖ WFC-05 ‖ WFC-13 harness | M1 |
| 2 | WFC-11 ‖ WFC-10 | M2 |
| 3 | WFC-01, then connect "pop → publish step" | M3 |
| 4 | WFC-02 | M4 — **outcome 1** |
| 5 | WFC-06 | M5 — **outcome 3** |
| 6 | WFC-08, then WFC-07 | M6 — **outcome 4** |
| 7 | WFC-09 | M7 — **outcome 5** |
| 8 | WFC-04 ‖ WFC-03 | M8 — **outcome 2** |
| 9 | WFC-12 remainder, WFC-13 close-out, final sweep | M9 — then the PR |

**Step 0 — spikes** (no production code; commit only the tests they justify):

1. Does lauren-ai accept a changed tool list between agent-loop iterations?
   (WFC-08 §5.2.) Record yes/no; if no, drop same-turn pickup — the primary
   guarantee in WFC-08 §5.3 does not depend on it.
2. Can lauren-ai's schema generator express `dict[str, str] | None` for a tool
   argument? (WFC-02 §5.2, WFC-04 §5.1.) If not, use the JSON-string fallback the
   PRDs name. Commit the schema test.
3. Does a Bubblewrap-sandboxed subprocess import harness work (no network,
   read-only workspace, bounded time and output)? (WFC-11 §5.5.)

Write each outcome into the relevant PRD as a dated **Amendment** (§10).

**Step-specific traps** (each is already in the PRD; they are listed because they
are easy to miss):

- **WFC-14.** Extract from `create_workflow` rather than reinventing: generalise
  `generation_tools.make_generation_writer_tools`, move `repair_cycles` /
  `_max_repair_cycles` into a persisted `RepairBudget`, and delete the originals.
  The sandboxed import harness (§5.5) is wired when WFC-11 lands.
- **WFC-05.** Delete the four `TUISession._reload_*` methods and the four
  `CommandContext.reload_*` callbacks; `/skills`, `/commands`, `/tools`,
  `/workflow` reload subcommands and the new `/reload` all call the service.
  Discovery runs off the event loop; add the lag test to `tests/performance`.
- **WFC-11 / WFC-01 seam.** Build WFC-11's types, store, publish step,
  provenance, `/authored` commands and `agenthicc authored` CLI first, tested by
  calling the publish step directly. Connect "pop → publish step" as the last
  slice of WFC-01. Add no stub trigger in between.
- **WFC-11 / WFC-06.** `ExtensionTrust.trust` for authored code ends up called
  only from `store.commit` (including `finalize_authoring`) and the
  `agenthicc trust` CLI; remove the runner-side call in `create_workflow`. Add
  the static tests (no tool module references a trust mutator; no authoring path
  can write under `~/.agenthicc`).
- **WFC-01.** `_workflow_override`, `_set_workflow_override`,
  `_reset_workflow_to_mode_default` and `conversation.workflow_override` are
  deleted; fix the five write sites and every reader.
- **WFC-08.** Remove all four private tool snapshots (`WorkflowConfig.plugin_tools`
  replacement, per-turn list, `_tool_bundle_cache`, `SubagentPool._all_tools`).
- **WFC-07.** The rename leaves no alias. Delete the `create-tools` (and, in
  WFC-06, `create-workflow`) bootstrap skills through the bootstrap marker
  mechanism (remove an unmodified seeded copy; leave and report an edited one).
  Add the `CHANGELOG.md` breaking entry.
- **WFC-04.** Extract `run_workflow_core` from `headless.execute_workflow` and
  have both call it. Add no turn, time or concurrency cap for workers.
- **WFC-03.** New events need a reducer handler, `_HANDLERS` registration, a
  reducer test, and `llms-full.txt`/architecture docs, per "Adding an event" in
  CLAUDE.md.
- **Every PRD that adds a slash command** registers it in `commands/builtins.py`
  with picker-visibility **and** execution tests.

### The per-PRD loop

1. Re-read the PRD; verify its source references; read the full call chain.
2. Write the tests the PRD's §8 names (they will fail first).
3. Implement; delete what the PRD supersedes; fix every caller and test.
4. Run the slice's tests, ruff, mypy; commit.
5. Make the PRD's §9 documentation changes; commit.
6. Run the PRD's search checks (zero legacy hits, no `Any` in new files).
7. Tick the PRD's criteria with test names; add its **Implementation status**
   section; update the log; commit.

## 8. Windows and platform care

`nox -s tests_windows` runs in CI on a Windows runner and you cannot run it here.
Keep POSIX-only calls in `posix_backend.py`-style modules, never assume
Bubblewrap, use `pathlib`, and make new tests either platform-neutral or
explicitly skipped with a reason. Review each new module for POSIX assumptions
before the milestone push.

## 9. Gates

| When | Run |
|---|---|
| Every commit | the slice's tests; `uv run ruff check src/ tests/ scripts/`; `uv run ruff format --check src/ tests/ scripts/` (pre-commit runs ruff) |
| Every milestone, before pushing | `uv run mypy src/agenthicc`; `uv run python scripts/type_audit.py --check docs/reference/type-safety-baseline.json`; `uv run pytest tests/unit -q`; `uv run pytest tests/integration -q`; `uv run pytest tests/e2e -q` (and `tests/performance -q` when the step touched it); `uv run nox -s llms_check` when public symbols changed; `uv run mkdocs build --strict` when docs changed |
| Before the PR (M9) | all of the above, plus `uv run nox -s typecheck_contracts`, the legacy-symbol searches in WFC-13 §8, the no-`Any` search over new files, and a filled traceability table |

The full coverage session takes about an hour locally; CI runs it. Do not run it
locally unless CI's coverage job fails and you must reproduce.

Run full-suite commands in the background and poll. `uptime` before trusting a
timing failure; re-run a suspected flake in isolation and record that you did.

## 10. When a PRD turns out to be wrong

The PRDs are the spec, not scripture. If a source reference is stale, a spike
removes a design branch, or a requirement cannot be met as written:

- if the PRD already names a fallback, take it and record it;
- otherwise make the smallest change that preserves the PRD's goals and the
  security invariants in §4, and write a dated **Amendment** at the end of that
  PRD (what was assumed, what is true, what you did, which criteria changed);
- commit the amendment *with* the code that needs it;
- if the change would weaken a security invariant, change an outcome, or
  contradict another PRD, **stop and report** (§12) instead of deciding alone.

When in doubt about trust semantics, choose the more restrictive reading and log
it.

## 11. Pull request, CI and merge

Open the PR only when **M9 is pushed and every item in §1 holds locally**.

```bash
gh pr create --base main --head feat/workflow-composition \
  --title "feat: workflow composition and agent self-extension (WFC-01…14)" \
  --body-file <path to a body you wrote>
```

The PR body: the five outcomes and how each is demonstrated; the step/milestone
list with commit ranges; **breaking changes** (`make_agenthicc_tool` →
`create_tools`; removed `$create-tools` / `create-workflow` bootstrap skills;
removed `_reload_*` and the sticky workflow selection; authored extensions are
now trusted automatically, with the controls that bound that and the
`stage_only` opt-in); the gates you ran and their results; amendments and
deviations; anything not done.

Then watch CI until the **Required checks** job finishes:

```bash
gh pr checks --watch --interval 60
gh run list --branch feat/workflow-composition --limit 5
```

- **A job fails.** `gh run view <run-id> --log-failed`, reproduce it locally with
  the same nox session or pytest selection, fix with a new commit, push (the
  pre-push gate runs), and watch again. CI cancels the in-progress run when you
  push, so batch related fixes into one push instead of pushing one at a time.
- **Windows, perf or coverage fail only in CI.** These are the jobs you could
  not run locally; treat them as the likeliest source of surprises. Fix the
  cause; do not skip or weaken the test.
- **Suspected flake.** Confirm it from the log first. Re-run with
  `gh run rerun --failed` only for demonstrable infrastructure failures
  (runner, network), and say so in the PR.
- **A ratchet blocks** (type baseline, performance budget, coverage floor):
  stop and report (§12); do not raise it yourself.
- The coverage job is slow. Wait for it; do not merge on a partial result.

When **Required checks** is green and the PR is mergeable:

```bash
gh pr merge --merge --delete-branch
```

Use a merge commit (this repository merges PRs with merge commits; do not
squash — the per-PRD history is the point). After merging:

1. `git fetch origin` and confirm `origin/main` contains the merge commit.
2. Watch the CI run that the merge triggers on `main` to green. If it fails
   because of this change, fix forward in a new PR promptly and say so.
3. Remove the worktree: `git worktree remove /root/python_projects/agenthicc-wfc`
   (only when clean).

## 12. Stop and report instead of guessing

Stop, write the state into `IMPLEMENTATION-LOG.md`, push what is stable, and
report if:

- a spike or a source fact invalidates a PRD design with no named fallback;
- an invariant in §4 would have to be weakened, or a PRD contradicts another;
- a ratchet (type baseline, perf budget, coverage floor) would have to rise;
- the same failure survives three genuine fix attempts;
- a failure in code you did not touch cannot be explained;
- CI fails for reasons outside the repository and a re-run does not clear it;
- anything would require touching the owner's unrelated work or repository
  settings.

## 13. Final report

End with a report the owner can act on:

- **Result**: merged or not, the PR URL, the merge commit, the CI run that
  covered it.
- **Per PRD**: implemented / partial / not done, with the commit and the test
  names that prove each criterion (point to the WFC-13 traceability table).
- **Deviations and amendments**, each with its reason.
- **What you verified and how** (commands and outcomes), and what you could not
  verify (for example Windows).
- **Follow-ups** you chose not to do, and the next command for the owner —
  including: the owner's original checkout still has an untracked
  `prds/workflow-composition/` copy that will conflict with the merged one when
  they `git pull`; they should delete it first.
