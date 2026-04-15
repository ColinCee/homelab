"""Tests for git operations."""

import asyncio
import json
import threading
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

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

    def test_raises_clear_error_on_timeout(self):
        async def run():
            first_call = True

            async def communicate() -> tuple[bytes, bytes]:
                nonlocal first_call
                if first_call:
                    first_call = False
                    await asyncio.sleep(3600)
                return (b"", b"")

            with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_proc:
                proc = AsyncMock()
                proc.communicate.side_effect = communicate
                proc.kill = Mock()
                mock_proc.return_value = proc

                with (
                    patch.object(git_module, "GIT_COMMAND_TIMEOUT_SECONDS", 0.01),
                    pytest.raises(
                        RuntimeError,
                        match=r"Git command timed out after 0\.01s: git status",
                    ),
                ):
                    await git_module._run(["git", "status"])

            proc.kill.assert_called_once_with()

        asyncio.run(run())


class TestRepoLock:
    def test_acquire_repo_file_lock_creates_lock_file(self, tmp_path: Path):
        with patch.object(git_module, "REVIEWS_PATH", tmp_path):
            fd = git_module._acquire_repo_file_lock(1)

        try:
            assert (tmp_path / git_module.REPO_LOCK_FILE).exists()
        finally:
            git_module._release_repo_file_lock(fd)

    def test_hold_repo_lock_acquires_and_releases_shared_lock(self, tmp_path: Path):
        async def run():
            with (
                patch.object(git_module, "REVIEWS_PATH", tmp_path),
                patch.object(
                    git_module, "_acquire_repo_file_lock", return_value=123
                ) as mock_acquire,
                patch.object(git_module, "_release_repo_file_lock") as mock_release,
            ):
                async with git_module._hold_repo_lock():
                    pass

            mock_acquire.assert_called_once_with(git_module.REPO_LOCK_TIMEOUT_SECONDS)
            mock_release.assert_called_once_with(123)

        asyncio.run(run())

    def test_hold_repo_lock_raises_clear_error_when_async_lock_times_out(self, tmp_path: Path):
        async def run():
            lock = asyncio.Lock()
            await lock.acquire()

            with (
                patch.object(git_module, "_repo_lock", lock),
                patch.object(git_module, "REVIEWS_PATH", tmp_path),
                patch.object(git_module, "REPO_LOCK_TIMEOUT_SECONDS", 0.01),
                pytest.raises(
                    RuntimeError,
                    match=r"Git repo lock timed out after 0\.01s",
                ),
            ):
                async with git_module._hold_repo_lock():
                    pytest.fail("timed out acquisition should not enter the critical section")

            lock.release()

        asyncio.run(run())

    def test_hold_repo_lock_releases_async_lock_when_timeout_fires_after_acquire(self):
        async def timeout_after_acquire(awaitable, timeout):
            await awaitable
            raise TimeoutError

        async def run():
            lock = asyncio.Lock()

            with (
                patch.object(git_module, "_repo_lock", lock),
                patch("services.git.asyncio.wait_for", side_effect=timeout_after_acquire),
                pytest.raises(RuntimeError, match="Git repo lock timed out"),
            ):
                async with git_module._hold_repo_lock():
                    pytest.fail("timed out acquisition should not enter the critical section")

            assert not lock.locked()

        asyncio.run(run())

    def test_hold_repo_lock_raises_clear_error_when_file_lock_times_out(self, tmp_path: Path):
        async def run():
            lock = asyncio.Lock()

            with (
                patch.object(git_module, "_repo_lock", lock),
                patch.object(git_module, "REVIEWS_PATH", tmp_path),
                patch.object(git_module, "_acquire_repo_file_lock", side_effect=TimeoutError),
                pytest.raises(RuntimeError, match="Git repo lock timed out"),
            ):
                async with git_module._hold_repo_lock():
                    pytest.fail("timed out acquisition should not enter the critical section")

            assert not lock.locked()

        asyncio.run(run())

    def test_hold_repo_lock_releases_shared_lock_when_cancelled(self, tmp_path: Path):
        acquire_started = threading.Event()
        allow_acquire_to_finish = threading.Event()

        def block_then_acquire(timeout_seconds: int) -> int:
            acquire_started.set()
            allow_acquire_to_finish.wait()
            return 123

        async def wait_for_lock() -> None:
            async with git_module._hold_repo_lock():
                pytest.fail("cancelled acquisition should not enter the critical section")

        async def run():
            with (
                patch.object(git_module, "REVIEWS_PATH", tmp_path),
                patch.object(git_module, "_acquire_repo_file_lock", side_effect=block_then_acquire),
                patch.object(git_module, "_release_repo_file_lock") as mock_release,
            ):
                task = asyncio.create_task(wait_for_lock())
                await asyncio.to_thread(acquire_started.wait)
                task.cancel()
                allow_acquire_to_finish.set()

                with pytest.raises(asyncio.CancelledError):
                    await task

            mock_release.assert_called_once_with(123)

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


class TestInitBareClone:
    def test_removes_main_worktrees_before_fetching(self, tmp_path: Path):
        bare_clone = tmp_path / "repo.git"
        bare_clone.mkdir()
        (bare_clone / "HEAD").write_text("ref: refs/heads/main\n")
        calls = []

        async def mock_run(cmd, cwd=None):
            calls.append((cmd, cwd))
            if cmd == ["git", "worktree", "list", "--porcelain"]:
                return (
                    "worktree /reviews/agent-issue-96\n"
                    "HEAD abcdef1234567890\n"
                    "branch refs/heads/main\n\n"
                    "worktree /reviews/pr-42\n"
                    "HEAD fedcba0987654321\n"
                    "branch refs/heads/pr-42\n"
                )
            return ""

        async def run():
            with (
                patch.object(git_module, "_run", side_effect=mock_run),
                patch.object(git_module, "BARE_CLONE_PATH", bare_clone),
            ):
                path = await git_module.init_bare_clone("https://github.com/user/repo.git")

            assert path == bare_clone

        asyncio.run(run())

        assert calls == [
            (["git", "worktree", "prune"], bare_clone),
            (["git", "worktree", "list", "--porcelain"], bare_clone),
            (["git", "worktree", "remove", "--force", "/reviews/agent-issue-96"], bare_clone),
            (["git", "fetch", "origin", "+refs/heads/main:refs/heads/main"], bare_clone),
        ]


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

    def test_reap_old_worktrees_uses_shared_repo_lock(self, tmp_path: Path):
        async def run():
            with (
                patch.object(git_module, "REVIEWS_PATH", tmp_path),
                patch.object(
                    git_module, "_reap_old_worktrees_locked", new_callable=AsyncMock
                ) as mock_reap,
                patch.object(
                    git_module, "_acquire_repo_file_lock", return_value=123
                ) as mock_acquire,
                patch.object(git_module, "_release_repo_file_lock") as mock_release,
            ):
                await git_module.reap_old_worktrees()

            mock_reap.assert_awaited_once()
            mock_acquire.assert_called_once_with(git_module.REPO_LOCK_TIMEOUT_SECONDS)
            mock_release.assert_called_once_with(123)

        asyncio.run(run())
