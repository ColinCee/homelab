# ADR-014: Pinned dependencies, human merges

**Status:** Accepted

Renovate opens dependency PRs but does not merge them. Dependency changes run
code on the homelab; passing CI is not approval to deploy.

Keep Docker digest and GitHub Actions SHA pinning. Ordinary updates wait three
days; vulnerability alerts skip that delay but still require human merge.
Group Python and uv changes across mise and Dockerfiles to reduce version drift.

The policy is defined in [renovate.json](../../.github/renovate.json). Do not add
a custom dependency bot or a separate version-parsing script.
