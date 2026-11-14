# Secrets

Every secret the homelab uses. How they reach containers: [architecture](architecture.md); restoring them:
[recovery](recovery.md).

Names are public; values live in Bitwarden's "Homelab secrets".
[`checks/secrets_listed_test.py`](../checks/secrets_listed_test.py) keeps this table matched to the compose files.

| `secrets/` file | Used by | Gives |
| --- | --- | --- |
| `agents_password` | observability | Agents' read-only gateway login |
| `cf_tunnel_token` | tunnel | Runs the Cloudflare Tunnel |
| `grafana_admin_password` | observability | Grafana's admin |
| `grafana_secret_key` | observability | Grafana's signing key |
| `healthchecks_ping_url` | observability | Incidents relay's dead-man ping |
| `homeassistant_metrics_token` | observability | Reads Home Assistant's metrics |
| `incidents_discord_bot_token` | observability | The Homelab Discord bot |
| `oncall_fire_token` | observability | Starts the on-call routine |
| `public_stats_key` | observability | Signs the public stats page |
| `r2_access_key_id` | backup | R2 login, with the next two |
| `r2_account_id` | backup | |
| `r2_secret_access_key` | backup | |
| `restic_password` | backup | Decrypts every backup |
| `stats_password` | observability | Public stats' gateway login |

Elsewhere: the CI runner's `/etc/homelab-ci/github-token`, and the claude.ai Homelab environment's network secrets
for `observe.colincheung.dev` (the gateway login and a Cloudflare Access service token) and `discord.com` (the bot token).
