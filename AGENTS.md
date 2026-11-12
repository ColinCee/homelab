# Agents

Single Beelink host running Docker Compose stacks. How parts connect and stack conventions: `docs/architecture.md`,
which code comments point to by section.

Checks: `mise run check` (CI's check job; Compose validation needs Docker), before pushing.

- Keep each service, operational command and scheduled unit under `stacks/<name>/`; shared deployment logic stays
  stack-name agnostic. No image polling or per-stack deploy branches: Git pins image revisions; Renovate moves
  them, auto-merging non-major ones after checks and its release-age gate.
- Bind admin ports to Tailscale; host networking only when needed. Pin images, Actions.
- Logic is typed, tested Python; shell only calls one program. A workflow step is one such command (`scripts/ci.py`),
  YAML only `runs-on` and `if:`.
- Never print or expose secrets. Private operational records belong in the notes repository, not this public one.
- Prepare Tailscale policy changes, but leave `scripts/tailscale_policy.py` credential entry and live approval to the
  user's own terminal. Never request a Bitwarden vault session or run these prompts through an agent tool.
- The `homelab-fc` Firecracker VMs (no secrets, deploy access or home network) run only Colin's jobs; others' run on
  GitHub's runners. Change the Beelink's CI runner only by merging to main.
- Unattended agents (the `homelab-oncall` check) may open pull requests, never merge; Renovate alone
  auto-merges.
- Docs are `README.md`, `docs/architecture.md`, `docs/recovery.md` and `docs/adr/` (only comparisons run),
  capped by `docs-size.json` (`mise run docs:size`): cut first, or say in the pull request why it grows.
