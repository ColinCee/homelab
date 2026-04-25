# ADR-017: Deploy Key for Knowledge Save

**Date:** 2026-04-20
**Status:** Accepted

## Context

The `knowledge save` command runs in a container and needs to push commits to `git@github.com:ColinCee/notes.git`. The container needs SSH credentials to authenticate with GitHub.

The initial approach mounted the entire host `~/.ssh/` directory into the container. This exposes all private keys (personal, server, other services) to anything running inside the container — unnecessarily broad access for a single-repo push.

## Options Considered

### Deploy key (SSH, repo-scoped)

A dedicated ed25519 key added as a GitHub deploy key with write access on the notes repo. Only the single private key file is mounted into the container.

- **Pros:** Minimal blast radius (one key = one repo), no runtime dependencies, SSH remote URL works unchanged
- **Cons:** No built-in expiry, manual setup and rotation, write deploy keys can push destructive ref updates unless the target repo blocks them
- **Verdict:** Chosen — right level of scoping for a single-repo push from a homelab container.

### GitHub App token (HTTPS)

Reuse the existing `colins-homelab-bot` App to generate short-lived installation tokens, push over HTTPS.

- **Pros:** Short-lived tokens, fine-grained permissions, existing infrastructure
- **Cons:** Requires changing git remote to HTTPS (or overriding push URL), adds token generation code, still mounts a private key (the App PEM), new failure mode if token API is unreachable
- **Verdict:** Rejected — over-engineered for a manual, infrequent, single-repo operation.

### Mount all host SSH keys

Mount `~/.ssh/` read-only into the container.

- **Pros:** Zero setup, works immediately
- **Cons:** Exposes all host private keys to the container, violates least-privilege
- **Verdict:** Rejected — unnecessary risk even on a single-node homelab.

### Personal Access Token (HTTPS)

Use a PAT as an HTTPS credential.

- **Pros:** Simple
- **Cons:** Broad permissions (user-scoped, not repo-scoped), requires manual rotation, same remote URL issue as App tokens
- **Verdict:** Rejected — less scoped than a deploy key with more maintenance burden.

## Decision

Use a dedicated deploy key (`~/.ssh/notes_deploy_key`) with write access scoped to `ColinCee/notes`. The compose service mounts only this key file read-only. `known_hosts` is baked into the image during build, and `/etc/passwd` is mounted read-only so the host uid resolves to a user for SSH.

Pattern for future stacks needing git push access: generate a per-repo deploy key rather than sharing host credentials.

## Accepted Residual Risks

The deploy key is a long-lived static credential. If it is copied from the host
or exposed through a container escape, GitHub will not time-bound the credential
the way it does for GitHub App installation tokens.

The risk is acceptable for this homelab because:

1. The key is scoped to the notes repo instead of all host SSH identities.
2. The key is mounted read-only and only into the `save` compose profile.
3. The save command only performs a normal `git push origin main`.
4. `ColinCee/notes` is the data target for this workflow; the homelab repo and
   agent credentials are not exposed by this key.

Branch protection or repository rules on `ColinCee/notes` would be a stronger
server-side mitigation for force-push and branch deletion. If those controls are
not available for the private notes repo, the fallback control is fast key
revocation plus restoring from git history or backups.

## Operational Mitigations

The deploy key needs lifecycle management because GitHub deploy keys do not
expire automatically:

1. Rotate `~/.ssh/notes_deploy_key` after any suspected host/container
   compromise, accidental key disclosure, or migration to a new host.
2. Review GitHub deploy-key metadata during routine maintenance; `last_used_at`
   is sufficient for a manual sanity check at this scale.
3. Rebuild the knowledge image if GitHub rotates SSH host keys, because
   `known_hosts` is generated at image build time.
4. Reconsider GitHub App tokens if this grows beyond a single repo/key or if
   automated deploy-key monitoring becomes necessary.

The copy-paste procedures live in
[`docs/runbooks/knowledge-base.md`](../runbooks/knowledge-base.md).

## References

- [GitHub docs: Deploy keys](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/managing-deploy-keys#deploy-keys)
- `stacks/knowledge/compose.yaml` — save service volume mounts
- `stacks/knowledge/app/Dockerfile` — openssh-client installation
