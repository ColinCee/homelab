#!/usr/bin/env python3
"""Refuse any job on the Beelink deploy runner except the deploy workflows on main.

Runs as the runner's job-started hook (ACTIONS_RUNNER_HOOK_JOB_STARTED), before the
job's first step. A failing hook only marks the job failed: the runner still runs later
steps whose condition allows it (`if: always()`, and the pre steps of JavaScript
actions). So a refusal also kills the job's Runner.Worker, the process that runs the
steps, and nothing after the hook runs. The repository is public, so a fork
pull request can bring its own workflow targeting this runner, and only an approval
click stands in the way. This guard closes that path on the host, where workflows
cannot change it.

The installed copy is root-owned and is not updated by deploys, so a push to main
cannot rewrite the policy. See docs/runbooks/deploying-services.md.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

REPOSITORY = "ColinCee/homelab"
REF = "refs/heads/main"
EVENTS = frozenset({"push", "schedule", "workflow_dispatch"})
WORKFLOWS = frozenset(
    f"{REPOSITORY}/.github/workflows/{name}@{REF}" for name in ("deploy.yaml", "server-health.yaml")
)
# Who may start a job here: Colin, and Renovate merging its own pull requests.
# The actor is checked by ID. The triggering actor (who started a re-run) is only
# available by name, so renaming either account means updating this list.
ACTOR_IDS = {"11246116": "ColinCee", "29139614": "renovate[bot]"}
TRIGGERING_ACTORS = frozenset(ACTOR_IDS.values())
# Present only if a workflow's env reached the hook; bash would have sourced them first.
INJECTION_VARIABLES = ("BASH_ENV", "ENV")
# Values safe to log; everything else is logged by name only.
LOGGED_VALUES = (
    "GITHUB_REPOSITORY",
    "GITHUB_EVENT_NAME",
    "GITHUB_REF",
    "GITHUB_WORKFLOW_REF",
    "GITHUB_ACTOR",
    "GITHUB_ACTOR_ID",
    "GITHUB_TRIGGERING_ACTOR",
    "GITHUB_RUN_ID",
)


WORKER = "Runner.Worker"
# The hook is bash running the wrapper, which execs Python; allow a few hops.
MAX_ANCESTORS = 8


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
    actor_id = env.get("GITHUB_ACTOR_ID", "")
    if actor_id not in ACTOR_IDS:
        actor = env.get("GITHUB_ACTOR", "") or "(missing)"
        return Decision(False, f"actor {actor} ({actor_id or 'no id'}) is not allowed")
    triggering = env.get("GITHUB_TRIGGERING_ACTOR", "")
    if triggering not in TRIGGERING_ACTORS:
        return Decision(False, f"triggering actor {triggering or '(missing)'} is not allowed")
    return Decision(True, f"{event} of {workflow} by {ACTOR_IDS[actor_id]}")


def audit_line(env: Mapping[str, str], decision: Decision, mode: str) -> str:
    """One line for the host journal: the decision, safe values, and other variable names."""
    values = " ".join(f"{name}={env.get(name, '')}" for name in LOGGED_VALUES)
    others = ",".join(sorted(name for name in env if name not in LOGGED_VALUES))
    verdict = "allow" if decision.allowed else "refuse"
    return f"mode={mode} verdict={verdict} reason={decision.reason!r} {values} names={others}"


def command_value(text: str) -> str:
    """Escape text for a workflow command, so a crafted workflow name cannot inject one."""
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _read_stat(pid: int) -> tuple[str, int]:
    """Process name and parent PID from /proc/<pid>/stat."""
    with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as f:
        stat = f.read()
    name = stat[stat.index("(") + 1 : stat.rindex(")")]
    parent = int(stat[stat.rindex(")") + 2 :].split()[1])
    return name, parent


def find_worker(start: int, read_stat: Callable[[int], tuple[str, int]] = _read_stat) -> int | None:
    """Walk up from the hook's parent to the Runner.Worker running this job."""
    pid = start
    for _ in range(MAX_ANCESTORS):
        if pid <= 1:
            return None
        try:
            name, parent = read_stat(pid)
        except (OSError, ValueError):
            return None
        if name == WORKER:
            return pid
        pid = parent
    return None


def _kill(pid: int) -> None:
    if sys.platform == "win32":  # tests only; the guard runs on the Beelink
        raise OSError("no SIGKILL on Windows")
    os.kill(pid, signal.SIGKILL)


def stop_job(
    start: int,
    read_stat: Callable[[int], tuple[str, int]] = _read_stat,
    kill: Callable[[int], None] = _kill,
) -> str:
    """Kill the job's Runner.Worker so no later step can run. Returns what happened."""
    worker = find_worker(start, read_stat)
    if worker is None:
        return "no Runner.Worker found above the hook; the job is only marked failed"
    try:
        kill(worker)
    except OSError as e:
        return f"could not kill Runner.Worker {worker}: {e}"
    return f"killed Runner.Worker {worker}"


def _journal(line: str) -> None:
    if sys.platform == "win32":  # tests only; the guard runs on the Beelink
        print(line, file=sys.stderr)
        return
    import syslog

    syslog.openlog("homelab-runner-guard")
    syslog.syslog(syslog.LOG_NOTICE, line)


def main(
    argv: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
    stop: Callable[[int], str] = stop_job,
) -> int:
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
    reason = command_value(decision.reason)
    if args.mode == "observe":
        print(f"::warning::Runner guard (observe mode) would refuse this job: {reason}")
        return 0
    print(f"::error::Runner guard refused this job: {reason}", flush=True)
    outcome = stop(os.getppid())
    _journal(f"mode=enforce stop={outcome!r}")
    print(f"Runner guard: {outcome}.", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
