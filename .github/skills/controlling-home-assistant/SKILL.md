---
name: controlling-home-assistant
description: "Use when reading or changing anything Home Assistant runs at home: heating, boosts, lights, switches, scenes or sensors."
---

# Controlling Home Assistant

Agents call its API on Beelink as the non-admin `agent-control` user. Never print the token.

1. Find the entity and its options:

   ```bash
   ssh -o ConnectTimeout=10 beelink 'printf "Authorization: Bearer %s\n" "$(cat ~/code/homelab/secrets/homeassistant_agent_token)" | curl -fsS --max-time 30 -H @- http://127.0.0.1:8123/api/states | jq -c ".[] | {entity_id, state, name: .attributes.friendly_name, presets: .attributes.preset_modes}"'
   ```

2. Call the action. A heating boost is `climate/set_preset_mode` with `preset_mode` `boost_1h`:

   ```bash
   ssh -o ConnectTimeout=10 beelink 'printf "Authorization: Bearer %s\n" "$(cat ~/code/homelab/secrets/homeassistant_agent_token)" | curl -fsS --max-time 30 -H @- -H "Content-Type: application/json" -d "{\"entity_id\":\"<entity>\",\"preset_mode\":\"boost_1h\"}" http://127.0.0.1:8123/api/services/climate/set_preset_mode'
   ```

3. Read `/api/states/<entity>` and report what it shows. After a timeout, read it before retrying: the action may still finish.

Admin changes (users, integrations) wait for Colin. To replace the token, he deletes `agent-control`'s `claude-code` token, then runs `stacks/home-assistant/create-agent-token.sh` on Beelink with `ssh -t`.
