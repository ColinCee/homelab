"""Tests for git operations."""

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

import pytest

import git as git_module


class TestRunCommand:
    def test_raises_on_failure(self):
        async def run():
            with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_proc:
                proc = AsyncMock()
                proc.communicate.return_value = (b"", b"error message")
                proc.returncode = 1
                mock_proc.return_value = proc

                with pytest.raises(RuntimeError, match="Command failed"):
                    await git_module._run(["git", "status"])

        asyncio.run(run())


class TestCreateWorktree:
    def test_creates_worktree_for_pr(self):
        calls = []

        async def mock_run(cmd, cwd=None):
            calls.append((cmd, cwd))
            return ""

        async def run():
            with TemporaryDirectory() as tmp_dir:
                bare_clone = Path(tmp_dir) / "repo.git"
                reviews_path = Path(tmp_dir) / "reviews"
                with (
                    patch.object(git_module, "_run", side_effect=mock_run),
                    patch.object(git_module, "BARE_CLONE_PATH", bare_clone),
                    patch.object(git_module, "REVIEWS_PATH", reviews_path),
                ):
                    path = await git_module.create_worktree(42, "https://github.com/user/repo.git")

            assert path == Path(tmp_dir) / "reviews" / "pr-42"
            assert len(calls) == 4
            assert "clone" in calls[0][0][1]
            assert "branch" in calls[1][0][1]
            assert "pull/42/head:pr-42" in calls[2][0][3]
            assert "worktree" in calls[3][0][1]

        asyncio.run(run())

    def test_create_worktree_reaps_expired_worktrees_before_creating(self):
        async def run():
            with TemporaryDirectory() as tmp_dir:
                bare_clone = Path(tmp_dir) / "repo.git"
                reviews_path = Path(tmp_dir) / "reviews"
                with (
                    patch.object(git_module, "_run", new=AsyncMock(return_value="")),
                    patch.object(
                        git_module,
                        "_reap_old_worktrees_locked",
                        new=AsyncMock(),
                    ) as mock_reap,
                    patch.object(git_module, "BARE_CLONE_PATH", bare_clone),
                    patch.object(git_module, "REVIEWS_PATH", reviews_path),
                ):
                    await git_module.create_worktree(42, "https://github.com/user/repo.git")

                mock_reap.assert_awaited_once()

        asyncio.run(run())

    def test_cleans_stale_worktree_without_deadlock(self):
        """Recreating a worktree for the same PR must not deadlock."""
        calls = []

        async def mock_run(cmd, cwd=None):
            calls.append((cmd, cwd))
            return ""

        async def run():
            with TemporaryDirectory() as tmp_dir:
                bare_clone = Path(tmp_dir) / "repo.git"
                reviews_path = Path(tmp_dir) / "reviews"
                stale_worktree = reviews_path / "pr-42"
                stale_worktree.mkdir(parents=True)

                with (
                    patch.object(git_module, "_run", side_effect=mock_run),
                    patch.object(git_module, "BARE_CLONE_PATH", bare_clone),
                    patch.object(git_module, "REVIEWS_PATH", reviews_path),
                ):
                    path = await git_module.create_worktree(42, "https://github.com/user/repo.git")

            assert path == Path(tmp_dir) / "reviews" / "pr-42"
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
                if attempt < 4:
                    raise RuntimeError("couldn't find remote ref")
            return ""

        async def run():
            with TemporaryDirectory() as tmp_dir:
                bare_clone = Path(tmp_dir) / "repo.git"
                reviews_path = Path(tmp_dir) / "reviews"
                with (
                    patch.object(git_module, "_run", side_effect=mock_run),
                    patch.object(git_module, "BARE_CLONE_PATH", bare_clone),
                    patch.object(git_module, "REVIEWS_PATH", reviews_path),
                    patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
                ):
                    path = await git_module.create_worktree(42, "https://github.com/user/repo.git")

                assert path == reviews_path / "pr-42"
                fetch_calls = [
                    call for call in calls if "fetch" in call and "pull/" in " ".join(call)
                ]
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
            with TemporaryDirectory() as tmp_dir:
                bare_clone = Path(tmp_dir) / "repo.git"
                reviews_path = Path(tmp_dir) / "reviews"
                with (
                    patch.object(git_module, "_run", side_effect=mock_run),
                    patch.object(git_module, "BARE_CLONE_PATH", bare_clone),
                    patch.object(git_module, "REVIEWS_PATH", reviews_path),
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
            with TemporaryDirectory() as tmp_dir:
                bare_clone = Path(tmp_dir) / "repo.git"
                reviews_path = Path(tmp_dir) / "reviews"
                with (
                    patch.object(git_module, "_run", side_effect=mock_run),
                    patch.object(git_module, "BARE_CLONE_PATH", bare_clone),
                    patch.object(git_module, "REVIEWS_PATH", reviews_path),
                ):
                    await git_module.create_worktree(
                        42, "https://github.com/user/repo.git", head_ref="agent/issue-42"
                    )

            fetch_calls = [call for call in calls if "fetch" in call]
            assert len(fetch_calls) == 1
            assert "agent/issue-42:pr-42" in fetch_calls[0][3]
            assert "pull/" not in fetch_calls[0][3]

        asyncio.run(run())

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
                diff_proc = AsyncMock()
                diff_proc.communicate.return_value = (b"", b"")
                diff_proc.returncode = 1
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

            rm_calls = [call for call in calls if "rm" in call and "--cached" in call]
            assert len(rm_calls) == 2
            assert any(".copilot-session.md" in call for call in rm_calls)
            assert any(".copilot" in call for call in rm_calls)

        asyncio.run(run())


class TestDeferredCleanup:
    def test_cleanup_worktree_writes_marker(self):
        async def run():
            with TemporaryDirectory() as tmp_dir:
                reviews_path = Path(tmp_dir) / "reviews"
                worktree_path = reviews_path / "pr-42"
                worktree_path.mkdir(parents=True)

                with (
                    patch.object(git_module, "REVIEWS_PATH", reviews_path),
                    patch.object(git_module, "WORKTREE_RETENTION_SECONDS", 60),
                    patch.object(
                        git_module,
                        "_reap_old_worktrees_locked",
                        new=AsyncMock(),
                    ) as mock_reap,
                    patch("time.time", return_value=1_000),
                ):
                    await git_module.cleanup_worktree(42)

                mock_reap.assert_awaited_once()
                marker = json.loads((worktree_path / ".cleanup-after").read_text())
                assert marker == {"expires_at": 1_060, "branch_name": "pr-42"}

        asyncio.run(run())

    def test_cleanup_branch_worktree_writes_marker(self):
        async def run():
            with TemporaryDirectory() as tmp_dir:
                reviews_path = Path(tmp_dir) / "reviews"
                worktree_path = reviews_path / "agent-issue-59"
                worktree_path.mkdir(parents=True)

                with (
                    patch.object(git_module, "REVIEWS_PATH", reviews_path),
                    patch.object(git_module, "WORKTREE_RETENTION_SECONDS", 120),
                    patch.object(
                        git_module,
                        "_reap_old_worktrees_locked",
                        new=AsyncMock(),
                    ) as mock_reap,
                    patch("time.time", return_value=2_000),
                ):
                    await git_module.cleanup_branch_worktree("agent/issue-59")

                mock_reap.assert_awaited_once()
                marker = json.loads((worktree_path / ".cleanup-after").read_text())
                assert marker == {"expires_at": 2_120, "branch_name": "agent/issue-59"}

        asyncio.run(run())

    def test_reaps_only_expired_worktrees(self):
        async def run():
            with TemporaryDirectory() as tmp_dir:
                bare_clone = Path(tmp_dir) / "repo.git"
                reviews_path = Path(tmp_dir) / "reviews"

                expired_path = reviews_path / "pr-42"
                expired_path.mkdir(parents=True)
                (expired_path / ".cleanup-after").write_text(
                    json.dumps({"expires_at": 100, "branch_name": "pr-42"})
                )

                retained_path = reviews_path / "pr-43"
                retained_path.mkdir(parents=True)
                (retained_path / ".cleanup-after").write_text(
                    json.dumps({"expires_at": 1_000, "branch_name": "pr-43"})
                )

                with (
                    patch.object(git_module, "BARE_CLONE_PATH", bare_clone),
                    patch.object(git_module, "REVIEWS_PATH", reviews_path),
                    patch.object(
                        git_module,
                        "_run",
                        side_effect=RuntimeError("git worktree metadata unavailable"),
                    ),
                    patch("time.time", return_value=500),
                ):
                    await git_module.reap_old_worktrees()

                assert not expired_path.exists()
                assert retained_path.exists()

        asyncio.run(run())
