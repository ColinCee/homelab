"""Tests for git operations."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import git as git_module


class TestRunCommand:
    def test_raises_on_failure(self):
        async def run():
            with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_proc:
                proc = AsyncMock()
                proc.communicate.return_value = (b"", b"error message")
                proc.returncode = 1
                mock_proc.return_value = proc

                with __import__("pytest").raises(RuntimeError, match="Command failed"):
                    await git_module._run(["git", "status"])

        asyncio.run(run())


class TestCreateWorktree:
    def test_creates_worktree_for_pr(self):
        calls = []

        async def mock_run(cmd, cwd=None):
            calls.append((cmd, cwd))
            return ""

        async def run():
            with (
                patch.object(git_module, "_run", side_effect=mock_run),
                patch.object(git_module, "reap_old_worktrees", new=AsyncMock(return_value=0)),
                patch.object(git_module, "BARE_CLONE_PATH", Path("/tmp/test-repo.git")),
                patch.object(git_module, "REVIEWS_PATH", Path("/tmp/test-reviews")),
                patch("pathlib.Path.exists", return_value=False),
                patch("pathlib.Path.mkdir"),
            ):
                path = await git_module.create_worktree(42, "https://github.com/user/repo.git")

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
        # 3-4: BARE_CLONE_PATH.exists() in _remove_named_worktree → True
        # 5: head_file.exists() in init_bare_clone → False (fresh clone)
        exists_calls = iter([True, True, True, True, False])

        async def run():
            with (
                patch.object(git_module, "_run", side_effect=mock_run),
                patch.object(git_module, "reap_old_worktrees", new=AsyncMock(return_value=0)),
                patch.object(git_module, "BARE_CLONE_PATH", Path("/tmp/test-repo.git")),
                patch.object(git_module, "REVIEWS_PATH", Path("/tmp/test-reviews")),
                patch("pathlib.Path.exists", side_effect=lambda *a: next(exists_calls)),
                patch("pathlib.Path.mkdir"),
            ):
                path = await git_module.create_worktree(42, "https://github.com/user/repo.git")

            assert path == Path("/tmp/test-reviews/pr-42")
            assert any("worktree" in cmd and "remove" in cmd for cmd, _ in calls)

        asyncio.run(run())


class TestCleanupWorktree:
    def test_defers_pr_cleanup_with_marker(self, tmp_path):
        worktree_path = tmp_path / "pr-42"
        worktree_path.mkdir()

        async def run():
            with (
                patch.object(git_module, "REVIEWS_PATH", tmp_path),
                patch.object(git_module, "WORKTREE_RETENTION_SECONDS", 60),
                patch.object(git_module, "reap_old_worktrees", new=AsyncMock(return_value=0)),
                patch("git.time.time", return_value=1_700_000_000),
            ):
                await git_module.cleanup_worktree(42)

        asyncio.run(run())

        marker = json.loads((worktree_path / git_module.CLEANUP_MARKER_FILE).read_text())
        assert marker == {"expires_at": 1_700_000_060, "branch_name": "pr-42"}

    def test_defers_branch_cleanup_with_original_branch_name(self, tmp_path):
        worktree_path = tmp_path / "agent-issue-59"
        worktree_path.mkdir()

        async def run():
            with (
                patch.object(git_module, "REVIEWS_PATH", tmp_path),
                patch.object(git_module, "WORKTREE_RETENTION_SECONDS", 120),
                patch.object(git_module, "reap_old_worktrees", new=AsyncMock(return_value=0)),
                patch("git.time.time", return_value=1_700_000_000),
            ):
                await git_module.cleanup_branch_worktree("agent/issue-59")

        asyncio.run(run())

        marker = json.loads((worktree_path / git_module.CLEANUP_MARKER_FILE).read_text())
        assert marker == {"expires_at": 1_700_000_120, "branch_name": "agent/issue-59"}

    def test_removes_ref_immediately_when_worktree_path_is_missing(self, tmp_path):
        async def run():
            with (
                patch.object(git_module, "REVIEWS_PATH", tmp_path),
                patch.object(git_module, "reap_old_worktrees", new=AsyncMock(return_value=0)),
                patch.object(git_module, "_remove_named_worktree", new=AsyncMock()) as mock_remove,
            ):
                await git_module.cleanup_worktree(42)

            mock_remove.assert_awaited_once_with(tmp_path / "pr-42", "pr-42")

        asyncio.run(run())


class TestReapOldWorktrees:
    def test_reaps_only_expired_markers(self, tmp_path):
        expired = tmp_path / "pr-1"
        expired.mkdir()
        (expired / git_module.CLEANUP_MARKER_FILE).write_text(
            json.dumps({"expires_at": 1_700_000_000, "branch_name": "pr-1"})
        )

        fresh = tmp_path / "pr-2"
        fresh.mkdir()
        (fresh / git_module.CLEANUP_MARKER_FILE).write_text(
            json.dumps({"expires_at": 1_700_000_100, "branch_name": "pr-2"})
        )

        async def run():
            with (
                patch.object(git_module, "REVIEWS_PATH", tmp_path),
                patch.object(git_module, "_remove_named_worktree", new=AsyncMock()) as mock_remove,
                patch("git.time.time", return_value=1_700_000_050),
            ):
                reaped = await git_module.reap_old_worktrees()

            assert reaped == 1
            mock_remove.assert_awaited_once_with(expired, "pr-1")

        asyncio.run(run())

    def test_skips_malformed_markers(self, tmp_path):
        broken = tmp_path / "pr-9"
        broken.mkdir()
        (broken / git_module.CLEANUP_MARKER_FILE).write_text("{not-json}")

        async def run():
            with (
                patch.object(git_module, "REVIEWS_PATH", tmp_path),
                patch.object(git_module, "_remove_named_worktree", new=AsyncMock()) as mock_remove,
            ):
                reaped = await git_module.reap_old_worktrees()

            assert reaped == 0
            mock_remove.assert_not_called()

        asyncio.run(run())
