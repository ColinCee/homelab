# Agents

Single Beelink host running Docker Compose stacks. How parts connect and stack conventions: `docs/architecture.md`,
which code comments point to by section.

Checks: `mise run check` (CI's job) before pushing. Linux-only tests: `linux_only`.

- Layout: `checks/placement_test.py`. Deploy logic stays stack-name agnostic. No image polling or per-stack deploy
  branches: Git pins images; Renovate moves them, auto-merging non-major ones after checks and its release-age gate.
- Bind admin ports to Tailscale; host networking only when needed. Pin images, Actions.
- Logic is Python. A workflow step is one command (`checks/ci.py`), YAML only `runs-on` and `if:`.
- Secrets: `docs/secrets.md`.
- Prepare Tailscale policy changes, but leave `tailnet/tailscale_policy.py` credential entry and live approval to the
  user's own terminal, never an agent tool.
- The `homelab-fc` Firecracker VMs (no secrets, deploy access or home network) run only Colin's jobs; others' run on
  GitHub's runners. Change the Beelink's CI runner only by merging to main.
- Unattended agents open pull requests; only Renovate and the on-call agent (`checks/agent_scope.py`) merge.
- Docs are `README.md`, `docs/*.md` and `docs/adr/` (only comparisons run), capped by `docs-size.json`: cut first,
  or say in the pull request why it grows.
