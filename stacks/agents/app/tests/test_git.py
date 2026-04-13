"""Tests for git operations."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

import services.git as git_module


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

    def test_marks_new_pr_worktree_for_cleanup(self):
        async def mock_run(cmd, cwd=None):
            return ""

        async def run():
            with (
                patch.object(git_module, "_run", side_effect=mock_run),
                patch.object(git_module, "_mark_worktree_for_cleanup") as mock_mark,
                patch.object(git_module, "BARE_CLONE_PATH", Path("/tmp/test-repo.git")),
                patch.object(git_module, "REVIEWS_PATH", Path("/tmp/test-reviews")),
                patch("pathlib.Path.exists", return_value=False),
                patch("pathlib.Path.mkdir"),
            ):
                path = await git_module.create_worktree(42, "https://github.com/user/repo.git")

            assert path == Path("/tmp/test-reviews/pr-42")
            mock_mark.assert_called_once_with(Path("/tmp/test-reviews/pr-42"), "pr-42")

        asyncio.run(run())

    def test_reaps_expired_worktrees_before_creating(self):
        calls = []

        async def mock_run(cmd, cwd=None):
            calls.append((cmd, cwd))
            return ""

        async def run():
            with (
                patch.object(git_module, "_run", side_effect=mock_run),
                patch.object(
                    git_module, "_reap_old_worktrees_locked", new_callable=AsyncMock
                ) as mock_reap,
                patch.object(git_module, "BARE_CLONE_PATH", Path("/tmp/test-repo.git")),
                patch.object(git_module, "REVIEWS_PATH", Path("/tmp/test-reviews")),
                patch("pathlib.Path.exists", return_value=False),
                patch("pathlib.Path.mkdir"),
            ):
                await git_module.create_worktree(42, "https://github.com/user/repo.git")

            mock_reap.assert_awaited_once()

        asyncio.run(run())

    def test_cleans_stale_worktree_without_deadlock(self):
        """Recreating a worktree for the same PR must not deadlock."""

        async def mock_run(cmd, cwd=None):
            return ""

        async def run():
            with (
                patch.object(git_module, "_run", side_effect=mock_run),
                patch.object(git_module, "_reap_old_worktrees_locked", new_callable=AsyncMock),
                patch.object(git_module, "_mark_worktree_for_cleanup"),
                patch.object(git_module, "_remove_worktree", new_callable=AsyncMock) as mock_remove,
                patch.object(git_module, "BARE_CLONE_PATH", Path("/tmp/test-repo.git")),
                patch.object(git_module, "REVIEWS_PATH", Path("/tmp/test-reviews")),
                patch("pathlib.Path.exists", side_effect=[True, False]),
                patch("pathlib.Path.mkdir"),
            ):
                path = await git_module.create_worktree(42, "https://github.com/user/repo.git")

            assert path == Path("/tmp/test-reviews/pr-42")
            mock_remove.assert_awaited_once_with(Path("/tmp/test-reviews/pr-42"), 42)

        asyncio.run(run())

    def test_retries_fetch_on_failure(self):
        """Fetch retries with backoff when PR ref isn't available yet."""
        calls = []
        attempt = 0

        async def mock_run(cmd, cwd=None):
            nonlocal attempt
            calls.append(cmd)
            if "fetch" in cmd and "pull/" in " ".join(cmd):
                attempt += 1
                if attempt < 4:
                    raise RuntimeError("couldn't find remote ref")
            return ""

        async def run():
            with (
                patch.object(git_module, "_run", side_effect=mock_run),
                patch.object(git_module, "BARE_CLONE_PATH", Path("/tmp/test-repo.git")),
                patch.object(git_module, "REVIEWS_PATH", Path("/tmp/test-reviews")),
                patch("pathlib.Path.exists", return_value=False),
                patch("pathlib.Path.mkdir"),
                patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
            ):
                path = await git_module.create_worktree(42, "https://github.com/user/repo.git")

            assert path == Path("/tmp/test-reviews/pr-42")
            fetch_calls = [c for c in calls if "fetch" in c and "pull/" in " ".join(c)]
            assert len(fetch_calls) == 4
            assert mock_sleep.call_count == 3
            mock_sleep.assert_any_call(2)
            mock_sleep.assert_any_call(4)
            mock_sleep.assert_any_call(8)

        asyncio.run(run())

    def test_fetch_raises_after_all_retries_exhausted(self):
        """Fetch raises after exhausting all retry attempts."""

        async def mock_run(cmd, cwd=None):
            if "fetch" in cmd and "pull/" in " ".join(cmd):
                raise RuntimeError("couldn't find remote ref")
            return ""

        async def run():
            with (
                patch.object(git_module, "_run", side_effect=mock_run),
                patch.object(git_module, "BARE_CLONE_PATH", Path("/tmp/test-repo.git")),
                patch.object(git_module, "REVIEWS_PATH", Path("/tmp/test-reviews")),
                patch("pathlib.Path.exists", return_value=False),
                patch("pathlib.Path.mkdir"),
                patch("asyncio.sleep", new_callable=AsyncMock),
                pytest.raises(RuntimeError, match="couldn't find remote ref"),
            ):
                await git_module.create_worktree(42, "https://github.com/user/repo.git")

        asyncio.run(run())

    def test_fetches_head_ref_when_provided(self):
        """When head_ref is given, fetch the actual branch instead of pull/N/head."""
        calls = []

        async def mock_run(cmd, cwd=None):
            calls.append(cmd)
            return ""

        async def run():
            with (
                patch.object(git_module, "_run", side_effect=mock_run),
                patch.object(git_module, "BARE_CLONE_PATH", Path("/tmp/test-repo.git")),
                patch.object(git_module, "REVIEWS_PATH", Path("/tmp/test-reviews")),
                patch("pathlib.Path.exists", return_value=False),
                patch("pathlib.Path.mkdir"),
            ):
                await git_module.create_worktree(
                    42, "https://github.com/user/repo.git", head_ref="agent/issue-42"
                )

            fetch_calls = [c for c in calls if "fetch" in c]
            assert len(fetch_calls) == 1
            assert "agent/issue-42:pr-42" in fetch_calls[0][3]
            assert "pull/" not in fetch_calls[0][3]

        asyncio.run(run())


