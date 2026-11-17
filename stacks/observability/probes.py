"""Checks Home Assistant, the .internal names and each of the host's DNS servers each minute and
writes the results where Alloy's textfile collector reads them (/var/lib/homelab-metrics), for
the alerts in prometheus/; and when the blog's homelab numbers were last published, a dashboard
number. Stale numbers need no alert of their own: a failing push turns public-stats unhealthy
(compose.yaml), which alerts.

It runs on the host's network: the Beelink's firewall admits only Tailscale and loopback, so a
container on a Docker network cannot reach the services on the host's network."""

import secrets
import socket
import struct
import time
import urllib.error
from pathlib import Path

import telemetry

ADDRESS = "100.100.146.119"
OUT = Path("/metrics/probes.prom")
BLOG_STATS = "https://colincheung.dev/homelab.json"
# The host's resolver, which Docker's forwards each container's lookup to, and its servers.
STUB = "127.0.0.53"
UPSTREAMS = Path("/resolve/resolv.conf")
# libnetwork's extIOTimeout: a slower answer fails the container's lookup.
DOCKER_DNS_TIMEOUT = 4.0
FRESH_ZONE = "colincheung.dev"


def http_status(url: str, host: str | None = None) -> int:
    """The response's status, 200 for any success after redirects, or 0 when nothing answered."""
    try:
        telemetry.http(url, headers={"Host": host} if host else {}, timeout=10, limit=0)
    except urllib.error.HTTPError as error:
        return error.code
    except OSError:
        return 0
    return 200


def ask(name: str, server: str, port: int, timeout: float) -> tuple[bytes, bytes] | None:
    """The A query for `name` and `server`'s reply to it, or None when none came in `timeout`."""
    labels = b"".join(bytes([len(p)]) + p.encode() for p in name.split("."))
    question = struct.pack(">HHHHHH", 0x4B1D, 0x0100, 1, 0, 0, 0) + labels + b"\0\0\1\0\1"
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        try:
            s.sendto(question, (server, port))
            reply = s.recv(512)
        except OSError:
            return None
    return (question, reply) if len(reply) >= 12 and reply[:2] == question[:2] else None


def resolves(name: str, server: str, address: str, port: int = 53, timeout: float = 5) -> bool:
    """Whether `server` answers an A query for `name` with `address`."""
    asked = ask(name, server, port, timeout)
    if asked is None:
        return False
    question, reply = asked
    answers = struct.unpack(">H", reply[6:8])[0]
    return (
        reply[3] & 0x0F == 0
        and answers > 0
        and (socket.inet_aton(address) in reply[len(question) :])
    )


def answers(
    server: str, name: str | None = None, port: int = 53, timeout: float = DOCKER_DNS_TIMEOUT
) -> bool:
    """Whether `server` answers `name` in the time Docker's resolver waits; by default a name no
    cache holds, so the query goes all the way upstream. "No such name" is an answer."""
    name = name or f"probe-{secrets.token_hex(6)}.{FRESH_ZONE}"
    asked = ask(name, server, port, timeout)
    return asked is not None and asked[1][3] & 0x0F in (0, 3)


def upstreams(resolv: Path = UPSTREAMS) -> list[str]:
    """The servers the host's resolver forwards to, the router's and the global ones."""
    try:
        lines = resolv.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [line.split()[1] for line in lines if line.startswith("nameserver ")]


def checks() -> dict[str, bool]:
    return {
        "home-assistant": 200 <= http_status(f"http://{ADDRESS}:8123/") < 400,
        "names-dns": resolves("home.internal", ADDRESS, ADDRESS),
        # Caddy refuses the Beelink's own address with 403 (stacks/internal-names), which still
        # shows it up and serving the site; a name it does not serve gets an empty 200.
        "names-proxy": http_status(f"http://{ADDRESS}/", host="home.internal") == 403,
        # A name that exists: the stub waits for every server before saying a name does not
        # (beelink/dns/homelab-dns.conf), so one lost packet would fail a made-up name's check.
        "dns-stub": answers(STUB, FRESH_ZONE),
        **{f"dns-{server}": answers(server) for server in upstreams()},
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
        lines += [
            "# HELP homelab_public_stats_published_timestamp_seconds The blog numbers' time.",
            f"homelab_public_stats_published_timestamp_seconds {published}",
        ]
    lines += [""]
    telemetry.replace_file(OUT, "\n".join(lines))


def main() -> None:
    while True:
        started = time.time()
        write(checks(), started, published_at())
        time.sleep(max(1.0, 60 - (time.time() - started)))


if __name__ == "__main__":
    main()
