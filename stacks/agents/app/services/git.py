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
DEFAULT_REPO_LOCK_TIMEOUT_SECONDS = 120
REPO_LOCK_TIMEOUT_SECONDS = int(
    os.environ.get("REPO_LOCK_TIMEOUT_SECONDS", str(DEFAULT_REPO_LOCK_TIMEOUT_SECONDS))
)
DEFAULT_GIT_COMMAND_TIMEOUT_SECONDS = 300
GIT_COMMAND_TIMEOUT_SECONDS = int(
    os.environ.get("GIT_COMMAND_TIMEOUT_SECONDS", str(DEFAULT_GIT_COMMAND_TIMEOUT_SECONDS))
)
_REPO_FILE_LOCK_POLL_SECONDS = 0.1
REPO_LOCK_FILE = ".repo-setup.lock"

if WORKTREE_RETENTION_SECONDS < 0:
    raise ValueError("WORKTREE_RETENTION_SECONDS must be non-negative")
if REPO_LOCK_TIMEOUT_SECONDS <= 0:
    raise ValueError("REPO_LOCK_TIMEOUT_SECONDS must be positive")
if GIT_COMMAND_TIMEOUT_SECONDS <= 0:
    raise ValueError("GIT_COMMAND_TIMEOUT_SECONDS must be positive")

# Serializes git operations within a single worker process.
_repo_lock = asyncio.Lock()


@dataclass(frozen=True)
class CleanupMarker:
    expires_at: int
    branch: str


@dataclass(frozen=True)
class WorktreeDetails:
    path: Path
    branch: str | None


def _kill_git_process(proc: asyncio.subprocess.Process, cmd: list[str]) -> None:
    try:
        proc.kill()
    except ProcessLookupError:
        logger.debug("Git subprocess already exited before kill: %s", " ".join(cmd), exc_info=True)


