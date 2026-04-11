"""Tests for git operations."""

import asyncio
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
        # 3: head_file.exists() in init_bare_clone → False (fresh clone)
        exists_calls = iter([True, True, False])

        async def run():
            with (
                patch.object(git_module, "_run", side_effect=mock_run),
                patch.object(git_module, "BARE_CLONE_PATH", Path("/tmp/test-repo.git")),
                patch.object(git_module, "REVIEWS_PATH", Path("/tmp/test-reviews")),
                patch("pathlib.Path.exists", side_effect=lambda *a: next(exists_calls)),
                patch("pathlib.Path.mkdir"),
            ):
                path = await git_module.create_worktree(42, "https://github.com/user/repo.git")

            assert path == Path("/tmp/test-reviews/pr-42")
            assert any("worktree" in cmd and "remove" in cmd for cmd, _ in calls)

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
                if attempt < 3:
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
            assert len(fetch_calls) == 3
            assert mock_sleep.call_count == 2
            mock_sleep.assert_any_call(2)
            mock_sleep.assert_any_call(4)

        asyncio.run(run())

    def test_fetch_raises_after_all_retries_exhausted(self):
        """Fetch raises after exhausting all retry attempts."""
        import pytest

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


class TestCommitAndPush:
    def test_unstages_cli_artifacts(self):
        """commit_and_push unstages .copilot-session.md and .copilot/ before committing."""
        calls = []

        async def mock_run(cmd, cwd=None):
            calls.append(cmd)
            return "abc123"

        async def run():
            with (
                patch.object(git_module, "_run", side_effect=mock_run),
                patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec,
                patch("pathlib.Path.write_text"),
                patch("pathlib.Path.chmod"),
                patch("pathlib.Path.unlink"),
            ):
                # git diff --cached --quiet → returncode 1 (has changes)
                diff_proc = AsyncMock()
                diff_proc.communicate.return_value = (b"", b"")
                diff_proc.returncode = 1
                # git push → success
                push_proc = AsyncMock()
                push_proc.communicate.return_value = (b"", b"")
                push_proc.returncode = 0
                mock_exec.side_effect = [diff_proc, push_proc]

                await git_module.commit_and_push(
                    Path("/tmp/wt"),
                    message="test",
                    token="tok",
                    repo="user/repo",
                    branch="main",
                )

            # Check git rm --cached was called for artifacts
            rm_calls = [c for c in calls if "rm" in c and "--cached" in c]
            assert len(rm_calls) == 2
            assert any(".copilot-session.md" in c for c in rm_calls)
            assert any(".copilot" in c for c in rm_calls)

        asyncio.run(run())
