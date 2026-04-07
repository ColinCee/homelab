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
            # Should have: clone, fetch PR, add worktree
            assert len(calls) == 3
            assert "clone" in calls[0][0][1]
            assert "pull/42/head:pr-42" in calls[1][0][3]
            assert "worktree" in calls[2][0][1]

        asyncio.run(run())
