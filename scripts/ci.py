#!/usr/bin/env python3
"""The workflows' steps (.github/workflows/), one subcommand each, so no workflow holds logic of
its own.

The rules that judge a pull request (pin_only.py, versions_hash.py, the test time limits) are read
from the base commit, so a change to one cannot judge itself. The rest of this file is the pull
request's own, as its workflow is.

Runs on GitHub's runners and in the CI VMs with their system Python: standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path

BASE = "HEAD^1"
LIMITS_FILE = "tests/conftest.py"
LIMIT_NAMES = ("RUN_LIMIT", "TEST_LIMIT")
SLOWER_LABEL = "slower-ci"
VERSIONS = Path("ci-runner/versions.env")
# The key of what the VM's cache disk (/cache) holds (cache_key); in a folder, as fc-cache packs
# only folders.
CACHE_KEY = Path("/cache/mise/.ci-key")


def git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def base_file(path: str) -> str | None:
    """The file as the base commit has it, None when it has none."""
    try:
        return git("show", f"{BASE}:{path}")
    except subprocess.CalledProcessError:
        return None


def output(name: str, value: str) -> None:
    """A step output, for a later step's `if`."""
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
        f.write(f"{name}={value}\n")


def run_base_rule(script: str) -> int | None:
    """Run the base commit's copy of a rule script on BASE..HEAD; None when the base has none."""
    text = base_file(f"scripts/{script}")
    if text is None:
        return None
    with tempfile.TemporaryDirectory() as folder:
        copy = Path(folder) / script
        copy.write_text(text, encoding="utf-8")
        return subprocess.run([sys.executable, str(copy), BASE, "HEAD"]).returncode


def pins_only() -> bool:
    """Whether the pull request only moves image pins (pin_only.py): then only Compose runs. No
    rule in the base means full checks."""
    only = run_base_rule("pin_only.py") == 0
    if only:
        output("only", "true")
    return only


def changed(paths: Sequence[str]) -> list[str]:
    return [p for p in git("diff", "--name-only", BASE, "HEAD", "--", *paths).split("\n") if p]


def versions_hash() -> int:
    """A changed release in versions.env against its hash (versions_hash.py), before merge."""
    return (run_base_rule("versions_hash.py") or 0) if changed([str(VERSIONS)]) else 0


def validate_changed_compose() -> int:
    files = [p for p in changed([]) if p.endswith("compose.yaml")]
    return subprocess.run(["scripts/validate-compose.sh", *files]).returncode if files else 0


def limits(text: str) -> dict[str, float]:
    found = re.findall(rf"^({'|'.join(LIMIT_NAMES)}) = ([0-9.]+)$", text, re.MULTILINE)
    return {name: float(value) for name, value in found}


def raised_limits(base: str | None, head: str) -> list[str]:
    """Each test time limit the head raised or removed against the base's."""
    if base is None:
        return []
    old, new = limits(base), limits(head)
    return [
        f"{name} {old[name]:g} s to {f'{new[name]:g} s' if name in new else 'none'}"
        for name in old
        if new.get(name, float("inf")) > old[name]
    ]


def labels(event: Mapping[str, object]) -> list[str]:
    """The pull request's label names, from the event file; [] for any other shape."""
    pull = event.get("pull_request")
    found = pull.get("labels") if isinstance(pull, dict) else None
    return [str(label.get("name")) for label in found or [] if isinstance(label, dict)]


def time_limits() -> int:
    """A raised or removed test time limit needs the slower-ci label, with the reason in the pull
    request: a slower CI is a decision."""
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    if SLOWER_LABEL in labels(event):
        return 0
    raised = raised_limits(base_file(LIMITS_FILE), Path(LIMITS_FILE).read_text(encoding="utf-8"))
    for change in raised:
        print(f"::error::A test time limit went up: {change}. Make the tests faster, or add the "
              f"{SLOWER_LABEL} label and say why in the pull request.")  # fmt: skip
    return 1 if raised else 0


def prepare() -> int:
    """A pull request's rules, then a VM's tools, in one step: each runner step costs about a
    fifth of a second. Every rule runs before the verdict; a pin-only change needs no tools."""
    failed = only = False
    if os.environ.get("GITHUB_EVENT_NAME") == "pull_request":
        only = pins_only()
        failed = any([versions_hash(), time_limits()])
    if os.environ.get("RUNNER_ENVIRONMENT") == "self-hosted" and not only:
        failed = bool(vm_tools()) or failed
    return 1 if failed else 0


