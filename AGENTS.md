# Agents

Single Beelink host running Docker Compose stacks. See `README.md` for services
and how to run them locally, `docs/` for architecture and runbooks, and
`docs/runbooks/deploying-services.md` for deployment.

Checks: `mise run check`

That runs what CI's check job runs: lint, typecheck, test, docs:size and validate:compose
(Compose validation needs Docker). Run it before pushing.

- Keep the simplest working solution. Do not add generic frameworks, wrappers,
  or instructions for things the code already makes clear.
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
- Change the Beelink's CI runner only by merging to main: it takes the commit and job image within a minute or
  two, and running jobs carry on (docs/runbooks/ci-runner.md, "Changing the Beelink
  while jobs run"; a new host's first install is by hand there).
  Run any wait on the Beelink as a named systemd unit, never as a loop tied to your own
  connection.
- Use the existing `mise` tasks for relevant checks. Keep tests for meaningful
  behavior, not implementation details.
- Update the relevant operational doc when behavior changes. Keep necessary
  rationale beside its instructions or code; a decision between designs gets a
  record in `docs/adr/` (the recording-learnings skill).
  Delete obsolete guidance and rewrite a decision record in place; archive only
  for a concrete retention need.
  Track future work in issues rather than another roadmap.
- Docs may not grow past `docs-size.json` (`mise run docs:size`): cut first; if growth
  is needed, `mise run docs:size -- --update` and say why in the pull request.
