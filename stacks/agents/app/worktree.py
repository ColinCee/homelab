"""Git worktree management for PR reviews."""

import asyncio
import contextlib
import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

BARE_CLONE_PATH = Path("/repo.git")
REVIEWS_PATH = Path("/reviews")

# Serializes all git operations on the shared bare clone
_repo_lock = asyncio.Lock()


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


async def init_bare_clone(repo_url: str) -> Path:
    """Initialize or update the bare clone used as an object store."""
    head_file = BARE_CLONE_PATH / "HEAD"
    if head_file.exists():
        # Prune stale worktree refs (e.g. after container restart with persistent volume)
        with contextlib.suppress(RuntimeError):
            await _run(["git", "worktree", "prune"], cwd=BARE_CLONE_PATH)
        await _run(["git", "fetch", "--all", "--prune"], cwd=BARE_CLONE_PATH)
    else:
        BARE_CLONE_PATH.parent.mkdir(parents=True, exist_ok=True)
        await _run(["git", "clone", "--bare", repo_url, str(BARE_CLONE_PATH)])
    return BARE_CLONE_PATH


async def create_worktree(pr_number: int, repo_url: str) -> Path:
    """Fetch a PR ref and create a worktree for review."""
    worktree_path = REVIEWS_PATH / f"pr-{pr_number}"

    async with _repo_lock:
        if worktree_path.exists():
            await _remove_worktree(worktree_path, pr_number)

        await init_bare_clone(repo_url)

        # Force-update the branch ref (handles stale refs from previous container)
        with contextlib.suppress(RuntimeError):
            await _run(["git", "branch", "-D", f"pr-{pr_number}"], cwd=BARE_CLONE_PATH)

        await _run(
            ["git", "fetch", "origin", f"pull/{pr_number}/head:pr-{pr_number}"],
            cwd=BARE_CLONE_PATH,
        )

        REVIEWS_PATH.mkdir(parents=True, exist_ok=True)
        await _run(
            ["git", "worktree", "add", str(worktree_path), f"pr-{pr_number}"],
            cwd=BARE_CLONE_PATH,
        )

    logger.info("Created worktree for PR #%d at %s", pr_number, worktree_path)
    return worktree_path


async def cleanup_worktree(pr_number: int) -> None:
    """Remove a PR worktree and its branch ref."""
    worktree_path = REVIEWS_PATH / f"pr-{pr_number}"

    async with _repo_lock:
        await _remove_worktree(worktree_path, pr_number)

    logger.info("Cleaned up worktree for PR #%d", pr_number)


async def _remove_worktree(worktree_path: Path, pr_number: int) -> None:
    """Remove a worktree directory and its branch ref. Caller must hold _repo_lock."""
    if worktree_path.exists():
        try:
            await _run(
                ["git", "worktree", "remove", "--force", str(worktree_path)],
                cwd=BARE_CLONE_PATH,
            )
        except RuntimeError:
            shutil.rmtree(worktree_path, ignore_errors=True)

    # Clean up stale worktree references (e.g. after directory was removed manually)
    with contextlib.suppress(RuntimeError):
        await _run(["git", "worktree", "prune"], cwd=BARE_CLONE_PATH)

    with contextlib.suppress(RuntimeError):
        await _run(["git", "branch", "-D", f"pr-{pr_number}"], cwd=BARE_CLONE_PATH)
