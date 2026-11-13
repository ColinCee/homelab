"""What the supervisor asks GitHub: which repositories it serves, their queued jobs, and
runner registrations, through one client that keeps its connections open and asks again
only with conditional requests, which cost nothing when the answer has not changed."""

from __future__ import annotations

import http.client
import io
import json
import logging
import threading
import time
import urllib.error
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from ci_supervisor import State

log = logging.getLogger("ci-supervisor").info


LABEL = "homelab-fc"
RUNNER_LABELS = ("self-hosted", "linux", "x64", LABEL)
# One extra label, `fc-cache-<owner>--<name>`, asks for another served repo's cache disk
# (a job working on that repo's code). GitHub owner names cannot hold `--`.
CACHE_LABEL_PREFIX = "fc-cache-"
# Only Colin's own repositories are served, whatever else the token can see.
OWNER = "ColinCee"
OWNER_ID = 11246116
# Public repos served as well. A repo signs up here, not by token access: GitHub must hold
# every outside contributor's run for approval (STRICT_APPROVAL) and nobody else may push
# (only_colin). VMs start only for Colin's runs, and each VM's job guard refuses any other
# job (runner_job_guard.py), since a repo's runner takes any queued job.
PUBLIC_REPOS = frozenset({"ColinCee/homelab"})
STRICT_APPROVAL = "all_external_contributors"
# Written by build-rootfs.sh when the VM image carries that guard: until then (an image not yet
# rebuilt, or a failed rebuild) no public repo is served.
GUARD_MARKER = Path("/var/lib/homelab-ci/images/job-guard")


# How often to ask GitHub which repositories the token can see. The listing and its
# collaborator checks (two requests a repo) run beside the poll, never holding it up.
REPOS_EVERY = 60


API_HOST = "api.github.com"
API = f"https://{API_HOST}"


# Checked every poll: conditional requests make an unchanged answer free.
PRIVACY_RECHECK_SECONDS = 0
RUNNER_PREFIX = "fc-"


# What a VM is for: its repo, and the extra cache label it registers with ("" for none).
# A VM covers only jobs with the same pair.
type Want = tuple[str, str]


@dataclass(frozen=True)
class Job:
    repo: str
    id: int
    name: str
    labels: tuple[str, ...]
    # False for a job asking for our label with others no VM of ours has: it never starts.
    ours: bool = True
    # When GitHub queued it (its created_at), 0 when unknown.
    queued_at: float = 0.0
    # Its workflow's name, "" when unknown.
    workflow: str = ""
    # Its run already finished a job on the build label.
    run_started: bool = False

    @property
    def want(self) -> Want:
        return (self.repo, cache_label(self.labels))


@dataclass
class Serving:
    served: bool
    checked: float
    default_branch: str = ""
    public: bool = False


def cache_label_repo(label: str) -> str | None:
    """owner/name (lowercase) named by an `fc-cache-<owner>--<name>` label, else None."""
    label = label.lower()
    owner, sep, name = label.removeprefix(CACHE_LABEL_PREFIX).partition("--")
    if not label.startswith(CACHE_LABEL_PREFIX) or not (sep and owner and name):
        return None
    return f"{owner}/{name}"


def cache_label(labels: Iterable[str]) -> str:
    """The job's cache label, lowercase, or "" if it asks for none."""
    return next((label.lower() for label in labels if cache_label_repo(label)), "")


def wants_us(labels: Iterable[str]) -> bool:
    """A job is ours if it asks for our label and nothing our VMs do not have,
    apart from at most one cache label."""
    asked = {label.lower() for label in labels}
    extra = asked - set(RUNNER_LABELS)
    return LABEL in asked and len(extra) <= 1 and all(map(cache_label_repo, extra))


