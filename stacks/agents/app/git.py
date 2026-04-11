"""Git operations — bare clone, worktrees, branches, and push."""

import asyncio
import contextlib
import logging
import shutil
from pathlib import Path

from github import bot_email, bot_login

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


async def create_branch_worktree(branch_name: str, repo_url: str) -> Path:
    """Create a new branch from origin/main and a worktree for it."""
    worktree_path = REVIEWS_PATH / branch_name.replace("/", "-")

    async with _repo_lock:
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

    logger.info("Created branch worktree %s at %s", branch_name, worktree_path)
    return worktree_path


async def cleanup_branch_worktree(branch_name: str) -> None:
    """Remove a branch worktree and its branch ref."""
    worktree_path = REVIEWS_PATH / branch_name.replace("/", "-")

    async with _repo_lock:
        await _remove_named_worktree(worktree_path, branch_name)

    logger.info("Cleaned up branch worktree %s", branch_name)


async def commit_and_push(
    worktree_path: Path, *, message: str, token: str, repo: str, branch: str
) -> str:
    """Stage all changes, commit, and push to the remote branch. Returns commit SHA."""
    await _run(["git", "add", "-A"], cwd=worktree_path)

    # git diff --cached --quiet returns 0 if NO changes staged
    proc = await asyncio.create_subprocess_exec(
        "git",
        "diff",
        "--cached",
        "--quiet",
        cwd=worktree_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()
    if proc.returncode == 0:
        raise RuntimeError("No changes to commit")

    await _run(
        [
            "git",
            "-c",
            f"user.name={bot_login()}",
            "-c",
            f"user.email={bot_email()}",
            "commit",
            "-m",
            message,
        ],
        cwd=worktree_path,
    )

    sha = await _run(["git", "rev-parse", "HEAD"], cwd=worktree_path)

    # Push using GIT_ASKPASS to keep the token out of command args and error messages
    askpass_script = worktree_path / ".git-askpass.sh"
    askpass_script.write_text(f"#!/bin/sh\necho '{token}'\n")
    askpass_script.chmod(0o700)
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "push",
            f"https://x-access-token@github.com/{repo}.git",
            f"HEAD:refs/heads/{branch}",
            "--force-with-lease",
            cwd=worktree_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**__import__("os").environ, "GIT_ASKPASS": str(askpass_script)},
        )
        _stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"git push failed (exit {proc.returncode})\n{stderr.decode()}")
    finally:
        askpass_script.unlink(missing_ok=True)

    logger.info("Pushed %s to %s/%s", sha[:8], repo, branch)
    return sha


async def _remove_named_worktree(worktree_path: Path, branch_name: str) -> None:
    """Remove a worktree and its branch ref. Caller must hold _repo_lock."""
    if worktree_path.exists():
        try:
            await _run(
                ["git", "worktree", "remove", "--force", str(worktree_path)],
                cwd=BARE_CLONE_PATH,
            )
        except RuntimeError:
            shutil.rmtree(worktree_path, ignore_errors=True)

    with contextlib.suppress(RuntimeError):
        await _run(["git", "worktree", "prune"], cwd=BARE_CLONE_PATH)

    with contextlib.suppress(RuntimeError):
        await _run(["git", "branch", "-D", branch_name], cwd=BARE_CLONE_PATH)


async def _remove_worktree(worktree_path: Path, pr_number: int) -> None:
    """Remove a PR worktree directory and its branch ref. Caller must hold _repo_lock."""
    await _remove_named_worktree(worktree_path, f"pr-{pr_number}")
