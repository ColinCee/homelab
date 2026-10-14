#!/usr/bin/env python3
"""Refuse any job on the Beelink deploy runner except the deploy workflows on main.

Runs as the runner's job-started hook (ACTIONS_RUNNER_HOOK_JOB_STARTED), before the
job's first step; a non-zero exit fails the job. The repository is public, so a fork
pull request can bring its own workflow targeting this runner, and only an approval
click stands in the way. This guard closes that path on the host, where workflows
cannot change it.

The installed copy is root-owned and is not updated by deploys, so a push to main
cannot rewrite the policy. See docs/runbooks/deploying-services.md.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

REPOSITORY = "ColinCee/homelab"
REF = "refs/heads/main"
EVENTS = frozenset({"push", "schedule", "workflow_dispatch"})
WORKFLOWS = frozenset(
    f"{REPOSITORY}/.github/workflows/{name}@{REF}" for name in ("deploy.yaml", "server-health.yaml")
)
# Present only if a workflow's env reached the hook; bash would have sourced them first.
INJECTION_VARIABLES = ("BASH_ENV", "ENV")
# Values safe to log; everything else is logged by name only.
LOGGED_VALUES = (
    "GITHUB_REPOSITORY",
    "GITHUB_EVENT_NAME",
    "GITHUB_REF",
    "GITHUB_WORKFLOW_REF",
    "GITHUB_ACTOR",
    "GITHUB_RUN_ID",
)


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str


def decide(env: Mapping[str, str]) -> Decision:
    """Allow only the deploy workflows, from main, on this repository. Missing means no."""
    for name in INJECTION_VARIABLES:
        if env.get(name):
            return Decision(False, f"{name} is set; workflow environment reached the hook")
    repository = env.get("GITHUB_REPOSITORY", "")
    if repository != REPOSITORY:
        return Decision(False, f"repository {repository or '(missing)'} is not {REPOSITORY}")
    event = env.get("GITHUB_EVENT_NAME", "")
    if event not in EVENTS:
        return Decision(False, f"event {event or '(missing)'} is not one of {sorted(EVENTS)}")
    ref = env.get("GITHUB_REF", "")
    if ref != REF:
        return Decision(False, f"ref {ref or '(missing)'} is not {REF}")
    workflow = env.get("GITHUB_WORKFLOW_REF", "")
    if workflow not in WORKFLOWS:
        return Decision(
            False, f"workflow {workflow or '(missing)'} is not an allowed deploy workflow"
        )
    return Decision(True, f"{event} of {workflow}")


def audit_line(env: Mapping[str, str], decision: Decision, mode: str) -> str:
    """One line for the host journal: the decision, safe values, and other variable names."""
    values = " ".join(f"{name}={env.get(name, '')}" for name in LOGGED_VALUES)
    others = ",".join(sorted(name for name in env if name not in LOGGED_VALUES))
    verdict = "allow" if decision.allowed else "refuse"
    return f"mode={mode} verdict={verdict} reason={decision.reason!r} {values} names={others}"


def _journal(line: str) -> None:
    if sys.platform == "win32":  # tests only; the guard runs on the Beelink
        print(line, file=sys.stderr)
        return
    import syslog

    syslog.openlog("homelab-runner-guard")
    syslog.syslog(syslog.LOG_NOTICE, line)


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("enforce", "observe"),
        default="enforce",
        help="observe logs the decision but lets every job run (for rollout only)",
    )
    args = parser.parse_args(argv)
    env = os.environ if env is None else env
    decision = decide(env)
    _journal(audit_line(env, decision, args.mode))
    if decision.allowed:
        print(f"Runner guard: allowed ({decision.reason}).")
        return 0
    if args.mode == "observe":
        print(f"::warning::Runner guard (observe mode) would refuse this job: {decision.reason}")
        return 0
    print(f"::error::Runner guard refused this job: {decision.reason}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
