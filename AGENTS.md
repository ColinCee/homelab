# Agents

Single Beelink host running Docker Compose stacks. How the parts connect, and the conventions for a stack:
`docs/architecture.md`, which code comments point to by section.

Checks: `mise run check` (what CI's check job runs; Compose validation needs Docker). Run it before pushing.

- Keep each service, operational command and scheduled unit under `stacks/<name>/`; shared deployment logic stays
  stack-name agnostic. No image polling or stack-specific deploy branches: Git owns pinned image revisions, moved by
  Renovate, which may auto-merge non-major updates after checks and its release-age gate.
- Bind admin ports to Tailscale; host networking only when the service needs it. Pin images and Actions.
- Never print or expose secrets. Private operational records belong in the notes repository, not this public one.
- Prepare Tailscale policy changes, but leave `scripts/tailscale_policy.py` credential entry and live approval to the
  user's own terminal. Never request a Bitwarden vault session or run these prompts through an agent tool.
- The `homelab-fc` Firecracker VMs (no secrets, deploy access or home network) run only Colin's jobs; others' run on
  GitHub's runners. Change the Beelink's CI runner only by merging to main.
- No unattended agent changes this repo; dependency auto-merge belongs in Renovate.
- Docs are `README.md`, `docs/architecture.md`, `docs/recovery.md` and `docs/adr/` (comparisons that were run, only);
  they may not grow past `docs-size.json` (`mise run docs:size`): cut first, and say in the pull request why any growth
  is needed.