def queued_jobs(
    repo: str,
    runs: Iterable[Mapping[str, Any]],
    get_jobs: Callable[[int], Iterable[Mapping[str, Any]]],
) -> list[Job]:
    jobs: list[Job] = []
    for run in runs:
        run_jobs = list(get_jobs(int(run["id"])))
        done = [job for job in run_jobs if job.get("status") == "completed"]
        run_started = any(on_build_label(job.get("labels", [])) for job in done)
        for job in run_jobs:
            labels = tuple(str(label) for label in job.get("labels", []))
            if job.get("status") != "queued":
                continue
            ours = wants_us(labels)
            # One asking for our label with another we lack is kept, to log why it waits.
            if ours or LABEL in {label.lower() for label in labels}:
                queued_at = github_time(job.get("created_at"))
                jobs.append(
                    Job(
                        repo,
                        int(job["id"]),
                        str(job.get("name", "")),
                        labels,
                        ours,
                        queued_at,
                        str(job.get("workflow_name") or run.get("name") or ""),
                        run_started,
                    )
                )
    return jobs


def on_build_label(labels: Iterable[str]) -> bool:
    return LABEL in {str(label).lower() for label in labels}


def github_time(value: object) -> float:
    """Seconds since the epoch of one of GitHub's times, 0 when missing or unreadable."""
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except ValueError:
        return 0.0


def is_served(repo_data: Mapping[str, Any], repo: str, approval: Callable[[], str]) -> bool:
    """Private, or public, listed and holding outside contributors' runs for approval
    (`approval` asks GitHub, only for a listed public repo)."""
    if str(repo_data.get("full_name", "")).lower() != repo.lower():
        return False
    if repo_data.get("private") is True:
        return True
    listed = repo_data.get("private") is False and repo in PUBLIC_REPOS
    return listed and GUARD_MARKER.exists() and approval() == STRICT_APPROVAL


def approval_policy(gh: GitHubApi, repo: str) -> str:
    path = f"/repos/{repo}/actions/permissions/fork-pr-contributor-approval"
    return str(gh.request("GET", path).get("approval_policy", ""))


class RateLimited(Exception):
    def __init__(self, until: float) -> None:
        super().__init__(f"rate limited until {time.strftime('%H:%M:%S', time.localtime(until))}")
        self.until = until


class GitHubApi(Protocol):
    """What the supervisor asks of GitHub, so tests can stand in for it."""

    def request(self, method: str, path: str, body: Mapping[str, Any] | None = None) -> Any: ...  # noqa: ANN401 - GitHub's JSON, read as each endpoint documents it

    def forget(self, drop: Callable[[str], bool]) -> None: ...

    def clone(self) -> GitHubApi: ...


class GitHub:
    def __init__(self, token: str) -> None:
        self._token = token
        # Conditional GETs: a 304 reply does not count against the rate limit. The threads
        # asking repos at once share it, so they take the lock to touch it or the counts.
        self._cache: dict[str, tuple[str, Any]] = {}
        self._lock = threading.Lock()
        # One connection per thread, kept open across requests, so a request does not wait
        # for a new connection and its TLS handshake (4 Nov: about 0.45 s a request).
        self._local = threading.local()
        # Requests made and seconds spent on them, for the slow-poll log.
        self.requests = 0
        self.seconds = 0.0

    def _send(
        self, method: str, path: str, data: bytes | None, headers: Mapping[str, str]
    ) -> tuple[http.client.HTTPResponse, bytes]:
        """One request on this thread's open connection. One GitHub closed while it sat
        idle fails at once and is tried again on a new one; a POST (a VM's registration)
        always gets a new one, so it is never sent twice."""
        held = getattr(self._local, "conn", None)
        conn = held if isinstance(held, http.client.HTTPSConnection) else None
        if method == "POST" and conn is not None:
            conn.close()
            conn = None
        while True:
            reused = conn is not None
            if conn is None:
                conn = self._local.conn = http.client.HTTPSConnection(API_HOST, timeout=20)
            try:
                conn.request(method, path, body=data, headers=dict(headers))
                response = conn.getresponse()
                return response, response.read()
            except http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError:
                conn.close()
                if not reused:
                    raise
                conn = None
            except BaseException:
                conn.close()
                raise

    def request(self, method: str, path: str, body: Mapping[str, Any] | None = None) -> Any:  # noqa: ANN401 - see GitHubApi.request
        data = None if body is None else json.dumps(body).encode()
        # Sent only to api.github.com: redirects are not followed.
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "homelab-ci-supervisor",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        with self._lock:
            cached = self._cache.get(path) if method == "GET" else None
            self.requests += 1
        if cached:
            headers["If-None-Match"] = cached[0]
        started = time.monotonic()
        try:
            response, raw = self._send(method, path, data, headers)
        finally:
            with self._lock:
                self.seconds += time.monotonic() - started
        code, reply = response.status, response.headers
        if code == 304 and cached:
            return cached[1]
        if code in (403, 429) and (
            reply.get("Retry-After") or reply.get("X-RateLimit-Remaining") == "0"
        ):
            until = rate_limit_until(
                reply.get("Retry-After"), reply.get("X-RateLimit-Reset"), time.time()
            )
            raise RateLimited(until)
        if not 200 <= code < 300:
            url = f"{API}{path}"
            raise urllib.error.HTTPError(url, code, response.reason, reply, io.BytesIO(raw))
        result = json.loads(raw) if raw else None
        if method == "GET" and (etag := reply.get("ETag")):
            with self._lock:
                self._cache[path] = (etag, result)
        return result

    def clone(self) -> GitHub:
        return GitHub(self._token)

    def forget(self, drop: Callable[[str], bool]) -> None:
        with self._lock:
            for path in [path for path in self._cache if drop(path)]:
                del self._cache[path]


