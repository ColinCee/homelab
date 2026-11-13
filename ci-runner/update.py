#!/usr/bin/env python3
"""Bring the Beelink's CI to one commit of homelab's main:

    sudo ci-runner/update.sh --follow     (its timer, every minute: takes main)
    sudo ci-runner/update.sh <commit>     (by hand)

Code comes from git itself, never a tarball: the commit must be on main and not older than the one
installed. A changed image is rebuilt. Runs as its own unit (homelab-ci-update): installing
restarts the services that would otherwise run it. Installed beside update.sh, alone: standard
library only.
"""

from __future__ import annotations

import fcntl
import os
import re
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

REPO = "https://github.com/ColinCee/homelab.git"
SRC = "/var/lib/homelab-ci/src"
IMAGES = "/var/lib/homelab-ci/images"
INSTALLED = "/etc/homelab-ci/commit"
FAILED = "/var/lib/homelab-ci/update-failed"
PINNED = "/etc/homelab-ci/pinned"
LOCK = "/run/homelab-ci-update.lock"
# The units run homelab-python3 (install_host.py, PYTHON_LINK); this file stays on /usr/bin/python3,
# so a broken pinned Python cannot stop the updater that would repair it.
SUPERVISOR = ["/usr/local/bin/homelab-python3", "-Es", "/usr/local/lib/homelab-ci/ci_supervisor.py"]
USAGE = "update.sh: usage: update.sh <commit> [reason] | --follow"
# What install-host.sh installs, and what goes into the image; tests sit beside the code they test
# but are neither.
NOT_TESTS = ":(glob,exclude)**/*_test.py"
INSTALL_PATHS = ["beelink/security", "beelink/ssd-wear", "ci-runner", "stacks/telemetry", NOT_TESTS]
IMAGE_INPUTS = [
    "ci-runner/build-rootfs.sh",
    "ci-runner/rootfs",
    "ci-runner/versions.env",
    NOT_TESTS,
]
GIVE_UP_FAILURES = 2
RETRY_SECONDS = 3600


class Run(Protocol):
    def __call__(
        self,
        command: list[str],
        *,
        check: bool = True,
        capture: bool = False,
        quiet: bool = False,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]: ...


def run_command(
    command: list[str],
    *,
    check: bool = True,
    capture: bool = False,
    quiet: bool = False,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command: `capture` keeps its stdout, `quiet` drops its stderr. A missing program
    fails as the shell's 127."""
    try:
        return subprocess.run(
            command,
            check=check,
            text=True,
            env=env,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.DEVNULL if quiet else None,
        )
    except FileNotFoundError:
        if check:
            raise
        return subprocess.CompletedProcess(command, 127, "", "")


@dataclass(frozen=True)
class Host:
    """Where the update reads and writes (`root`, `/` but in tests) and how it runs commands."""

    root: Path = Path("/")
    run: Run = run_command

    def path(self, absolute: str) -> Path:
        return self.root / absolute.lstrip("/")

    def git(self, *args: str, check: bool = True, quiet: bool = False) -> int:
        return self.run(
            ["git", "-C", str(self.path(SRC)), *args], check=check, quiet=quiet
        ).returncode

    def git_out(self, *args: str, check: bool = True) -> str:
        command = ["git", "-C", str(self.path(SRC)), *args]
        return self.run(command, check=check, capture=True).stdout


def read(path: Path) -> str:
    """A file as `$(cat file)` reads it: "" when missing."""
    try:
        return path.read_text().rstrip("\n")
    except OSError:
        return ""


def say(text: str, *, error: bool = False) -> None:
    # Flushed at once, so the journal keeps it in order with the commands' own output.
    print(text, file=sys.stderr if error else sys.stdout, flush=True)


# A change that fails twice (its install or its image) is tried again only an hour after its last
# failure. The file holds one line, "<change> <failures> <time>", for the last change that failed.
def gave_up(host: Host, change: str, now: float) -> bool:
    for line in read(host.path(FAILED)).splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[0] == change:
            try:
                if int(fields[1]) >= GIVE_UP_FAILURES and now - float(fields[2]) < RETRY_SECONDS:
                    return True
            except ValueError:
                continue
    return False


def failed_once(host: Host, change: str, now: float) -> None:
    failures = 0
    for line in read(host.path(FAILED)).splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0] == change and fields[1].isdecimal():
            failures = int(fields[1])
    host.path(FAILED).write_text(f"{change} {failures + 1} {int(now)}\n")


def shipped(subjects: str) -> str:
    """The pull requests a change ships, for install-host.sh's restart reason: the first five."""
    found = [str(m) for m in re.findall(r"\(#[0-9]*\)", subjects)]
    return " ".join(m.strip("()") for m in found[:5])


def plain(reason: str) -> str:
    """The reason reaches a shell: plain characters only."""
    return re.sub(r"[^A-Za-z0-9 #:._,()/-]", "", reason)