def vm_tools() -> int:
    """Install mise.toml's tools onto the VM's cache disk, and point later steps at them."""
    env = {
        "MISE_DATA_DIR": "/cache/mise",
        "MISE_YES": "1",
        "MISE_TRUSTED_CONFIG_PATHS": os.environ["GITHUB_WORKSPACE"],
    }
    with open(os.environ["GITHUB_ENV"], "a", encoding="utf-8") as f:
        f.writelines(f"{k}={v}\n" for k, v in env.items())
    return subprocess.run(["mise", "install"], env=os.environ | env).returncode


def annotation(text: str) -> str:
    """A workflow command's message: one line, with % escaped."""
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def slowest_tests() -> int:
    """Post the slowest tests (tests/conftest.py) as an annotation, for scripts/ci_times.py."""
    times = Path(os.environ["RUNNER_TEMP"]) / "test-times.json"
    if times.exists():
        print(f"::notice title=slowest tests::{annotation(times.read_text(encoding='utf-8'))}")
    return 0


def cache_key(root: Path = Path()) -> str:
    """mise.toml's tools, settings and env, and uv.lock: its tasks change often and install
    nothing."""
    mise = tomllib.loads((root / "mise.toml").read_text(encoding="utf-8"))
    installs = {name: mise.get(name, {}) for name in ("tools", "settings", "env")}
    digest = hashlib.sha256(json.dumps(installs, sort_keys=True).encode())
    digest.update((root / "uv.lock").read_bytes())
    return digest.hexdigest()[:16]


def cache_changed() -> int:
    """Whether the VM's cache disk would change: saving it takes about 14 s, so only then."""
    key = cache_key()
    if not CACHE_KEY.exists() or CACHE_KEY.read_text(encoding="utf-8").strip() != key:
        CACHE_KEY.parent.mkdir(parents=True, exist_ok=True)
        CACHE_KEY.write_text(key + "\n", encoding="utf-8")
        output("changed", "true")
    return 0


def versions(text: str) -> dict[str, str]:
    """versions.env's NAME=value lines, as the shell reads them (quoted values may span lines)."""
    found: dict[str, str] = {}
    for token in shlex.split(text, comments=True):
        name, _, value = token.partition("=")
        if re.fullmatch(r"[A-Z0-9_]+", name):
            found[name] = value
    return found


def build_image() -> int:
    """Build the CI VMs' image as ci-runner/build-rootfs.sh does, to catch a broken one before
    merge: the Beelink builds it only after."""
    pinned = versions(VERSIONS.read_text(encoding="utf-8"))
    command = ["docker", "build", "-q", "--build-context", "scripts=scripts"]
    for name, value in pinned.items():
        command += ["--build-arg", f"{name}={value}"]
    return subprocess.run([*command, "ci-runner/rootfs"], stdout=subprocess.DEVNULL).returncode


def ghcr_login() -> int:
    """Log Docker in to GHCR (deploy.yaml) in the job's temp folder, which the runner deletes
    after the job, so no registry credential stays on the host."""
    config = Path(os.environ["RUNNER_TEMP"]) / "docker"
    config.mkdir(parents=True, exist_ok=True)
    with open(os.environ["GITHUB_ENV"], "a", encoding="utf-8") as f:
        f.write(f"DOCKER_CONFIG={config}\n")
    command = ["docker", "login", "ghcr.io", "-u", os.environ["GHCR_USER"], "--password-stdin"]
    env = os.environ | {"DOCKER_CONFIG": str(config)}
    return subprocess.run(command, input=os.environ["GHCR_TOKEN"], text=True, env=env).returncode


STEPS = {
    "ghcr-login": ghcr_login,
    "prepare": prepare,
    "validate-changed-compose": validate_changed_compose,
    "slowest-tests": slowest_tests,
    "cache-changed": cache_changed,
    "build-image": build_image,
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("step", choices=STEPS)
    return STEPS[parser.parse_args(argv).step]()


if __name__ == "__main__":
    sys.exit(main())
