#!/usr/bin/env bash
# Run on Beelink from your own terminal (ssh -t): the password and token stay out of an agent's context.
set -euo pipefail

[[ -t 0 ]] || { echo 'Needs a terminal to hide the password: connect with ssh -t' >&2; exit 1; }

secret="$HOME/code/homelab/secrets/homeassistant_agent_token"
IFS= read -rsp 'agent-control password: ' password
echo

umask 077
tmp=$(mktemp "$secret.XXXXXX")
trap 'rm -f "$tmp"' EXIT

printf %s "$password" | docker exec -i homeassistant python3 -c "$(cat <<'PY'
import asyncio, sys
import aiohttp

BASE = "http://127.0.0.1:8123"
CLIENT = f"{BASE}/"

async def main(password):
    async with aiohttp.ClientSession() as http:
        async def post(path, **kwargs):
            async with http.post(BASE + path, **kwargs) as response:
                body = await response.json(content_type=None)
                if response.status >= 400:
                    raise SystemExit(f"Home Assistant refused {path}: HTTP {response.status}")
                return body

        flow = await post("/auth/login_flow", json={
            "client_id": CLIENT, "handler": ["homeassistant", None], "redirect_uri": CLIENT,
        })
        login = await post(f"/auth/login_flow/{flow['flow_id']}", json={
            "client_id": CLIENT, "username": "agent-control", "password": password,
        })
        if login.get("type") != "create_entry":
            raise SystemExit(f"Login failed: {login.get('errors') or login.get('reason')}")
        session = await post("/auth/token", data={
            "grant_type": "authorization_code", "code": login["result"], "client_id": CLIENT,
        })

        async with http.ws_connect(f"{BASE}/api/websocket", receive_timeout=30) as ws:
            await ws.receive_json()
            await ws.send_json({"type": "auth", "access_token": session["access_token"]})
            if (await ws.receive_json()).get("type") != "auth_ok":
                raise SystemExit("Websocket login failed")
            await ws.send_json({
                "id": 1, "type": "auth/long_lived_access_token",
                "client_name": "claude-code", "lifespan": 3650,
            })
            result = await ws.receive_json()
            if not result.get("success"):
                raise SystemExit(
                    f"Token not created: {result.get('error', {}).get('message')}. "
                    "To replace it, first delete agent-control's claude-code token."
                )
            print(result["result"], end="")

asyncio.run(main(sys.stdin.read()))
PY
)" > "$tmp"

mv "$tmp" "$secret"
echo "Saved to $secret"
