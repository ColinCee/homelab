# Homelab

Single Beelink host running Docker Compose stacks. See `README.md` for services
and `docs/runbooks/deploying-services.md` for deployment.

- Keep the simplest working solution. Do not add generic frameworks, wrappers,
  or instructions for things the code already makes clear.
- Keep each service and its configuration under `stacks/<name>/`.
- Bind admin ports to Tailscale, not all interfaces. Use host networking only
  when required by the service.
- Pin container images and Actions. Renovate may auto-merge non-major updates
  after checks and the release-age gate pass; major updates remain manual.
- Never source generated `.env` files or expose secrets. Private operational
  records belong in the separate notes repository, not this public repository.
- Prepare Tailscale policy changes, but leave `scripts/tailscale_policy.py`
  credential entry and live approval to the user's own terminal. Never request
  a Bitwarden vault session or run these prompts through an agent tool.
- CI runs on GitHub-hosted runners; only trusted `main` deploys run on Beelink.
  Do not reintroduce custom unattended implementation, review, or merge agents;
  dependency auto-merge belongs in Renovate.
- Use the existing `mise` tasks for relevant checks. Keep tests for meaningful
  behavior, not implementation details.
- Update the relevant operational doc when behavior changes. Keep necessary
  rationale beside its instructions or code, not in ADRs or a decision log.
  Delete obsolete guidance; archive only for a concrete retention need.
  Track future work in issues rather than another roadmap.
