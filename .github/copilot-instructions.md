# Homelab

Single Beelink host running Docker Compose stacks. See `README.md` for services
and `docs/runbooks/deploying-services.md` for deployment.

- Keep the simplest working solution. Do not add generic frameworks, wrappers,
  or instructions for things the code already makes clear.
- Keep each service and its configuration under `stacks/<name>/`.
- Bind admin ports to Tailscale, not all interfaces. Use host networking only
  when required by the service.
- Pin container images and Actions; dependency updates require human merge.
- Never source generated `.env` files or expose secrets. Preserve git-crypt
  protection for `docs/private/`.
- Prepare Tailscale policy changes, but leave `scripts/tailscale_policy.py`
  credential entry and live approval to the user's own terminal. Never request
  a Bitwarden vault session or run these prompts through an agent tool.
- CI runs on GitHub-hosted runners; only trusted `main` deploys run on Beelink.
  Do not reintroduce unattended implementation, review, or merge agents.
- Use the existing `mise` tasks for relevant checks. Keep tests for meaningful
  behavior, not implementation details.
- Update the relevant runbook when operator steps change. Keep facts in one
  place; do not add an ADR for routine changes.
