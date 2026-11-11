"""Makes Grafana's `agents` login a Viewer with AGENTS_PASSWORD, then exits. Grafana has no file
provisioning for users, and its admin login is set by compose.yaml (GRAFANA_ADMIN_PASSWORD).
Reads GRAFANA_URL, GRAFANA_ADMIN_USER, GRAFANA_ADMIN_PASSWORD and AGENTS_PASSWORD."""

import base64
import json
import os
import time
import urllib.error
import urllib.request

URL = os.environ.get("GRAFANA_URL", "http://grafana:3000")
# Grafana redirects any other name to its own (compose.yaml, GF_SERVER_ENFORCE_DOMAIN).
HOST = os.environ.get("GRAFANA_DOMAIN", "beelink.tail4f465e.ts.net")
LOGIN = "agents"


def call(method: str, path: str, body: object = None, auth: str = "") -> object:
    auth = auth or f"{os.environ['GRAFANA_ADMIN_USER']}:{os.environ['GRAFANA_ADMIN_PASSWORD']}"
    request = urllib.request.Request(
        URL + path,
        data=None if body is None else json.dumps(body).encode(),
        method=method,
        headers={
            "Authorization": "Basic " + base64.b64encode(auth.encode()).decode(),
            "Content-Type": "application/json",
            "Host": HOST,
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        text = response.read().decode()
    return json.loads(text) if text else None


def wait_until_ready(timeout: float = 90) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            health = urllib.request.Request(URL + "/api/health", headers={"Host": HOST})
            with urllib.request.urlopen(health, timeout=5):
                return
        except OSError:
            if time.monotonic() > deadline:
                raise SystemExit(f"Grafana did not answer in {timeout:.0f} s") from None
            time.sleep(2)


def main() -> None:
    password = os.environ["AGENTS_PASSWORD"]
    if not password:
        raise SystemExit("AGENTS_PASSWORD is empty")
    wait_until_ready()
    try:
        user = call("GET", f"/api/users/lookup?loginOrEmail={LOGIN}")
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        email = f"{LOGIN}@homelab.internal"
        body = {"name": LOGIN, "login": LOGIN, "email": email, "password": password}
        call("POST", "/api/admin/users", body)
        user = call("GET", f"/api/users/lookup?loginOrEmail={LOGIN}")
        print("created the agents login", flush=True)
    assert isinstance(user, dict)
    # A changed secret or a hand-set role is put back. Grafana refuses to set the password it has.
    try:
        call("GET", "/api/user", auth=f"{LOGIN}:{password}")
    except urllib.error.HTTPError as error:
        if error.code != 401:
            raise
        call("PUT", f"/api/admin/users/{user['id']}/password", {"password": password})
    call("PATCH", f"/api/org/users/{user['id']}", {"role": "Viewer"})
    print("the agents login is a Viewer with the current password", flush=True)


if __name__ == "__main__":
    main()