def lock(host: Host, follow: bool) -> int | None:
    """One update at a time: a named commit waits its turn; the timer's catch-up gives way.
    The lock's file, held open while the update runs, or None when it could not be taken."""
    fd = os.open(host.path(LOCK), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    deadline = time.monotonic() + 900
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            if follow or time.monotonic() >= deadline:
                os.close(fd)
                return None
            time.sleep(1)


def update(host: Host, args: Sequence[str], env: Mapping[str, str], now: float) -> int:
    want = args[0] if args else ""
    if not want:
        say(USAGE, error=True)
        return 1
    follow = want == "--follow"
    fd = lock(host, follow)
    if fd is None:
        if follow:
            say("another update is running")
            return 0
        say("another update has run for 15 minutes", error=True)
        return 1
    try:
        return update_locked(host, args, env, now, want, follow)
    finally:
        os.close(fd)


def update_locked(
    host: Host, args: Sequence[str], env: Mapping[str, str], now: float, want: str, follow: bool
) -> int:
    pinned = host.path(PINNED)
    if follow and pinned.exists():
        say(f"rolled back by hand to {read(pinned)}; sudo rm {PINNED} to follow main again")
        return 0
    if follow:
        heads = host.run(
            ["timeout", "30", "git", "ls-remote", REPO, "refs/heads/main"],
            check=False,
            capture=True,
        ).stdout
        want = "\n".join(line.split("\t")[0] for line in heads.splitlines())
        if not want:
            say("GitHub did not answer")
            return 0
    if not re.fullmatch(r"[0-9a-f]{40}", want):
        say(f"not a commit: {want}", error=True)
        return 2
    have = read(host.path(INSTALLED))

    if want != have:
        if not host.path(f"{SRC}/.git").is_dir():
            host.run(["git", "clone", "-q", "--no-checkout", REPO, str(host.path(SRC))])
        host.git("fetch", "-q", "origin", "main")
        if host.git("cat-file", "-e", f"{want}^{{commit}}", check=False, quiet=True):
            host.git("fetch", "-q", "origin", want)
        if host.git("merge-base", "--is-ancestor", want, "origin/main", check=False):
            say(f"{want} is not on main", error=True)
            return 2
        if have and host.git("merge-base", "--is-ancestor", have, want, check=False):
            # Main moved back past the installed commit (a force push): the timer waits rather
            # than failing every minute.
            if follow:
                say(f"main is at {want}, older than the installed {have}")
                return 0
            say(
                f"{want} is older than the installed {have}: roll back by hand with ROLLBACK=1",
                error=True,
            )
            if env.get("ROLLBACK") != "1":
                return 2
            # Held there until the pin is removed, or the timer would take main again within a
            # minute.
            pinned.write_text(f"{want}\n")
        host.git("checkout", "-q", "--detach", "--force", want)
        # Main moves for many reasons: only a change to what install-host.sh installs is installed.
        if not have or host.git("diff", "--quiet", have, want, "--", *INSTALL_PATHS, check=False):
            change = host.git_out("log", "-1", "--format=%H", want, "--", *INSTALL_PATHS).strip()
            if gave_up(host, change, now):
                say(f"{change} failed twice; tried again an hour after")
                return 0
            span = f"{have}..{want}" if have else want
            subjects = host.git_out("log", "--format=%s", span, "--", *INSTALL_PATHS, check=False)
            given = args[1] if len(args) > 1 else ""
            reason = plain(given or env.get("REASON") or f"homelab {shipped(subjects)}")
            install = [str(host.path(f"{SRC}/ci-runner/install-host.sh"))]
            if host.run(install, check=False, env={**env, "REASON": reason}).returncode:
                failed_once(host, change, now)
                return 1
            # For the intrusion alerts (beelink/security/security_facts.py): code installed as root.
            author = host.git_out("log", "-1", "--format=%an", want, check=False).rstrip("\n")
            host.run(
                ["logger", "-t", "homelab-ci-update", f"installed commit={want} author={author}"]
            )

    # Rebuilt only when what goes into it changed since the commit it was built from; below the
    # jobs on the disk.
    built_file = host.path(f"{IMAGES}/commit")
    built = read(built_file)
    if not built or host.git("diff", "--quiet", built, want, "--", *IMAGE_INPUTS, check=False):
        change = host.git_out("log", "-1", "--format=%H", want, "--", *IMAGE_INPUTS).strip()
        build = [
            "nice",
            "-n",
            "10",
            "ionice",
            "-c3",
            str(host.path(f"{SRC}/ci-runner/build-rootfs.sh")),
        ]
        if gave_up(host, change, now):
            say(f"the image for {change} failed twice; jobs keep the old one for now")
        elif host.run(build, check=False).returncode == 0:
            built_file.write_text(f"{want}\n")
        else:
            failed_once(host, change, now)
            return 1

    # Last: a host whose update stopped half way says it still runs the old commit, and tries again.
    installed = host.path(INSTALLED)
    staged = installed.with_name(installed.name + ".new")
    staged.write_text(f"{want}\n")
    staged.replace(installed)
    say(f"on {want}, image built at {read(built_file) or 'none'}")
    # Each slot count is tested once under full load, in its own unit (homelab-ci-load-test).
    if host.run([*SUPERVISOR, "start-load-test-if-due"], check=False).returncode:
        say("could not start the load test; next pass")
    return 0


def main() -> int:
    if os.geteuid() != 0:
        print("run with sudo", file=sys.stderr)
        return 1
    try:
        return update(Host(), sys.argv[1:], os.environ, time.time())
    except subprocess.CalledProcessError as e:
        return e.returncode if e.returncode > 0 else 128 - e.returncode


if __name__ == "__main__":
    sys.exit(main())
