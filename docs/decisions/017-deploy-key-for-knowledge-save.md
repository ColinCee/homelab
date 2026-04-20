# ADR-017: Deploy Key for Knowledge Save

**Date:** 2026-04-20
**Status:** Accepted

## Context

The `knowledge save` command runs in a container and needs to push commits to `git@github.com:ColinCee/notes.git`. The container needs SSH credentials to authenticate with GitHub.

The initial approach mounted the entire host `~/.ssh/` directory into the container. This exposes all private keys (personal, server, other services) to anything running inside the container — unnecessarily broad access for a single-repo push.

## Options Considered

### Deploy key (SSH, repo-scoped)

A dedicated ed25519 key added as a GitHub deploy key with write access on the notes repo. Only the single private key file is mounted into the container.

- **Pros:** Minimal blast radius (one key = one repo), no expiry, no runtime dependencies, SSH remote URL works unchanged
- **Cons:** Manual one-time setup (key generation + adding to GitHub)
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

Use a dedicated deploy key (`~/.ssh/notes_deploy_key`) with write access scoped to `ColinCee/notes`. The compose service mounts only this key file and `known_hosts` (both read-only). `HOME=/home/user` is set so SSH finds the key without needing `/etc/passwd`.

Pattern for future stacks needing git push access: generate a per-repo deploy key rather than sharing host credentials.

## References

- [GitHub docs: Deploy keys](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/managing-deploy-keys#deploy-keys)
- `stacks/knowledge/compose.yaml` — save service volume mounts
- `stacks/knowledge/app/Dockerfile` — openssh-client installation
