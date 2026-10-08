#!/usr/bin/env python3
"""Alert when a deploy stays queued, usually because the Beelink runner is down.

Runs on GitHub-hosted runners because on-host monitoring cannot detect its own
outage; Healthchecks.io covers full host outages. Notifies Discord only when
the watchdog's state changes, using the previous watchdog run's conclusion as
state.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

API_URL = "https://api.github.com"
USER_AGENT = "homelab-deploy-watchdog"

Fetch = Callable[[str, str], Any]


@dataclass(frozen=True)
class Problem:
    """One reason the deploy path is unhealthy."""

    summary: str
    url: str | None = None


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def stalled_runs(
    runs: Iterable[Mapping[str, Any]], now: datetime, max_queued: timedelta
) -> list[Problem]:
    """Return queued runs whose latest attempt has waited longer than allowed."""
    problems = []
    for run in runs:
        if run.get("status") != "queued":
            continue
        started = _parse_time(run.get("run_started_at") or run["created_at"])
        waited = now - started
        if waited > max_queued:
            minutes = int(waited.total_seconds() // 60)
            problems.append(
                Problem(
                    f"Deploy run #{run['run_number']} queued for {minutes} min", run["html_url"]
                )
            )
    return problems


def format_message(problems: list[Problem], recovered: bool) -> str:
    if recovered:
        return "✅ **Deploy watchdog recovered**: no deploy is stuck in the queue."
    lines = ["🚨 **Deploy watchdog**: deploys to Beelink are stalled."]
    for problem in problems:
        lines.append(f"- {problem.summary}" + (f" ({problem.url})" if problem.url else ""))
    return "\n".join(lines)


def should_notify(problems: list[Problem], previous_conclusion: str | None) -> bool:
    """Notify on transitions only, so a long outage posts once and recovers once."""
    previously_failing = previous_conclusion == "failure"
    return bool(problems) != previously_failing


def github_fetch(token: str) -> Fetch:
    def fetch(path: str, query: str = "") -> Any:
        url = f"{API_URL}{path}" + (f"?{query}" if query else "")
        request = urllib.request.Request(
            url,
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


def post_discord(webhook_url: str, content: str) -> None:
    # Discord rejects urllib's default user agent.
    request = urllib.request.Request(
        webhook_url,
        data=json.dumps({"content": content}).encode(),
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30):
        pass


ROUTINE_FIRE_URL = (
    "https://api.anthropic.com/v1/claude_code/routines/trig_0177p7ADgjATF9pSBqk74wQ1/fire"
)


def fire_routine(token: str, text: str) -> None:
    """Wake the Claude alert-triage routine with the alert text."""
    request = urllib.request.Request(
        ROUTINE_FIRE_URL,
        data=json.dumps({"text": text}).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30):
        pass


def collect_problems(
    fetch: Fetch,
    repo: str,
    *,
    workflow: str,
    now: datetime,
    max_queued: timedelta,
) -> list[Problem]:
    problems = []
    try:
        data = fetch(f"/repos/{repo}/actions/workflows/{workflow}/runs", "status=queued")
        problems += stalled_runs(data["workflow_runs"], now, max_queued)
    except Exception as exc:
        problems.append(Problem(f"Could not list queued deploy runs: {exc}"))

    return problems


def previous_conclusion(fetch: Fetch, repo: str, workflow: str) -> str | None:
    data = fetch(f"/repos/{repo}/actions/workflows/{workflow}/runs", "status=completed&per_page=1")
    runs = data["workflow_runs"]
    return runs[0]["conclusion"] if runs else None


def _positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--deploy-workflow", default="deploy.yaml")
    parser.add_argument("--watchdog-workflow", default="deploy-watchdog.yaml")
    parser.add_argument("--max-queued-minutes", type=_positive_int, default=15)
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    if not args.repo or not token:
        print("Error: GITHUB_REPOSITORY/--repo and GITHUB_TOKEN are required", file=sys.stderr)
        return 2

    fetch = github_fetch(token)
    problems = collect_problems(
        fetch,
        args.repo,
        workflow=args.deploy_workflow,
        now=datetime.now(UTC),
        max_queued=timedelta(minutes=args.max_queued_minutes),
    )
    for problem in problems:
        print(f"::error::{problem.summary}")

    previous = previous_conclusion(fetch, args.repo, args.watchdog_workflow)
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if should_notify(problems, previous):
        if webhook_url:
            post_discord(webhook_url, format_message(problems, recovered=not problems))
        else:
            print("DISCORD_WEBHOOK_URL not set; skipping notification")
        routine_token = os.environ.get("CLAUDE_ROUTINE_TOKEN")
        if problems and routine_token:
            try:
                fire_routine(routine_token, format_message(problems, recovered=False))
            except Exception as exc:
                print(f"::warning::Could not wake Claude triage routine: {exc}")

    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