def rate_limit_until(retry_after: str | None, reset: str | None, now: float) -> float:
    """When to try again: Retry-After seconds, else the reset time, else a minute."""
    if retry_after:
        return now + int(retry_after)
    return float(reset) if reset else now + 60


def is_public(state: State, repo: str) -> bool:
    known = state.serving.get(repo)
    return repo in PUBLIC_REPOS or bool(known and known.public)


def check_served(
    gh: GitHubApi, state: State, repo: str, now: float, *, fresh: bool = False
) -> bool:
    known = state.serving.get(repo)
    # A repo with VMs here, or about to get its first, is re-checked; the rest are known
    # served from the minutely repo listing (list_repos), one request for all of them,
    # and asked directly if that listing is out of date.
    if (
        known is None
        or fresh
        or now - known.checked > 2 * REPOS_EVERY
        or (repo in state.used and now - known.checked > PRIVACY_RECHECK_SECONDS)
    ):
        data = gh.request("GET", f"/repos/{repo}")
        served = is_served(data, repo, lambda: approval_policy(gh, repo))
        if not served:
            log(f"refusing {repo}: not private, nor a listed public repo holding runs for approval")
        public = data.get("private") is not True
        state.serving[repo] = Serving(served, now, str(data.get("default_branch", "")), public)
    return state.serving[repo].served


def colins(run: Mapping[str, Any]) -> bool:
    """A run Colin started and, if re-run, re-ran."""
    actor, triggering = run.get("actor") or {}, run.get("triggering_actor") or {}
    return bool(actor.get("id") == OWNER_ID and triggering.get("login") == OWNER)


def find_queued(gh: GitHubApi, repo: str, *, colins_only: bool = False) -> list[Job]:
    """`colins_only` (a public repo) leaves out others' runs: no VM starts for them."""
    runs: list[Mapping[str, Any]] = []
    # A multi-job run is in_progress while its later jobs wait for a runner. A run that
    # waited on a concurrency group can stay pending after its job is queued.
    for status in ("queued", "pending", "in_progress"):
        page = gh.request("GET", f"/repos/{repo}/actions/runs?status={status}&per_page=50")
        runs.extend(page.get("workflow_runs", []))
    if colins_only:
        runs = [run for run in runs if colins(run)]

    def jobs_path(run_id: int) -> str:
        return f"/repos/{repo}/actions/runs/{run_id}/jobs?filter=latest&per_page=100"

    def get_jobs(run_id: int) -> Iterable[Mapping[str, Any]]:
        return gh.request("GET", jobs_path(run_id)).get("jobs", [])

    # Forget finished runs, so the cache only holds what is still polled.
    live = {jobs_path(int(run["id"])) for run in runs}
    gh.forget(lambda path: path.startswith(f"/repos/{repo}/actions/runs/") and path not in live)
    return queued_jobs(repo, runs, get_jobs)


