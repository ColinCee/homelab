"""Each served repo's read-only cache disk, built from the outbox an ended VM leaves, once GitHub
shows its job passed on a push to the default branch (docs/architecture.md, "CI runner")."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.error
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import IO, TYPE_CHECKING

import github_api

if TYPE_CHECKING:
    from ci_supervisor import State
from github_api import GitHubApi

log = logging.getLogger("ci-supervisor").info


# Each repo's read-only cache disk: its package caches and installs, built from what a
# green push to its default branch leaves on its VM's outbox disk (docs/architecture.md,
# "CI runner").
CACHE_DIR = Path("/var/lib/homelab-ci/caches")
# / is shared with everything. run_vm.py sizes the outbox to this and its header.
CACHE_MAX_UNPACKED_BYTES = 6 * 1024**3
# Each VM's outbox (<runner name>.tar) and the repo it ran for (<runner name>.json), kept
# after the VM until judged. Not through GitHub artifacts: they count against the account's
# storage quota, and once it is full every upload fails.
OUTBOX_DIR = Path("/var/lib/homelab-ci/outbox")
# What the fc-cache action writes last, once the tar after it is complete.
OUTBOX_HEADER = re.compile(rb"homelab-ci-cache v1 run=([0-9]{1,20}) attempt=([0-9]{1,5})\n")
# An outbox whose job GitHub does not show finished yet is tried again a minute later,
# for up to an hour.
OUTBOX_RETRY_SECONDS = 60
OUTBOX_WAIT_SECONDS = 3600
RUNNER_UID = 1001  # the runner user inside the VM image


def cache_image(repo: str) -> Path:
    return CACHE_DIR / f"{repo.replace('/', '__')}.ext4"


def cache_for(repo: str, label: str, private: Iterable[str]) -> Path | None:
    """A VM's cache disk: its own repo's, or the one its label names if both repos are
    served and private (`private` is this poll's served private repos) and it has a disk.
    A public repo's job never gets, or gives, another repo's cache."""
    if not label:
        return cache_image(repo)
    served = {r.lower(): r for r in private}
    if repo.lower() not in served:
        return None
    named = served.get(github_api.cache_label_repo(label) or "")
    image = cache_image(named) if named else None
    return image if image and image.exists() else None


def safe_member(member: tarfile.TarInfo, dest: str) -> tarfile.TarInfo | None:
    """Plain files, folders and links only, nothing outside the folder (tar filter),
    and no device or sparse files: the host only unpacks this into a disk image."""
    if (
        not (member.isfile() or member.isdir() or member.issym() or member.islnk())
        or member.issparse()
    ):
        raise tarfile.FilterError(f"not a plain file, folder or link: {member.name}")
    # tar_filter checks a hard link's own path but not its target's: through a symlink
    # unpacked earlier, the target could be a host file, which the chown would then take.
    if member.islnk():
        root = os.path.realpath(dest)
        target = os.path.realpath(os.path.join(root, member.linkname))
        if os.path.commonpath([root, target]) != root:
            raise tarfile.FilterError(f"hard link to outside the folder: {member.name}")
    return tarfile.tar_filter(member, dest)


def size_capped(limit: int) -> Callable[[tarfile.TarInfo, str], tarfile.TarInfo | None]:
    """safe_member, refusing the archive once its files add up to more than limit."""
    total = 0

    def check(member: tarfile.TarInfo, dest: str) -> tarfile.TarInfo | None:
        nonlocal total
        total += member.size
        if total > limit:
            raise tarfile.FilterError(f"unpacks to more than {limit} bytes")
        return safe_member(member, dest)

    return check


def unpack_cache(stream: IO[bytes], root: Path) -> None:
    """Unpack a cache tar into root through size_capped. A file or link the archive holds
    twice takes its later entry, as tar does; tarfile would instead seek back for a hard link
    onto a file already there, which a stream cannot (servo's cache, 5 Nov)."""
    check = size_capped(CACHE_MAX_UNPACKED_BYTES)
    with tarfile.open(fileobj=stream, mode="r|") as tar:
        for member in tar:
            safe = check(member, str(root))  # the path is inside root, symlinks resolved
            if safe is None:
                continue
            dest = root / safe.name
            if not safe.isdir() and os.path.lexists(dest) and not os.path.isdir(dest):
                dest.unlink()
            tar.extract(safe, root, filter="fully_trusted")


class BadCache(Exception):
    """The tar itself cannot be unpacked; trying it again would fail the same way."""


def build_cache_image(stream: IO[bytes], image: Path) -> None:
    """Cache tar -> a folder -> an ext4 image, owned by the VM's runner user.
    Runs as root on the host and never executes anything from the tar."""
    with tempfile.TemporaryDirectory(dir=CACHE_DIR) as tmp:
        root = Path(tmp) / "root"
        root.mkdir()
        try:
            unpack_cache(stream, root)
        except Exception as e:
            raise BadCache(f"{type(e).__name__}: {e}") from e
        # Owned by the VM's runner user, so the job can write through its throwaway layer.
        # The folder is in a root-only temporary directory, so no host user reaches it.
        subprocess.run(["chown", "-R", f"{RUNNER_UID}:{RUNNER_UID}", str(root)], check=True)
        size = sum(f.stat().st_size for f in root.rglob("*") if f.is_file() and not f.is_symlink())
        staged = Path(tmp) / "cache.ext4"
        with staged.open("wb") as f:
            f.truncate(int(size * 1.3) + 256 * 1024**2)
        subprocess.run(
            [
                "mkfs.ext4",
                "-q",
                "-F",
                "-L",
                "cache",
                "-E",
                f"root_owner={RUNNER_UID}:{RUNNER_UID}",
                "-d",
                str(root),
                str(staged),
            ],
            check=True,
        )
        subprocess.run(["e2fsck", "-fp", str(staged)], check=False, capture_output=True)
        staged.chmod(0o644)
        staged.replace(image)


def take_outbox(gh: GitHubApi, tar: Path, branches: Mapping[str, str], now: float) -> bool:
    """Turn an ended VM's outbox into its repo's cache disk if the job that VM ran passed on
    a push to the default branch, so a branch or pull request cannot replace the cache. The
    header only says which run to ask about; GitHub's job list must name this VM's runner.
    Returns True to keep it for another try (GitHub does not show the job finished yet)."""
    runner = tar.stem
    try:
        fields = json.loads(tar.with_suffix(".json").read_text())
        repo = str(fields["repo"])
        with tar.open("rb") as f:
            first = f.read(512)
    except OSError, ValueError, KeyError, TypeError:
        return False
    header = OUTBOX_HEADER.match(first)
    if not header:
        return False  # nothing saved: most VMs end so
    branch = branches.get(repo)
    if not branch:
        log(f"cache for {repo}: outbox of {runner} dropped, the repo is not served")
        return False
    run_id, attempt = int(header[1]), int(header[2])
    image = cache_image(repo)
    seen = image.with_suffix(".json")
    known = json.loads(seen.read_text()) if seen.exists() else {}
    if (run_id, attempt) <= (int(known.get("run", 0)), int(known.get("attempt", 0))):
        return False  # not newer than the run the disk came from
    path = f"/repos/{repo}/actions/runs/{run_id}/attempts/{attempt}"
    run = gh.request("GET", path)
    jobs = gh.request("GET", f"{path}/jobs?per_page=100").get("jobs", [])
    job = next((j for j in jobs if j.get("runner_name") == runner), None)
    if not (job and run.get("event") == "push" and run.get("head_branch") == branch):
        log(f"cache for {repo}: outbox of {runner} is not from a push to {branch}; ignored")
        return False
    if job.get("status") != "completed":
        return now - tar.stat().st_mtime < OUTBOX_WAIT_SECONDS
    if job.get("conclusion") != "success":
        log(f"cache for {repo}: outbox of {runner} is from a job that did not pass; ignored")
        return False
    started = time.time()
    with tar.open("rb") as f:
        f.seek(512)
        try:
            build_cache_image(f, image)
        except BadCache as e:
            log(f"cache for {repo}: outbox of {runner} failed to unpack, {e}; ignored")
            return False
    seen.write_text(json.dumps({"run": run_id, "attempt": attempt, "sha": run.get("head_sha", "")}))
    log(f"cache for {repo}: built from run {run_id} in {time.time() - started:.0f}s")
    return False


def judge_outbox(gh: GitHubApi, tar: Path, branches: Mapping[str, str], now: float) -> bool:
    """take_outbox, deleting the outbox unless it is kept for another try. Only a failure that
    may clear (GitHub down or rate limited) keeps it: a VM could name a run that does not
    exist, and its 6 GiB would stay an hour."""
    try:
        keep = take_outbox(gh, tar, branches, now)
    except Exception as e:
        log(f"outbox {tar.name} failed: {type(e).__name__}: {e}")
        gone = isinstance(e, urllib.error.HTTPError) and e.code in (400, 404, 410, 422)
        keep = not gone and now - tar.stat().st_mtime < OUTBOX_WAIT_SECONDS
    if not keep:
        tar.unlink(missing_ok=True)
        tar.with_suffix(".json").unlink(missing_ok=True)
    return keep


def ended_outboxes(state: State, now: float) -> list[Path]:
    """The outboxes of VMs that have ended (each has its .json from launch), not tried in
    the last OUTBOX_RETRY_SECONDS."""
    running = {run.runner_name for run in state.launches.values()}
    return [
        record.with_suffix(".tar")
        for record in OUTBOX_DIR.glob(f"{github_api.RUNNER_PREFIX}*.json")
        if record.stem not in running
        and now - state.cache_checked.get(record.stem, 0) >= OUTBOX_RETRY_SECONDS
    ]


def maybe_take_outboxes(gh: GitHubApi, state: State, now: float) -> None:
    """Judge ended VMs' outboxes one at a time in the background, so polling never waits on
    a build. Each is deleted once judged; one GitHub cannot answer for is kept an hour."""
    due = ended_outboxes(state, now)
    if not due or not state.cache_busy.acquire(blocking=False):
        return
    branches = {r: p.default_branch for r, p in state.serving.items() if p.served}
    for tar in due:
        state.cache_checked[tar.stem] = now
    # Its own client: the request cache is not shared between threads.
    own = gh.clone()

    def work() -> None:
        try:
            for tar in due:
                if not judge_outbox(own, tar, branches, now):
                    state.cache_checked.pop(tar.stem, None)
        finally:
            state.cache_busy.release()

    threading.Thread(target=work, daemon=True).start()
