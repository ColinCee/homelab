"""Checks Home Assistant and the .internal names each minute and writes the results where Alloy's
textfile collector reads them (/var/lib/homelab-metrics), for the alerts in prometheus/rules.yml;
and when the blog's homelab numbers were last published, a dashboard number. Stale numbers need no
alert of their own: a failing push turns public-stats unhealthy (compose.yaml), which alerts.

It runs on the host's network: the Beelink's firewall admits only Tailscale and loopback, so a
container on a Docker network cannot reach the services on the host's network."""

import socket
import struct
import time
import urllib.error
from pathlib import Path

import telemetry

ADDRESS = "100.100.146.119"
OUT = Path("/metrics/probes.prom")
BLOG_STATS = "https://colincheung.dev/homelab.json"


def http_status(url: str, host: str | None = None) -> int:
    """The response's status, 200 for any success after redirects, or 0 when nothing answered."""
    try:
        telemetry.http(url, headers={"Host": host} if host else {}, timeout=10, limit=0)
    except urllib.error.HTTPError as error:
        return error.code
    except OSError:
        return 0
    return 200


def resolves(name: str, server: str, address: str, port: int = 53, timeout: float = 5) -> bool:
    """Whether `server` answers an A query for `name` with `address`."""
    labels = b"".join(bytes([len(p)]) + p.encode() for p in name.split("."))
    question = struct.pack(">HHHHHH", 0x4B1D, 0x0100, 1, 0, 0, 0) + labels + b"\0\0\1\0\1"
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        try:
            s.sendto(question, (server, port))
            reply = s.recv(512)
        except OSError:
            return False
    answers = struct.unpack(">H", reply[6:8])[0] if len(reply) >= 12 else 0
    return (
        reply[:2] == question[:2]
        and reply[3] & 0x0F == 0
        and answers > 0
        and (socket.inet_aton(address) in reply[len(question) :])
    )


def checks() -> dict[str, bool]:
    return {
        "home-assistant": 200 <= http_status(f"http://{ADDRESS}:8123/") < 400,
        "names-dns": resolves("home.internal", ADDRESS, ADDRESS),
        # Caddy refuses the Beelink's own address with 403 (stacks/internal-names), which still
        # shows it up and serving the site; a name it does not serve gets an empty 200.
        "names-proxy": http_status(f"http://{ADDRESS}/", host="home.internal") == 403,
    }


def published_at(url: str = BLOG_STATS) -> int | None:
    """The `at` of the numbers the blog serves (public_stats.py), or None when unreadable."""
    try:
        at = telemetry.http_json(url, timeout=10).get("at")
    except OSError, ValueError, AttributeError:
        return None
    return at if isinstance(at, int) and not isinstance(at, bool) else None


def write(results: dict[str, bool], now: float, published: int | None = None) -> None:
    lines = [
        "# HELP homelab_probe_success 1 when the check passed (stacks/observability/probes.py)."
    ]
    lines += [f'homelab_probe_success{{target="{t}"}} {int(ok)}' for t, ok in results.items()]
    lines += [f"homelab_probe_timestamp_seconds {now:.0f}"]
    if published is not None:
        lines += [f"homelab_public_stats_published_timestamp_seconds {published}"]
    lines += [""]
    telemetry.replace_file(OUT, "\n".join(lines))


def main() -> None:
    while True:
        started = time.time()
        write(checks(), started, published_at())
        time.sleep(max(1.0, 60 - (time.time() - started)))


if __name__ == "__main__":
    main()
