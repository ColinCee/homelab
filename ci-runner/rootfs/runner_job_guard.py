"""Refuse any job on a public repo's CI VM that is not Colin's own (docs/architecture.md,
"CI runner").

Runs as the runner's job-started hook (ACTIONS_RUNNER_HOOK_JOB_STARTED), before the
job's first step. A failing hook only marks the job failed: the runner still runs later
steps whose condition allows it (`if: always()`, and the pre steps of JavaScript
actions). So a refusal also kills the job's Runner.Worker, the process that runs the
steps, and nothing after the hook runs.

Built into the VM image (ci-runner/rootfs), on the image's python3.
"""

from __future__ import annotations

import json
import os
import signal
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

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


# What the supervisor says a VM serves, written by ci-boot.
SERVES = "/etc/homelab-ci/serves.json"
VM_EVENTS = frozenset({"push", "pull_request", "workflow_dispatch"})
OWNER_ID, OWNER = "11246116", "ColinCee"


def decide_vm(
    env: Mapping[str, str], serves: Mapping[str, Any], event: Mapping[str, Any]
) -> Decision:
    """A private repo's VM takes its repo's jobs. A public repo's takes only Colin's, from
    the repo itself, never a fork: its runner takes any queued job of the repo, whoever
    queued it (docs/architecture.md, "CI runner"). Missing means no."""
    served = serves.get("repo")
    repository = env.get("GITHUB_REPOSITORY", "")
    if not isinstance(served, str) or not served or repository.lower() != served.lower():
        return Decision(False, f"repository {repository or '(missing)'} is not the served {served}")
    if serves.get("public") is False:
        return Decision(True, f"a job of private {repository}")
    for name in INJECTION_VARIABLES:
        if env.get(name):
            return Decision(False, f"{name} is set; workflow environment reached the hook")
    kind = env.get("GITHUB_EVENT_NAME", "")
    if kind not in VM_EVENTS:
        return Decision(False, f"event {kind or '(missing)'} is not one of {sorted(VM_EVENTS)}")
    if env.get("GITHUB_ACTOR_ID", "") != OWNER_ID:
        actor = env.get("GITHUB_ACTOR", "") or "(missing)"
        return Decision(False, f"actor {actor} is not {OWNER}")
    triggering = env.get("GITHUB_TRIGGERING_ACTOR", "")
    if triggering != OWNER:
        return Decision(False, f"triggering actor {triggering or '(missing)'} is not {OWNER}")
    if kind == "pull_request":
        head = ((event.get("pull_request") or {}).get("head") or {}).get("repo") or {}
        source = head.get("full_name") if isinstance(head, Mapping) else None
        if not isinstance(source, str) or source.lower() != repository.lower():
            return Decision(False, f"pull request from {source or '(unknown)'}, not {repository}")
    return Decision(True, f"{kind} of public {repository} by {OWNER}")


def read_json(path: str) -> Mapping[str, Any]:
    """A JSON object from path, {} when missing or not one: the policy then refuses."""
    try:
        with open(path, encoding="utf-8") as f:
            value = json.load(f)
    except OSError, ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def audit_line(env: Mapping[str, str], decision: Decision) -> str:
    """One line for the VM's journal: the decision, safe values, and other variable names."""
    values = " ".join(f"{name}={env.get(name, '')}" for name in LOGGED_VALUES)
    others = ",".join(sorted(name for name in env if name not in LOGGED_VALUES))
    verdict = "allow" if decision.allowed else "refuse"
    return f"verdict={verdict} reason={decision.reason!r} {values} names={others}"


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
        except OSError, ValueError:
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


def main(env: Mapping[str, str] | None = None, stop: Callable[[int], str] = stop_job) -> int:
    env = os.environ if env is None else env
    event = read_json(env.get("GITHUB_EVENT_PATH", ""))
    decision = decide_vm(env, read_json(SERVES), event)
    _journal(audit_line(env, decision))
    if decision.allowed:
        print(f"Runner guard: allowed ({decision.reason}).")
        return 0
    print(f"::error::Runner guard refused this job: {command_value(decision.reason)}", flush=True)
    outcome = stop(os.getppid())
    _journal(f"stop={outcome!r}")
    print(f"Runner guard: {outcome}.", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
