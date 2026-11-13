"""Makes Grafana's `agents` login a Viewer with the agents password, then exits. Grafana has no
file provisioning for users, and its admin login is set by compose.yaml (grafana_admin_password).
Reads GRAFANA_ADMIN_USER and the secret files grafana_admin_password and agents_password."""

import logging
import os
import time
import urllib.error
from typing import Any

import telemetry

URL = "http://grafana:3000"
# Grafana redirects any other name to its own (compose.yaml, GF_SERVER_ENFORCE_DOMAIN).
HEADERS = {"Host": "grafana.internal"}
LOGIN = "agents"
log = logging.getLogger("grafana-users")


def call(method: str, path: str, body: object = None, auth: str = "") -> Any:  # noqa: ANN401 - Grafana's JSON
    admin = os.environ["GRAFANA_ADMIN_USER"]
    auth = auth or f"{admin}:{telemetry.secret('grafana_admin_password')}"
    return telemetry.http_json(URL + path, method=method, body=body, headers=HEADERS, login=auth)


def wait_until_ready(timeout: float = 90) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            telemetry.http(URL + "/api/health", headers=HEADERS, timeout=5)
            return
        except OSError:
            if time.monotonic() > deadline:
                raise SystemExit(f"Grafana did not answer in {timeout:.0f} s") from None
            time.sleep(2)


def main() -> None:
    password = telemetry.secret("agents_password")
    if not password:
        raise SystemExit("agents_password is empty")
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
        log.info("created the agents login")
    # A changed secret or a hand-set role is put back. Grafana refuses to set the password it has.
    try:
        call("GET", "/api/user", auth=f"{LOGIN}:{password}")
    except urllib.error.HTTPError as error:
        if error.code != 401:
            raise
        call("PUT", f"/api/admin/users/{user['id']}/password", {"password": password})
    call("PATCH", f"/api/org/users/{user['id']}", {"role": "Viewer"})
    log.info("the agents login is a Viewer with the current password")


if __name__ == "__main__":
    telemetry.start()
    main()
