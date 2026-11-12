---
name: reaching-homelab-devices
description: "Use when reaching a homelab device or service over Tailscale (Beelink, Grafana, Home Assistant, the desktop's pages), saving its address in code or docs, adding an .internal name, or changing who may reach what."
---

# Homelab network

What to type and save for each Beelink service. Addresses work only on Colin's
tailnet, so they may go in public code. Other devices, the desktop's pages, who may
reach what, and policy changes: Colin's notes repo (`ColinCee/notes`, checked out as
`notes` beside the code repos; pull first), `04-dev/homelab/network.md` and
`tailscale-policy.md`. No checkout: `gh api repos/ColinCee/notes/contents/<path> -H 'Accept: application/vnd.github.raw'`.

| Service | Type | Save in code, docs, dashboards |
|---|---|---|
| Grafana | `http://grafana.internal` | `http://grafana.internal` |
| Home Assistant | `http://home.internal` | `http://beelink.tail4f465e.ts.net:8123` |
| Beelink shell | `ssh beelink` | `beelink.tail4f465e.ts.net` |

- Type the short forms; save the `*.ts.net` form, but Grafana's `.internal`. Short
  names fail on Beelink: `/etc/hosts` maps `beelink` to `127.0.1.1`, containers resolve
  it to themselves or loopback, and `.internal` sites refuse it. Docker port bindings
  and Alloy scrape targets stay IPs (Beelink's: `100.100.146.119`).
- `.internal` names come from `stacks/internal-names` in the homelab repo. A new one
  needs a site in its Caddyfile, the name in its Corefile, a row here, and a tailnet
  policy grant if a new device should reach it.
