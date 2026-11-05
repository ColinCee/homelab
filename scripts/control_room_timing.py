#!/usr/bin/env python3
"""Report how long a Control room change took from its merge to the new version answering.

Runs in deploy.yaml after a deploy that changed the Control room's pinned image. The deployed
commit comes from the Control room's /state; its auto-kaizen committer date is the merge time
(squash merges). The time splits into the auto-kaizen image build, this repo's pull request that
pinned the image, and the deploy job. The result goes to the step summary and one notice, so it
can be read later through the checks API.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
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


def wait_for_commit(url: str, timeout: float = 120, interval: float = 1) -> tuple[str, datetime]:
    """Poll /state until it names a commit; return it and when it first answered."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                commit = json.load(response).get("commit")
            if commit:
                return commit, datetime.now(UTC)
        except (OSError, ValueError):
            pass
        if time.monotonic() >= deadline:
            raise TimeoutError(f"/state named no commit within {timeout:.0f}s")
        time.sleep(interval)


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
    parser.add_argument("--deploy-started", required=True, help="ISO time the deploy job began")
    args = parser.parse_args()

    kaizen = github_fetch(os.environ["KAIZEN_TOKEN"])
    homelab = github_fetch(os.environ["GITHUB_TOKEN"])
    repo, sha = os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_SHA"]

    commit, live = wait_for_commit(args.state_url)
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
