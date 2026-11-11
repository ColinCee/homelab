# Agents

Single Beelink host running Docker Compose stacks. Services: `README.md`; how parts connect:
`docs/architecture.md`; deploys: `docs/runbooks/deploying-services.md`.

Checks: `mise run check` (what CI's check job runs; Compose validation needs Docker). Run it before pushing.

- Keep each service, operational command, and scheduled unit under
  `stacks/<name>/`; shared deployment logic must stay stack-name agnostic.
- Bind admin ports to Tailscale, not all interfaces. Use host networking only
  when required by the service.
- Pin container images and Actions. Renovate may auto-merge non-major updates
  after checks and the release-age gate pass; major updates remain manual.
- Never print or expose secrets. Private operational
  records belong in the separate notes repository, not this public repository.
- Do not add image polling or stack-specific deployment branches. Git owns
  pinned image revisions (moved by Renovate); Compose and stack-owned units own
  application operations.
- Prepare Tailscale policy changes, but leave `scripts/tailscale_policy.py`
  credential entry and live approval to the user's own terminal. Never request
  a Bitwarden vault session or run these prompts through an agent tool.
- Public repositories' CI runs on GitHub-hosted runners; private repositories' CI
  runs only in the `homelab-fc` Firecracker VMs (docs/runbooks/ci-runner.md), which
  hold no secrets, deploy access or home network; only trusted `main` deploys run
  on the deploy runner.
- No unattended agent changes this repo; dependency auto-merge belongs in
  Renovate.
- Change the Beelink's CI runner only by merging to main (docs/runbooks/ci-runner.md, "Changing the Beelink
  while jobs run").
- Update the operational doc a behavior change touches; a decision between designs gets a record in `docs/adr/`.
- Docs may not grow past `docs-size.json` (`mise run docs:size`): cut first; if growth
  is needed, `mise run docs:size -- --update` and say why in the pull request.