def delete_runner(gh: GitHubApi, repo: str, runner_id: int, name: str) -> bool:
    """True if the registration is gone (removed now, or it removed itself)."""
    gh.forget(lambda path: path == f"/repos/{repo}/actions/runners/{runner_id}")
    try:
        gh.request("DELETE", f"/repos/{repo}/actions/runners/{runner_id}")
    except urllib.error.HTTPError as e:
        if e.code == 404:  # the runner removed itself after its job
            return True
        log(f"could not remove runner {name} from {repo}: HTTP {e.code}")
        return False
    return True


# Each repo GitHub would not show on the last try, with why; logged only when it changes.
SKIPPED: dict[str, str] = {}


def skipped(repo: str, error: Exception | None) -> None:
    """Notes one repo failing (or working again), so a lasting failure logs once, not every poll."""
    why = f"{type(error).__name__}: {error}" if error else None
    if why and SKIPPED.get(repo) != why:
        log(f"skipping {repo} until GitHub shows it: {why}")
        SKIPPED[repo] = why
    elif not why and SKIPPED.pop(repo, None):
        log(f"{repo} is back")


def remove_stale_runners(
    gh: GitHubApi, repos: Iterable[str], keep: Iterable[str] = ()
) -> list[str]:
    """Registrations left by a previous supervisor whose VMs have ended (`keep` names the
    ones still running). Returns the repos GitHub would not show (say, the token cannot see
    them), to try again later."""
    keep = set(keep)
    failed: list[str] = []
    for repo in repos:
        try:
            page = gh.request("GET", f"/repos/{repo}/actions/runners?per_page=100")
        except RateLimited:
            raise
        except Exception as e:
            skipped(f"{repo} runners", e)
            failed.append(repo)
            continue
        for runner in page.get("runners", []):
            name = str(runner.get("name", ""))
            if (
                name.startswith(RUNNER_PREFIX)
                and name not in keep
                and delete_runner(gh, repo, int(runner["id"]), name)
            ):
                log(f"removed stale runner {name} from {repo}")
    return failed


def private_repos(gh: GitHubApi) -> dict[str, str]:
    """Colin's private repositories the token can see, the ones that signed up, each
    with its default branch. An archived one takes no jobs, so it gets no VM or standby."""
    repos: list[dict[str, Any]] = []
    for n in range(1, 11):
        page = gh.request(
            "GET", f"/user/repos?visibility=private&affiliation=owner&per_page=100&page={n}"
        )
        repos += page
        if len(page) < 100:
            break
    return {
        str(r["full_name"]): str(r.get("default_branch", ""))
        for r in sorted(repos, key=lambda r: str(r["full_name"]))
        if r.get("private") is True
        and not r.get("archived")
        and (r.get("owner") or {}).get("login") == OWNER
    }


def only_colin(gh: GitHubApi, repo: str) -> bool:
    """Whether nobody but Colin can push to the repo: no other collaborator and no deploy
    key that can write. Asked on every listing and before a repo's first VM (unchanged
    answers are free); any doubt is a no, so only Colin and his own apps start jobs here."""
    try:
        people = gh.request("GET", f"/repos/{repo}/collaborators?affiliation=direct&per_page=100")
        keys = gh.request("GET", f"/repos/{repo}/keys?per_page=100")
    except RateLimited:
        raise
    except Exception as e:
        skipped(f"{repo} collaborators", e)
        return False
    skipped(f"{repo} collaborators", None)
    return all((p.get("login") or "") == OWNER for p in people) and all(
        k.get("read_only") is True for k in keys
    )


def public_repos(gh: GitHubApi) -> dict[str, str]:
    """The PUBLIC_REPOS served now (is_served), each with its default branch."""
    found: dict[str, str] = {}
    for repo in sorted(PUBLIC_REPOS):
        try:
            data = gh.request("GET", f"/repos/{repo}")
            served = is_served(data, repo, lambda r=repo: approval_policy(gh, r))
        except RateLimited:
            raise
        except Exception as e:
            skipped(repo, e)
            continue
        if served and not data.get("archived"):
            found[repo] = str(data.get("default_branch", ""))
    return found


def list_repos(gh: GitHubApi) -> tuple[dict[str, str], list[str]]:
    """The served repos with their default branches, and those nobody else can push to:
    the ones to serve."""
    listing = private_repos(gh) | public_repos(gh)
    return listing, [r for r in listing if only_colin(gh, r)]
