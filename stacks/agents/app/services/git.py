"""Git operations — bare clone, worktrees, and cleanup."""

import asyncio
import contextlib
import fcntl
import json
import logging
import os
import shutil
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

BARE_CLONE_PATH = Path("/repo.git")
REVIEWS_PATH = Path("/reviews")
CLEANUP_MARKER_FILE = ".cleanup-after"
DEFAULT_WORKTREE_RETENTION_SECONDS = 14 * 86400
WORKTREE_RETENTION_SECONDS = int(
    os.environ.get("WORKTREE_RETENTION_SECONDS", str(DEFAULT_WORKTREE_RETENTION_SECONDS))
)
REPO_LOCK_FILE = ".repo-setup.lock"

if WORKTREE_RETENTION_SECONDS < 0:
    raise ValueError("WORKTREE_RETENTION_SECONDS must be non-negative")

# Serializes git operations within a single worker process.
_repo_lock = asyncio.Lock()


@dataclass(frozen=True)
class CleanupMarker:
    expires_at: int
    branch: str


async def _run(cmd: list[str], cwd: Path | None = None) -> str:
    """Run a command asynchronously, raising on failure."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\n{stderr.decode()}")
    return stdout.decode().strip()


def _repo_lock_path() -> Path:
    # The shared /reviews volume exists before the bare clone does, so the
    # setup lock has to live here instead of inside /repo.git.
    return REVIEWS_PATH / REPO_LOCK_FILE


def _acquire_repo_file_lock() -> int:
    os.makedirs(REVIEWS_PATH, exist_ok=True)
    fd = os.open(_repo_lock_path(), os.O_RDWR | os.O_CREAT, 0o666)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
    except Exception:
        os.close(fd)
        raise
    return fd


def _release_repo_file_lock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@contextlib.asynccontextmanager
async def _hold_repo_lock() -> AsyncIterator[None]:
    """Serialize shared bare-clone mutations across tasks and containers."""
    async with _repo_lock:
        fd = await asyncio.to_thread(_acquire_repo_file_lock)
        try:
            yield
        finally:
            await asyncio.to_thread(_release_repo_file_lock, fd)


async def init_bare_clone(repo_url: str) -> Path:
    """Initialize or update the bare clone used as an object store."""
    head_file = BARE_CLONE_PATH / "HEAD"
    if head_file.exists():
        # Prune stale worktree refs (e.g. after container restart with persistent volume)
        with contextlib.suppress(RuntimeError):
            await _run(["git", "worktree", "prune"], cwd=BARE_CLONE_PATH)
        # Bare clones have no default refspec, so fetch main explicitly to keep
        # the base ref current. Only fetch main — fetching all of refs/heads/*
        # would prune local-only worktree branches (agent/issue-*, pr-*).
        await _run(
            ["git", "fetch", "origin", "+refs/heads/main:refs/heads/main"],
            cwd=BARE_CLONE_PATH,
        )
    else:
        BARE_CLONE_PATH.parent.mkdir(parents=True, exist_ok=True)
        await _run(["git", "clone", "--bare", repo_url, str(BARE_CLONE_PATH)])
    return BARE_CLONE_PATH


_FETCH_BACKOFF_SECONDS = [2, 4, 8]


async def create_worktree(pr_number: int, repo_url: str, *, head_ref: str | None = None) -> Path:
    """Fetch a PR ref and create a worktree for review.

    Args:
        pr_number: PR number (used for worktree naming and fallback fetch).
        repo_url: Repository clone URL.
        head_ref: Actual branch name (e.g. "agent/issue-42"). If provided,
            fetches this directly instead of the synthetic pull/N/head ref,
            which avoids GitHub's ref propagation delay.
    """
    worktree_path = REVIEWS_PATH / f"pr-{pr_number}"

    async with _hold_repo_lock():
        await _reap_old_worktrees_locked()

        if worktree_path.exists():
            await _remove_worktree(worktree_path, pr_number)

        await init_bare_clone(repo_url)

        # Force-update the branch ref (handles stale refs from previous container)
        with contextlib.suppress(RuntimeError):
            await _run(["git", "branch", "-D", f"pr-{pr_number}"], cwd=BARE_CLONE_PATH)

        # Prefer the actual branch ref — it's available immediately after push.
        # Fall back to pull/N/head (synthetic ref with propagation delay) when
        # the caller doesn't know the branch name.
        source_ref = head_ref or f"pull/{pr_number}/head"
        fetch_cmd = ["git", "fetch", "origin", f"{source_ref}:pr-{pr_number}"]

        # Retry with backoff: initial attempt + one retry per backoff interval.
        max_attempts = len(_FETCH_BACKOFF_SECONDS) + 1
        for attempt in range(max_attempts):
            try:
                await _run(fetch_cmd, cwd=BARE_CLONE_PATH)
                break
            except RuntimeError:
                if attempt == max_attempts - 1:
                    raise
                delay = _FETCH_BACKOFF_SECONDS[attempt]
                logger.warning(
                    "Fetch %s failed (attempt %d/%d), retrying in %ds",
                    source_ref,
                    attempt + 1,
                    max_attempts,
                    delay,
                )
                await asyncio.sleep(delay)

        REVIEWS_PATH.mkdir(parents=True, exist_ok=True)
        await _run(
            ["git", "worktree", "add", str(worktree_path), f"pr-{pr_number}"],
            cwd=BARE_CLONE_PATH,
        )
        # Start retention tracking immediately so crash-orphaned worktrees
        # can still age out even if graceful cleanup never runs.
        _mark_worktree_for_cleanup(worktree_path, f"pr-{pr_number}")

    logger.info("Created worktree for PR #%d at %s", pr_number, worktree_path)
    return worktree_path


async def cleanup_worktree(pr_number: int) -> None:
    """Mark a PR worktree for deferred cleanup."""
    worktree_path = REVIEWS_PATH / f"pr-{pr_number}"

    async with _hold_repo_lock():
        _mark_worktree_for_cleanup(worktree_path, f"pr-{pr_number}")
        await _reap_old_worktrees_locked()

    logger.info("Deferred cleanup for PR #%d worktree at %s", pr_number, worktree_path)


async def create_branch_worktree(branch_name: str, repo_url: str) -> Path:
    """Create a new branch from origin/main and a worktree for it."""
    worktree_path = REVIEWS_PATH / branch_name.replace("/", "-")

    async with _hold_repo_lock():
        await _reap_old_worktrees_locked()

        if worktree_path.exists():
            await _remove_named_worktree(worktree_path, branch_name)

        await init_bare_clone(repo_url)

        with contextlib.suppress(RuntimeError):
            await _run(["git", "branch", "-D", branch_name], cwd=BARE_CLONE_PATH)

        # In a bare clone, fetch writes directly to refs/heads/ (no remote-tracking
        # branches), so the ref is "main" not "origin/main".
        await _run(
            ["git", "branch", branch_name, "main"],
            cwd=BARE_CLONE_PATH,
        )

        REVIEWS_PATH.mkdir(parents=True, exist_ok=True)
        await _run(
            ["git", "worktree", "add", str(worktree_path), branch_name],
            cwd=BARE_CLONE_PATH,
        )
        # Start retention tracking immediately so crash-orphaned worktrees
        # can still age out even if graceful cleanup never runs.
        _mark_worktree_for_cleanup(worktree_path, branch_name)

    logger.info("Created branch worktree %s at %s", branch_name, worktree_path)
    return worktree_path


async def cleanup_branch_worktree(branch_name: str) -> None:
    """Mark a branch worktree for deferred cleanup."""
    worktree_path = REVIEWS_PATH / branch_name.replace("/", "-")

    async with _hold_repo_lock():
        _mark_worktree_for_cleanup(worktree_path, branch_name)
        await _reap_old_worktrees_locked()

    logger.info("Deferred cleanup for branch worktree %s", branch_name)


async def reap_old_worktrees() -> None:
    """Delete worktrees whose cleanup retention window has expired."""
    async with _hold_repo_lock():
        await _reap_old_worktrees_locked()


async def _remove_named_worktree(worktree_path: Path, branch_name: str) -> None:
    """Remove a worktree and its branch ref. Caller must hold the repo lock."""
    bare_clone_exists = (BARE_CLONE_PATH / "HEAD").exists()

    if worktree_path.exists():
        if bare_clone_exists:
            try:
                await _run(
                    ["git", "worktree", "remove", "--force", str(worktree_path)],
                    cwd=BARE_CLONE_PATH,
                )
            except RuntimeError:
                shutil.rmtree(worktree_path, ignore_errors=True)
        else:
            shutil.rmtree(worktree_path, ignore_errors=True)

    if not bare_clone_exists:
        return

    with contextlib.suppress(RuntimeError):
        await _run(["git", "worktree", "prune"], cwd=BARE_CLONE_PATH)

    with contextlib.suppress(RuntimeError):
        await _run(["git", "branch", "-D", branch_name], cwd=BARE_CLONE_PATH)


async def _remove_worktree(worktree_path: Path, pr_number: int) -> None:
    """Remove a PR worktree directory and its branch ref. Caller must hold the repo lock."""
    await _remove_named_worktree(worktree_path, f"pr-{pr_number}")


def _cleanup_marker_path(worktree_path: Path) -> Path:
    return worktree_path / CLEANUP_MARKER_FILE


def _mark_worktree_for_cleanup(worktree_path: Path, branch_name: str) -> None:
    """Write a marker file so the reaper can delete this worktree later."""
    if not worktree_path.exists():
        return

    expires_at = int(time.time()) + WORKTREE_RETENTION_SECONDS
    _cleanup_marker_path(worktree_path).write_text(
        json.dumps({"expires_at": expires_at, "branch": branch_name}) + "\n"
    )
    logger.info(
        "Marked worktree %s for cleanup at %d (branch=%s)",
        worktree_path,
        expires_at,
        branch_name,
    )


def _read_cleanup_marker(worktree_path: Path) -> CleanupMarker | None:
    marker_path = _cleanup_marker_path(worktree_path)
    if not marker_path.exists():
        return None

    try:
        raw_marker = json.loads(marker_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Skipping invalid cleanup marker at %s: %s", marker_path, exc)
        return None

    if not isinstance(raw_marker, dict):
        logger.warning("Skipping invalid cleanup marker at %s: expected object", marker_path)
        return None

    expires_at = raw_marker.get("expires_at")
    branch = raw_marker.get("branch")
    if not isinstance(expires_at, int) or not isinstance(branch, str) or not branch:
        logger.warning(
            "Skipping invalid cleanup marker at %s: expected expires_at=int and branch=str",
            marker_path,
        )
        return None

    return CleanupMarker(expires_at=expires_at, branch=branch)


async def _reap_old_worktrees_locked() -> None:
    """Delete expired worktrees. Caller must hold the repo lock."""
    if not REVIEWS_PATH.exists():
        return

    now = int(time.time())
    for worktree_path in REVIEWS_PATH.iterdir():
        if not worktree_path.is_dir():
            continue

        marker = _read_cleanup_marker(worktree_path)
        if marker is None or marker.expires_at > now:
            continue

        try:
            await _remove_named_worktree(worktree_path, marker.branch)
            logger.info("Reaped expired worktree %s (branch=%s)", worktree_path, marker.branch)
        except Exception:
            logger.exception("Failed to reap expired worktree %s", worktree_path)