async def _run(cmd: list[str], cwd: Path | None = None) -> str:
    """Run a command asynchronously, raising on failure."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=GIT_COMMAND_TIMEOUT_SECONDS
        )
    except asyncio.CancelledError:
        _kill_git_process(proc, cmd)
        await proc.communicate()
        raise
    except TimeoutError as err:
        _kill_git_process(proc, cmd)
        _, stderr = await proc.communicate()
        message = f"Git command timed out after {GIT_COMMAND_TIMEOUT_SECONDS}s: {' '.join(cmd)}"
        if details := stderr.decode().strip():
            message = f"{message}\n{details}"
        raise RuntimeError(message) from err

    if proc.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\n{stderr.decode()}")
    return stdout.decode().strip()


def _repo_lock_path() -> Path:
    # The shared /reviews volume exists before the bare clone does, so the
    # setup lock has to live here instead of inside /repo.git.
    return REVIEWS_PATH / REPO_LOCK_FILE


def _repo_lock_timeout_message() -> str:
    return (
        f"Git repo lock timed out after {REPO_LOCK_TIMEOUT_SECONDS}s; "
        "another git operation may be stuck"
    )


def _acquire_repo_file_lock(timeout_seconds: int) -> int:
    os.makedirs(REVIEWS_PATH, exist_ok=True)
    fd = os.open(_repo_lock_path(), os.O_RDWR | os.O_CREAT, 0o666)
    try:
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as err:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(_repo_lock_timeout_message()) from err
                time.sleep(min(_REPO_FILE_LOCK_POLL_SECONDS, remaining))
                continue
            return fd
    except Exception:
        os.close(fd)
        raise


def _release_repo_file_lock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _release_async_repo_lock_if_acquired(acquire_task: asyncio.Task[bool]) -> None:
    if not acquire_task.done() or acquire_task.cancelled():
        return

    try:
        if acquire_task.result():
            _repo_lock.release()
    except Exception:
        logger.debug("Failed to release in-process repo lock during cleanup", exc_info=True)


async def _acquire_in_process_repo_lock() -> None:
    acquire_task = asyncio.create_task(_repo_lock.acquire())
    try:
        await asyncio.wait_for(acquire_task, timeout=REPO_LOCK_TIMEOUT_SECONDS)
    except TimeoutError as err:
        _release_async_repo_lock_if_acquired(acquire_task)
        raise RuntimeError(_repo_lock_timeout_message()) from err
    except asyncio.CancelledError:
        _release_async_repo_lock_if_acquired(acquire_task)
        raise


async def _acquire_file_lock_cancellation_safe() -> int:
    """Acquire the cross-container file lock, handling cancellation safely.

    Uses asyncio.shield so a cancellation during acquisition still finishes
    the lock handshake, then releases if we were cancelled.
    """
    file_lock_task = asyncio.create_task(
        asyncio.to_thread(_acquire_repo_file_lock, REPO_LOCK_TIMEOUT_SECONDS)
    )
    try:
        return await asyncio.shield(file_lock_task)
    except TimeoutError as err:
        raise RuntimeError(_repo_lock_timeout_message()) from err
    except asyncio.CancelledError:
        # The file lock acquisition may still be in progress — wait for it
        # to finish so we can release it cleanly.
        try:
            fd = await file_lock_task
        except Exception:
            logger.warning(
                "Failed to finish repo lock acquisition during cancellation cleanup",
                exc_info=True,
            )
            raise
        try:
            await asyncio.shield(asyncio.to_thread(_release_repo_file_lock, fd))
        except Exception:
            logger.warning(
                "Failed to release repo lock during cancellation cleanup",
                exc_info=True,
            )
        raise


@contextlib.asynccontextmanager
async def _hold_repo_lock() -> AsyncIterator[None]:
    """Serialize shared bare-clone mutations across tasks and containers."""
    await _acquire_in_process_repo_lock()
    try:
        fd = await _acquire_file_lock_cancellation_safe()
        try:
            yield
        finally:
            await asyncio.shield(asyncio.to_thread(_release_repo_file_lock, fd))
    finally:
        _repo_lock.release()


async def init_bare_clone(repo_url: str) -> Path:
    """Initialize or update the bare clone used as an object store."""
    head_file = BARE_CLONE_PATH / "HEAD"
    if head_file.exists():
        # Prune stale worktree refs (e.g. after container restart with persistent volume)
        try:
            await _run(["git", "worktree", "prune"], cwd=BARE_CLONE_PATH)
        except RuntimeError:
            logger.warning(
                "Failed to prune stale git worktrees in %s", BARE_CLONE_PATH, exc_info=True
            )
        await _remove_main_worktrees()
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


def _parse_worktree_list(raw_worktrees: str) -> list[WorktreeDetails]:
    worktrees: list[WorktreeDetails] = []
    current_path: Path | None = None
    current_branch: str | None = None

    for line in raw_worktrees.splitlines():
        if not line:
            if current_path is not None:
                worktrees.append(WorktreeDetails(path=current_path, branch=current_branch))
            current_path = None
            current_branch = None
            continue

        key, _, value = line.partition(" ")
        if key == "worktree" and value:
            if current_path is not None:
                worktrees.append(WorktreeDetails(path=current_path, branch=current_branch))
            current_path = Path(value)
            current_branch = None
        elif key == "branch" and value:
            current_branch = value

    if current_path is not None:
        worktrees.append(WorktreeDetails(path=current_path, branch=current_branch))

    return worktrees


async def _remove_main_worktrees() -> None:
    """Remove stale worktrees that still have refs/heads/main checked out."""
    raw_worktrees = await _run(["git", "worktree", "list", "--porcelain"], cwd=BARE_CLONE_PATH)
    for worktree in _parse_worktree_list(raw_worktrees):
        if worktree.branch != "refs/heads/main":
            continue
        logger.info("Removing stale main worktree at %s", worktree.path)
        await _run(
            ["git", "worktree", "remove", "--force", str(worktree.path)],
            cwd=BARE_CLONE_PATH,
        )


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
        try:
            await _run(["git", "branch", "-D", f"pr-{pr_number}"], cwd=BARE_CLONE_PATH)
        except RuntimeError:
            logger.debug(
                "Skipping delete of missing review branch pr-%d",
                pr_number,
                exc_info=True,
            )

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

        try:
            await _run(["git", "branch", "-D", branch_name], cwd=BARE_CLONE_PATH)
        except RuntimeError:
            logger.debug(
                "Skipping delete of missing branch worktree ref %s",
                branch_name,
                exc_info=True,
            )

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

    try:
        await _run(["git", "worktree", "prune"], cwd=BARE_CLONE_PATH)
    except RuntimeError:
        logger.warning("Failed to prune git worktrees during cleanup", exc_info=True)

    try:
        await _run(["git", "branch", "-D", branch_name], cwd=BARE_CLONE_PATH)
    except RuntimeError:
        logger.debug("Skipping delete of missing branch ref %s", branch_name, exc_info=True)


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
