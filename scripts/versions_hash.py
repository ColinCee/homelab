"""Check each changed release hash in ci-runner/versions.env against its download.

Usage: versions_hash.py BASE HEAD. For every tool whose _VERSION or _SHA256
differs between the two commits, downloads the release the CI runner installs
and compares its sha256 with the _SHA256 line. Exits 1 naming each tool whose
hash is wrong, with the right one to paste. CI runs the base commit's copy, so a
pull request cannot change the rule that judges it.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
import urllib.request
from collections.abc import Callable

VERSIONS = "ci-runner/versions.env"
SCRIPT = "scripts/versions_hash.py"
LINE = re.compile(r"([A-Z]+)_(VERSION|SHA256|PYTHON)=(\S+)")
GH = "https://github.com"


def _jdk(v: str) -> str:
    major = re.split(r"[.+]", v, maxsplit=1)[0]
    return (
        f"{GH}/adoptium/temurin{major}-binaries/releases/download/jdk-{v.replace('+', '%2B')}"
        f"/OpenJDK{major}U-jdk_x64_linux_hotspot_{v.replace('+', '_')}.tar.gz"
    )


def pbs_url(tag: str, python: str) -> str:
    """The python-build-standalone asset ci-runner/install_host.py installs; its name carries both
    the Python version and the release tag."""
    return (
        f"{GH}/astral-sh/python-build-standalone/releases/download/{tag}"
        f"/cpython-{python}%2B{tag}-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz"
    )


# The URLs ci-runner/rootfs/Dockerfile and ci-runner/install_host.py download;
# change them together. `v` is the _VERSION value as written in versions.env.
URLS: dict[str, Callable[[str], str]] = {
    "FIRECRACKER": lambda v: (
        f"{GH}/firecracker-microvm/firecracker/releases/download/{v}/firecracker-{v}-x86_64.tgz"
    ),
    "RUNNER": lambda v: (
        f"{GH}/actions/runner/releases/download/{v}/actions-runner-linux-x64-{v.removeprefix('v')}.tar.gz"
    ),
    "NODE": lambda v: (
        f"https://nodejs.org/dist/v{v.removeprefix('v')}/node-v{v.removeprefix('v')}-linux-x64.tar.xz"
    ),
    "UV": lambda v: f"{GH}/astral-sh/uv/releases/download/{v}/uv-x86_64-unknown-linux-gnu.tar.gz",
    "JDK": _jdk,
    "MISE": lambda v: f"{GH}/jdx/mise/releases/download/{v}/mise-{v}-linux-x64",
    "BUN": lambda v: f"{GH}/oven-sh/bun/releases/download/bun-v{v}/bun-linux-x64.zip",
    "PNPM": lambda v: f"{GH}/pnpm/pnpm/releases/download/v{v}/pnpm-linux-x64.tar.gz",
    "DOCKER": lambda v: (
        f"https://download.docker.com/linux/static/stable/x86_64/docker-{v.removeprefix('v')}.tgz"
    ),
    "COMPOSE": lambda v: f"{GH}/docker/compose/releases/download/{v}/docker-compose-linux-x86_64",
    "BUILDX": lambda v: f"{GH}/docker/buildx/releases/download/{v}/buildx-{v}.linux-amd64",
    "GH": lambda v: (
        f"{GH}/cli/cli/releases/download/v{v.removeprefix('v')}"
        f"/gh_{v.removeprefix('v')}_linux_amd64.tar.gz"
    ),
    "GITLEAKS": lambda v: (
        f"{GH}/gitleaks/gitleaks/releases/download/v{v.removeprefix('v')}"
        f"/gitleaks_{v.removeprefix('v')}_linux_x64.tar.gz"
    ),
}


def pins(rev: str) -> dict[str, dict[str, str]]:
    """Each tool's VERSION and SHA256 at a commit; empty if the file is not there."""
    shown = subprocess.run(["git", "show", f"{rev}:{VERSIONS}"], capture_output=True, text=True)
    tools: dict[str, dict[str, str]] = {}
    for line in shown.stdout.splitlines():
        if m := LINE.fullmatch(line.strip()):
            tools.setdefault(m[1], {})[m[2]] = m[3]
    return tools


def sha256_of(url: str) -> str:
    digest = hashlib.sha256()
    with urllib.request.urlopen(url, timeout=60) as response:
        while chunk := response.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def problems(base: str, head: str, fetch: Callable[[str], str] = sha256_of) -> list[str]:
    old, new = pins(base), pins(head)
    found = []
    for tool, pin in new.items():
        if "SHA256" not in pin or pin == old.get(tool):
            continue
        if (tool not in URLS and tool != "PBS") or "VERSION" not in pin:
            found.append(f"{tool}: no download URL known; add it to URLS in {SCRIPT}")
            continue
        if tool == "PBS":
            if "PYTHON" not in pin:
                found.append(f"PBS: PBS_PYTHON is missing from {VERSIONS}")
                continue
            url = pbs_url(pin["VERSION"], pin["PYTHON"])
        else:
            url = URLS[tool](pin["VERSION"])
        try:
            actual = fetch(url)
        except OSError as error:
            found.append(f"{tool}: could not download {url}: {error}")
            continue
        if actual != pin["SHA256"]:
            found.append(f"{tool} {pin['VERSION']}: wrong hash; set {tool}_SHA256={actual}")
        else:
            print(f"{tool} {pin['VERSION']}: hash matches {url}")
    return found


if __name__ == "__main__":
    wrong = problems(sys.argv[1], sys.argv[2])
    for line in wrong:
        print(f"::error file={VERSIONS}::{line}")
    sys.exit(1 if wrong else 0)
