#!/usr/bin/env python3
"""Report how long a Control room change took from its merge to the new version going live.

Runs in deploy.yaml after a deploy that changed the Control room's pinned image. The deployed
commit is the one baked into the pinned image; its auto-kaizen committer date is the merge time
(squash merges). Live is the first /state answer naming that commit: answers naming another
commit come from the old container. With --ready-at, that is the first such answer a watcher
(--watch, started before the deploy) logged at or after the stack came up, so stacks deployed
after it do not count. The time splits into the auto-kaizen image build, this repo's pull
request that pinned the image, and the deploy job. The result goes to the step summary and one
notice, so it can be read later through the checks API.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

API_URL = "https://api.github.com"
KAIZEN_REPO = "ColinCee/auto-kaizen"
USER_AGENT = "homelab-control-room-timing"

Fetch = Callable[[str], Any]


@dataclass(frozen=True)
class Timing:
    """Seconds from the auto-kaizen merge to the new Control room answering, and its parts."""

    total: int
    image: int
    pull_request: int
    deploy: int


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _seconds(start: datetime, end: datetime) -> int:
    return round((end - start).total_seconds())


def compute(
    *,
    merged: datetime,
    image_done: datetime,
    pr_opened: datetime,
    pr_merged: datetime,
    deploy_started: datetime,
    live: datetime,
) -> Timing:
    return Timing(
        total=_seconds(merged, live),
        image=_seconds(merged, image_done),
        pull_request=_seconds(pr_opened, pr_merged),
        deploy=_seconds(deploy_started, live),
    )


def notice(t: Timing) -> str:
    return (
        f"::notice title=Control room live::{t.total}s after merge "
        f"(image {t.image}s, pull request {t.pull_request}s, deploy {t.deploy}s)"
    )


def summary(t: Timing, commit: str) -> str:
    return "\n".join(
        [
            f"### Control room live {t.total}s after merge",
            "",
            f"Commit `{commit[:7]}`",
            "",
            "| Part | Seconds |",
            "| --- | ---: |",
            f"| Merge to image built | {t.image} |",
            f"| Homelab pull request open to merged | {t.pull_request} |",
            f"| Deploy job start to live | {t.deploy} |",
            f"| Merge to live | {t.total} |",
            "",
        ]
    )


def image_run_end(runs: list[Mapping[str, Any]]) -> datetime:
    """End of the main-branch 'image' workflow run, which pushes the image."""
    for run in runs:
        if run.get("name") == "image" and run.get("event") == "push":
            return parse_time(run["updated_at"])
    raise LookupError("no image run for the deployed commit")


def merged_pull(pulls: list[Mapping[str, Any]]) -> tuple[datetime, datetime]:
    """When the merged pull request behind this push was opened and merged."""
    for pull in pulls:
        if pull.get("merged_at"):
            return parse_time(pull["created_at"]), parse_time(pull["merged_at"])
    raise LookupError("no merged pull request for this push")


def state_commit(url: str) -> str | None:
    """The commit /state names, or None when it does not answer."""
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return json.load(response).get("commit") or None
    except (OSError, ValueError):
        return None


def watch(url: str, log: Path, *, duration: float = 3600, interval: float = 1) -> None:
    """Log each /state answer with its time, one JSON line each, for ``duration`` seconds."""
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        if commit := state_commit(url):
            with log.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"at": datetime.now(UTC).isoformat(), "commit": commit}) + "\n")
        time.sleep(interval)


def read_answers(log: Path) -> list[tuple[datetime, str]]:
    if not log.is_file():
        return []
    answers = []
    for line in log.read_text(encoding="utf-8").splitlines():
        try:
            answer = json.loads(line)
            answers.append((parse_time(answer["at"]), answer["commit"]))
        except (ValueError, KeyError, TypeError):
            continue  # a line cut short when the watcher was stopped
    return answers


def first_live(
    answers: Iterable[tuple[datetime, str]], commit: str, ready_at: datetime
) -> datetime | None:
    """When /state first named ``commit`` at or after ``ready_at``."""
    return next((at for at, named in answers if named == commit and at >= ready_at), None)


def wait_for_logged_live(
    log: Path, commit: str, ready_at: datetime, *, timeout: float = 15, interval: float = 0.5
) -> datetime | None:
    """First logged answer naming ``commit`` from ``ready_at``, waiting for the watcher to log it.

    This step starts moments after the stack comes up, usually before the watcher's next answer.
    """
    deadline = time.monotonic() + timeout
    while (live := first_live(read_answers(log), commit, ready_at)) is None:
        if time.monotonic() >= deadline:
            return None
        time.sleep(interval)
    return live


def wait_for_commit(
    url: str,
    commit: str,
    *,
    timeout: float = 120,
    interval: float = 1,
    ask: Callable[[str], str | None] = state_commit,
) -> datetime:
    """Poll /state until it names ``commit``; return when it first did."""
    deadline = time.monotonic() + timeout
    while True:
        if ask(url) == commit:
            return datetime.now(UTC)
        if time.monotonic() >= deadline:
            raise TimeoutError(f"/state did not name {commit[:7]} within {timeout:.0f}s")
        time.sleep(interval)


def pinned_commit(compose_file: Path) -> str:
    """The auto-kaizen commit baked into the image the compose file pins (KAIZEN_COMMIT)."""
    found = re.search(r"^\s*image:\s*(\S+)", compose_file.read_text(encoding="utf-8"), re.M)
    if not found:
        raise LookupError(f"no image in {compose_file}")
    inspected = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{json .Config.Env}}", found[1]],
        capture_output=True,
        text=True,
        check=True,
    )
    for item in json.loads(inspected.stdout) or []:
        if item.startswith("KAIZEN_COMMIT="):
            return item.removeprefix("KAIZEN_COMMIT=")
    raise LookupError(f"{found[1]} names no KAIZEN_COMMIT")


def github_fetch(token: str) -> Fetch:
    def fetch(path: str) -> Any:
        request = urllib.request.Request(
            f"{API_URL}{path}",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "User-Agent": USER_AGENT,
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)

    return fetch


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-url", required=True, help="the Control room's /state")
    parser.add_argument("--deploy-started", help="ISO time the deploy job began")
    parser.add_argument(
        "--ready-at", default="", help="ISO time the Control room stack came up (deploy.py)"
    )
    parser.add_argument("--answers", type=Path, help="the --watch log to read live from")
    parser.add_argument("--watch", type=Path, help="only log /state's answers to this file")
    parser.add_argument(
        "--compose",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "stacks/kaizen-control-room/compose.yaml",
        help="the compose file pinning the deployed image",
    )
    args = parser.parse_args()
    if args.watch:
        watch(args.state_url, args.watch)
        return 0
    if not args.deploy_started:
        parser.error("--deploy-started is required")

    kaizen = github_fetch(os.environ["KAIZEN_TOKEN"])
    homelab = github_fetch(os.environ["GITHUB_TOKEN"])
    repo, sha = os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_SHA"]

    commit = pinned_commit(args.compose)
    live = None
    if args.ready_at and args.answers:
        live = wait_for_logged_live(args.answers, commit, parse_time(args.ready_at))
    if live is None:
        if args.ready_at:
            print("::warning::No /state answer was logged from the ready time; timing from now")
        live = wait_for_commit(args.state_url, commit)
    merged_at = kaizen(f"/repos/{KAIZEN_REPO}/commits/{commit}")["commit"]["committer"]["date"]
    merged = parse_time(merged_at)
    runs = kaizen(f"/repos/{KAIZEN_REPO}/actions/runs?head_sha={commit}")["workflow_runs"]
    pr_opened, pr_merged = merged_pull(homelab(f"/repos/{repo}/commits/{sha}/pulls"))

    t = compute(
        merged=merged,
        image_done=image_run_end(runs),
        pr_opened=pr_opened,
        pr_merged=pr_merged,
        deploy_started=parse_time(args.deploy_started),
        live=live,
    )
    print(notice(t))
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(path, "a", encoding="utf-8") as f:
            f.write(summary(t, commit))
    return 0


if __name__ == "__main__":
    sys.exit(main())
