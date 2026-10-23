#!/usr/bin/env bash
# Require every CI gate and code-owner review before anything merges into a
# branch (PRD-250 WS-1.3 and WS-1.9).
#
# Run once, by a repository administrator, with an authenticated GitHub CLI:
#
#     scripts/apply_branch_protection.sh                # main of the current repo
#     scripts/apply_branch_protection.sh OWNER/REPO dev
#
# "Required checks" is the aggregate job at the end of .github/workflows/ci.yml;
# it fails unless lint, typecheck, type audit, llms, docs, unit, integration,
# E2E, Windows and coverage all succeeded, so it is the only status check that
# has to be named here. Code-owner review makes .github/CODEOWNERS binding for
# the quality baselines.
#
# Administrators are deliberately not bound (enforce_admins=false): GitHub
# never lets a pull request's author approve it, so on a single-maintainer
# repository the owner would otherwise be unable to merge their own changes.
set -euo pipefail

repo="${1:-$(gh repo view --json nameWithOwner --jq .nameWithOwner)}"
branch="${2:-main}"

gh api --method PUT "repos/${repo}/branches/${branch}/protection" --input - <<'JSON'
{
  "required_status_checks": {
    "strict": true,
    "checks": [{"context": "Required checks"}]
  },
  "enforce_admins": false,
  "required_pull_request_reviews": {
    "require_code_owner_reviews": true,
    "required_approving_review_count": 1,
    "dismiss_stale_reviews": true
  },
  "restrictions": null,
  "required_linear_history": false,
  "allow_force_pushes": false,
  "allow_deletions": false
}
JSON

echo "Protected ${repo}@${branch}: merges need 'Required checks' and code-owner review."
