# ADR-014: Supply chain hardening and auto-merge policy

**Date:** 2026-04-19
**Status:** Accepted

## Context

Renovate manages dependency updates but had a single blanket auto-merge rule
(docker-compose minor/patch only). Most PRs piled up without merging. The repo
runs a self-hosted GitHub Actions runner on beelink — compromised dependencies
execute directly on the server with Docker socket access and Tailscale network
visibility. Supply chain attacks like the Axios npm compromise (March 2026,
Lazarus Group, ~600K installs in 3 hours) demonstrate that auto-merging without
safeguards is a direct path to infrastructure compromise.

## Options Considered

### Option A: No auto-merge — review everything manually

Safest, but unsustainable. Dependency PRs pile up and never get merged, leaving
the server running stale software with known vulnerabilities.

**Verdict:** Rejected. The cure is worse than the disease.

### Option B: Auto-merge everything with no delay

Fast, but one compromised package auto-deploys to the server. The Axios attack
was live for only 3 hours — enough time for Renovate to create, pass CI, and
merge a PR.

**Verdict:** Rejected. Unacceptable risk for a self-hosted runner.

### Option C: Tiered auto-merge with delay + pinning (chosen)

Three layers of defense:

1. **3-day minimum release age** — community detects most supply chain attacks
   within 24–48h. The delay means Renovate never acts on yanked malicious
   versions. Security fixes (matched via GitHub advisory) bypass the delay.
2. **Immutable references** — Docker digest pinning prevents retroactive tag
   mutation. GitHub Actions SHA pinning prevents force-pushed tags.
3. **Risk-tiered rules** — low-risk updates (compose patches, dev tools,
   SHA-pinned actions) auto-merge. High-risk updates (Dockerfile binaries,
   major versions, Python/uv toolchain) require manual review.

**Verdict:** Chosen. Balances security with maintenance burden.

## Decision

### Minimum release age

All updates wait 3 days before Renovate creates the PR. Security updates
(linked to a GitHub advisory / CVE) skip the delay and auto-merge immediately —
the fix is pre-vetted by the advisory process.

### Immutable pinning

| What | How | Readability impact |
|------|-----|--------------------|
| Docker images | Digest pinning (`grafana:12.4.3@sha256:...`) | Low — tag still visible |
| GitHub Actions | SHA pinning (`actions/checkout@<sha> # v6`) | Medium — comment preserves version |

Renovate manages both — humans never edit digests or SHAs manually.

### Auto-merge tiers

| Category | Auto-merge | Why |
|----------|-----------|-----|
| Security CVE fixes | Yes, 0 delay | Advisory-vetted |
| Docker compose patch/minor | Yes, 3-day delay | Digest-pinned, community vetted |
| mise.toml dev tools | Yes, 3-day delay | CI validates, low blast radius |
| GitHub Actions patch/minor | Yes, 3-day delay | SHA-pinned, immutable |
| Dockerfile binary tools | No | curl'd without checksums |
| Python/uv version bumps | No | Affects entire toolchain |
| Any major update | No | Breaking changes need human review |

### Version grouping

Python (mise.toml + Dockerfile FROM + Dockerfile ARG) and uv (mise.toml +
Dockerfile COPY) are grouped so Renovate creates one PR per logical tool.

### Drift detection

`scripts/check-versions.sh` asserts version consistency between mise.toml and
the Dockerfile. Wired into CI — the build fails if versions diverge.

## References

- Renovate docs: [minimumReleaseAge](https://docs.renovatebot.com/configuration-options/#minimumreleaseage), [pinDigests](https://docs.renovatebot.com/docker/#digest-pinning)
- Axios compromise post-mortem: https://github.com/axios/axios/issues/10636
- GitHub docs: [Pin actions to SHA](https://docs.github.com/en/actions/security-for-github-actions/security-guides/security-hardening-for-github-actions#using-third-party-actions)
