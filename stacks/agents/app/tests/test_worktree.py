"""Tests for git worktree management."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import worktree


class TestRunCommand:
    def test_raises_on_failure(self):
        async def run():
            with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_proc:
                proc = AsyncMock()
                proc.communicate.return_value = (b"", b"error message")
                proc.returncode = 1
                mock_proc.return_value = proc

                with __import__("pytest").raises(RuntimeError, match="Command failed"):
                    await worktree._run(["git", "status"])

        asyncio.run(run())


class TestCreateWorktree:
    def test_creates_worktree_for_pr(self):
        calls = []

        async def mock_run(cmd, cwd=None):
            calls.append((cmd, cwd))
            return ""

        async def run():
            with (
                patch.object(worktree, "_run", side_effect=mock_run),
                patch.object(worktree, "BARE_CLONE_PATH", Path("/tmp/test-repo.git")),
                patch.object(worktree, "REVIEWS_PATH", Path("/tmp/test-reviews")),
                patch("pathlib.Path.exists", return_value=False),
                patch("pathlib.Path.mkdir"),
            ):
                path = await worktree.create_worktree(42, "https://github.com/user/repo.git")

            assert path == Path("/tmp/test-reviews/pr-42")
            # Should have: clone, branch -D (cleanup stale ref), fetch PR, add worktree
            assert len(calls) == 4
            assert "clone" in calls[0][0][1]
            assert "branch" in calls[1][0][1]
            assert "pull/42/head:pr-42" in calls[2][0][3]
            assert "worktree" in calls[3][0][1]

        asyncio.run(run())

    def test_cleans_stale_worktree_without_deadlock(self):
        """Recreating a worktree for the same PR must not deadlock."""
        calls = []

        async def mock_run(cmd, cwd=None):
            calls.append((cmd, cwd))
            return ""

        # 1: worktree_path.exists() in create_worktree → True (stale)
        # 2: worktree_path.exists() in _remove_worktree → True (clean it)
        # 3: head_file.exists() in init_bare_clone → False (fresh clone)
        exists_calls = iter([True, True, False])

        async def run():
            with (
                patch.object(worktree, "_run", side_effect=mock_run),
                patch.object(worktree, "BARE_CLONE_PATH", Path("/tmp/test-repo.git")),
                patch.object(worktree, "REVIEWS_PATH", Path("/tmp/test-reviews")),
                patch("pathlib.Path.exists", side_effect=lambda *a: next(exists_calls)),
                patch("pathlib.Path.mkdir"),
            ):
                path = await worktree.create_worktree(42, "https://github.com/user/repo.git")

            assert path == Path("/tmp/test-reviews/pr-42")
            assert any("worktree" in cmd and "remove" in cmd for cmd, _ in calls)

        asyncio.run(run())
