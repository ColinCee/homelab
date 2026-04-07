#!/usr/bin/env bash
# Fetch PR metadata and diff from GitHub API.
# Outputs: /tmp/pr_title.txt, /tmp/pr_body.txt, /tmp/pr_diff.txt
#
# Required env: GH_TOKEN, REPO, PR_NUMBER
set -euo pipefail

gh api "repos/${REPO}/pulls/${PR_NUMBER}" --jq '.title' > /tmp/pr_title.txt
gh api "repos/${REPO}/pulls/${PR_NUMBER}" --jq '.body // ""' > /tmp/pr_body.txt

# shellcheck disable=SC2016
gh api "repos/${REPO}/pulls/${PR_NUMBER}/files" --paginate --jq '
  .[] | "### \(.filename) (\(.status))\n```diff\n\(.patch // "(binary)")\n```\n"
' > /tmp/pr_diff.txt

# Truncate very large diffs to stay within model context
head -c 80000 /tmp/pr_diff.txt > /tmp/pr_diff_truncated.txt

echo "Fetched PR #${PR_NUMBER}: $(cat /tmp/pr_title.txt)"
echo "Diff size: $(wc -c < /tmp/pr_diff.txt) bytes (truncated to $(wc -c < /tmp/pr_diff_truncated.txt))"
