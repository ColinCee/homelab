"""Deploys main to the Beelink: deploy-main.service runs it when the doorbell rings and every 15
minutes (docs/architecture.md, "Deploys").

It runs from the checkout it updates, so a change to this file takes effect from the next run.
Standard library only: it runs on the host's /usr/bin/python3.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

# The shared module, whatever unit file starts this one: a deploy that fails before installing the
# new unit must not leave the next run unable to import it.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "telemetry"))
import telemetry

HOME = Path.home()
REPO = HOME / "code/homelab"
# deploy.py records here the last commit whose every stack deployed; it is what main is compared to.
MARKER = HOME / ".local/state/homelab/last-deployed"
METRICS = Path("/var/lib/homelab-metrics/main-deploy.prom")
# A flood of rings costs one fetch per window.
FETCH_EVERY_SECONDS = 10
# Merges landing during a deploy are deployed in the same run: their ring arrives while this service
# is still active, and systemd does not start it again for it.
MAX_ROUNDS = 5
log = logging.getLogger("deploy-main")

Run = Callable[[list[str]], subprocess.CompletedProcess[str]]


def run_in(repo: Path) -> Run:
    def run(command: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(command, cwd=repo, capture_output=True, text=True, check=False)

    return run


@dataclass
class Host:
    run: Run
    deploy: Callable[[str | None], int]
    marker: Path
    metrics: Path
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    last_fetch: float = 0.0

    def git(self, *args: str) -> str:
        done = self.run(["git", *args])
        if done.returncode:
            raise RuntimeError(f"git {' '.join(args)}: {done.stderr.strip()}")
        return done.stdout.strip()

    def fetch_main(self) -> str:
        wait = self.last_fetch + FETCH_EVERY_SECONDS - self.clock()
        if wait > 0:
            self.sleep(wait)
        self.git("fetch", "--no-tags", "origin", "main")
        self.last_fetch = self.clock()
        return self.git("rev-parse", "origin/main")

    def deployed(self) -> str | None:
        return self.marker.read_text().strip() or None if self.marker.is_file() else None

    def follows(self, older: str, newer: str) -> bool:
        return self.run(["git", "merge-base", "--is-ancestor", older, newer]).returncode == 0

    def record(self, *, behind: bool) -> None:
        checked, behind_metric = (
            "homelab_main_deploy_checked_timestamp_seconds",
            "homelab_main_deploy_behind",
        )
        lines = [
            f"# HELP {checked} When deploy-main last fetched main.",
            f"# TYPE {checked} gauge",
            f"{checked} {self.clock():.0f}",
            f"# HELP {behind_metric} Whether main has commits not fully deployed (1 yes).",
            f"# TYPE {behind_metric} gauge",
            f"{behind_metric} {int(behind)}",
        ]
        telemetry.replace_file(self.metrics, "\n".join(lines) + "\n")


def deploy_main(host: Host) -> int:
    """Bring the checkout to main and deploy what changed, until main stops moving. 0 when main
    is deployed; else the deploy's code, and the next ring or timer tick tries again."""
    target = host.fetch_main()
    for _ in range(MAX_ROUNDS):
        since = host.deployed()
        if since and since != target and not host.follows(since, target):
            # Merges only add to main; a main that lost the deployed commit was force-pushed.
            log.error(
                "refused: main does not contain the deployed commit",
                extra={"main": target, "deployed": since},
            )
            host.record(behind=True)
            return 1
        # On branch main whatever was checked out before, so the checkout says what it runs.
        host.git("checkout", "--force", "-q", "-B", "main", target)
        # Also when main has not moved: deploy.py redeploys the stacks reading a changed secret.
        code = host.deploy(since)
        host.record(behind=code != 0)
        if code:
            log.error("deploy failed", extra={"commit": target, "code": code})
            return code
        log.info("deployed", extra={"commit": target})
        latest = host.fetch_main()
        if latest == target:
            return 0
        target = latest
    host.record(behind=True)
    return 0


def deploy_with_script(repo: Path, marker: Path) -> Callable[[str | None], int]:
    def deploy(since: str | None) -> int:
        command = [str(repo / "stacks/deploy/deploy.sh"), "--last-deployed", str(marker)]
        if since:
            command += ["--first-changed-since", since]
        return subprocess.run(command, cwd=repo, check=False).returncode

    return deploy


def main() -> int:
    host = Host(
        run=run_in(REPO),
        deploy=deploy_with_script(REPO, MARKER),
        marker=MARKER,
        metrics=METRICS,
    )
    try:
        return deploy_main(host)
    except (OSError, RuntimeError) as e:
        # Nothing recorded: the check's time goes stale, which "Main not deployed" reads.
        log.error("stopped", extra={"error": str(e)})
        return 1


if __name__ == "__main__":
    telemetry.start()
    raise SystemExit(main())