class TestDeferredCleanup:
    def test_cleanup_worktree_writes_marker(self, tmp_path: Path):
        worktree = tmp_path / "pr-42"
        worktree.mkdir()

        async def run():
            with (
                patch.object(git_module, "REVIEWS_PATH", tmp_path),
                patch.object(git_module, "WORKTREE_RETENTION_SECONDS", 3600),
                patch("services.git.time.time", return_value=1_700_000_000),
            ):
                await git_module.cleanup_worktree(42)

        asyncio.run(run())

        marker = json.loads((worktree / ".cleanup-after").read_text())
        assert marker == {"expires_at": 1_700_003_600, "branch": "pr-42"}

    def test_cleanup_branch_worktree_writes_branch_marker(self, tmp_path: Path):
        worktree = tmp_path / "agent-issue-42"
        worktree.mkdir()

        async def run():
            with (
                patch.object(git_module, "REVIEWS_PATH", tmp_path),
                patch.object(git_module, "WORKTREE_RETENTION_SECONDS", 120),
                patch("services.git.time.time", return_value=50),
            ):
                await git_module.cleanup_branch_worktree("agent/issue-42")

        asyncio.run(run())

        marker = json.loads((worktree / ".cleanup-after").read_text())
        assert marker == {"expires_at": 170, "branch": "agent/issue-42"}

    def test_create_branch_worktree_writes_marker_on_creation(self):
        async def mock_run(cmd, cwd=None):
            return ""

        async def run():
            with (
                patch.object(git_module, "_run", side_effect=mock_run),
                patch.object(git_module, "_mark_worktree_for_cleanup") as mock_mark,
                patch.object(git_module, "BARE_CLONE_PATH", Path("/tmp/test-repo.git")),
                patch.object(git_module, "REVIEWS_PATH", Path("/tmp/test-reviews")),
                patch("pathlib.Path.exists", return_value=False),
                patch("pathlib.Path.mkdir"),
            ):
                path = await git_module.create_branch_worktree(
                    "agent/issue-42", "https://github.com/user/repo.git"
                )

            assert path == Path("/tmp/test-reviews/agent-issue-42")
            mock_mark.assert_called_once_with(
                Path("/tmp/test-reviews/agent-issue-42"), "agent/issue-42"
            )

        asyncio.run(run())


class TestReapOldWorktrees:
    def test_reaps_only_expired_marked_worktrees(self, tmp_path: Path):
        expired = tmp_path / "pr-1"
        expired.mkdir()
        (expired / ".cleanup-after").write_text(
            json.dumps({"expires_at": 10, "branch": "pr-1"}) + "\n"
        )

        fresh = tmp_path / "pr-2"
        fresh.mkdir()
        (fresh / ".cleanup-after").write_text(
            json.dumps({"expires_at": 30, "branch": "pr-2"}) + "\n"
        )

        unmarked = tmp_path / "pr-3"
        unmarked.mkdir()

        async def run():
            with (
                patch.object(git_module, "REVIEWS_PATH", tmp_path),
                patch("services.git.time.time", return_value=20),
                patch.object(
                    git_module, "_remove_named_worktree", new_callable=AsyncMock
                ) as mock_remove,
            ):
                await git_module.reap_old_worktrees()

            mock_remove.assert_awaited_once_with(expired, "pr-1")

        asyncio.run(run())

    def test_remove_named_worktree_falls_back_without_bare_clone(self, tmp_path: Path):
        worktree = tmp_path / "pr-1"
        worktree.mkdir()
        (worktree / "file.txt").write_text("debug state")

        async def run():
            with patch.object(git_module, "BARE_CLONE_PATH", tmp_path / "missing-repo.git"):
                await git_module._remove_named_worktree(worktree, "pr-1")

        asyncio.run(run())

        assert not worktree.exists()
