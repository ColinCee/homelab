---
name: homelab-network
description: "Use when reaching a homelab device or service over Tailscale (Beelink, OpenObserve, Home Assistant, the desktop's pages), saving its address in code or docs, adding an .internal name, or changing who may reach what."
---

# Homelab network

What to type and what to save for each Beelink service. Device addresses, the
desktop's pages, who may reach what, and how a policy change is applied are
private: read them in Colin's notes repo (`ColinCee/notes`, checked out as `notes`
beside the code repos; pull first), `04-dev/homelab/network.md` and
`tailscale-policy.md`. No checkout: `gh api repos/ColinCee/notes/contents/<path> -H 'Accept: application/vnd.github.raw'`.

| Service | Type | Save in code, docs and dashboards |
|---|---|---|
| OpenObserve | `http://openobserve.internal` | `http://beelink.tail4f465e.ts.net:5080` |
| Home Assistant | `http://home.internal` | `http://beelink.tail4f465e.ts.net:8123` |
| Beelink shell | `ssh beelink` | `beelink.tail4f465e.ts.net` |

- Type the short forms; save the full `*.ts.net` form. Short names fail on Beelink
  itself: its `/etc/hosts` maps `beelink` to `127.0.1.1`, and its containers resolve
  it to themselves or to loopback. Docker port bindings and Alloy scrape targets
  stay IPs because they bind or dial an address.
- `.internal` names come from `stacks/internal-names` in the homelab repo. A new one
  needs a site in its Caddyfile, the name in its Corefile, a row here, and a tailnet
  policy grant if a new device should reach it.
