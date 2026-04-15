"""Tests for GitHub API — token management and REST helpers."""

import asyncio
from unittest.mock import AsyncMock, patch

import httpx

import services.github as github
from models import GitHubIssue, GitHubPullRequest


class TestTokenManagement:
    def test_returns_provided_token(self):
        github.set_token("ghs_test_token_123")

        async def run():
            return await github.get_token()

        token = asyncio.run(run())
        assert token == "ghs_test_token_123"
        github.reset_token_cache()

    def test_raises_when_no_token_set(self):
        github.reset_token_cache()

        async def run():
            return await github.get_token()

        import pytest

        with pytest.raises(RuntimeError, match="No GitHub token available"):
            asyncio.run(run())

    def test_reset_clears_token(self):
        github.set_token("token")
        github.reset_token_cache()

        async def run():
            return await github.get_token()

        import pytest

        with pytest.raises(RuntimeError):
            asyncio.run(run())


class TestBotLogin:
    def test_login(self):
        assert github.bot_login() == "colins-homelab-bot[bot]"


class TestBotEmail:
    def test_email(self):
        assert github.bot_email() == "274352150+colins-homelab-bot[bot]@users.noreply.github.com"


class TestFindPrByBranch:
    def test_returns_pr_when_found(self):
        pr_resp = httpx.Response(
            200,
            json=[{"number": 42, "html_url": "https://github.com/user/repo/pull/42"}],
            request=httpx.Request("GET", "https://api.github.com/pulls"),
        )

        async def run():
            with (
                patch("services.github.get_token", new_callable=AsyncMock, return_value="token"),
                patch(
                    "httpx.AsyncClient.get",
                    new_callable=AsyncMock,
                    return_value=pr_resp,
                ),
            ):
                return await github.find_pr_by_branch("user/repo", "agent/issue-1")

        result = asyncio.run(run())
        assert result is not None
        assert result.number == 42

    def test_returns_none_when_no_pr(self):
        pr_resp = httpx.Response(
            200,
            json=[],
            request=httpx.Request("GET", "https://api.github.com/pulls"),
        )

        async def run():
            with (
                patch("services.github.get_token", new_callable=AsyncMock, return_value="token"),
                patch(
                    "httpx.AsyncClient.get",
                    new_callable=AsyncMock,
                    return_value=pr_resp,
                ),
            ):
                return await github.find_pr_by_branch("user/repo", "agent/issue-1")

        result = asyncio.run(run())
        assert result is None


class TestIssueAndPrParsing:
    def test_get_issue_returns_typed_model(self):
        issue_resp = httpx.Response(
            200,
            json={"title": "Bug", "body": "Fix it", "user": {"login": "ColinCee"}},
            request=httpx.Request("GET", "https://api.github.com/issues/1"),
        )

        async def run():
            with (
                patch("services.github.get_token", new_callable=AsyncMock, return_value="token"),
                patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=issue_resp),
            ):
                return await github.get_issue("user/repo", 1)

        result = asyncio.run(run())
        assert isinstance(result, GitHubIssue)
        assert result.title == "Bug"
        assert result.user is not None
        assert result.user.login == "ColinCee"

    def test_get_pr_returns_typed_model(self):
        pr_resp = httpx.Response(
            200,
            json={
                "number": 42,
                "title": "Fix bug",
                "body": "Fixes #1",
                "base": {"ref": "main"},
                "head": {"ref": "agent/issue-1", "repo": {"full_name": "user/repo"}},
            },
            request=httpx.Request("GET", "https://api.github.com/pulls/42"),
        )

        async def run():
            with (
                patch("services.github.get_token", new_callable=AsyncMock, return_value="token"),
                patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=pr_resp),
            ):
                return await github.get_pr("user/repo", 42)

        result = asyncio.run(run())
        assert isinstance(result, GitHubPullRequest)
        assert result.number == 42
        assert result.base is not None
        assert result.base.ref == "main"


class TestIssueComments:
    def test_comment_on_issue_returns_comment_id(self):
        comment_resp = httpx.Response(
            201,
            json={"id": 12345},
            request=httpx.Request("POST", "https://api.github.com/comment"),
        )

        async def run():
            with (
                patch("services.github.get_token", new_callable=AsyncMock, return_value="token"),
                patch(
                    "httpx.AsyncClient.post",
                    new_callable=AsyncMock,
                    return_value=comment_resp,
                ) as mock_post,
            ):
                result = await github.comment_on_issue("user/repo", 7, "hello")
                return result, mock_post

        result, mock_post = asyncio.run(run())
        assert result == 12345
        assert mock_post.await_args.kwargs["json"] == {"body": "hello"}

    def test_update_comment_edits_existing_comment(self):
        update_resp = httpx.Response(
            200,
            json={"id": 12345},
            request=httpx.Request("PATCH", "https://api.github.com/comment"),
        )

        async def run():
            with (
                patch("services.github.get_token", new_callable=AsyncMock, return_value="token"),
                patch(
                    "httpx.AsyncClient.patch",
                    new_callable=AsyncMock,
                    return_value=update_resp,
                ) as mock_patch,
            ):
                await github.update_comment("user/repo", 12345, "updated")
                return mock_patch

        mock_patch = asyncio.run(run())
        assert (
            mock_patch.await_args.args[0]
            == "https://api.github.com/repos/user/repo/issues/comments/12345"
        )
        assert mock_patch.await_args.kwargs["json"] == {"body": "updated"}

    def test_find_issue_comment_by_body_prefix_returns_latest_bot_comment(self):
        comments_resp = httpx.Response(
            200,
            json=[
                {
                    "id": 1,
                    "body": "🔄 Review in progress for PR #7...",
                    "user": {"login": "someone-else"},
                },
                {
                    "id": 2,
                    "body": "✅ Review posted — see review above",
                    "user": {"login": github.bot_login()},
                },
                {
                    "id": 3,
                    "body": "🔄 Review in progress for PR #7...",
                    "user": {"login": github.bot_login()},
                },
            ],
            request=httpx.Request("GET", "https://api.github.com/comments"),
        )

        async def run():
            with (
                patch("services.github.get_token", new_callable=AsyncMock, return_value="token"),
                patch(
                    "httpx.AsyncClient.get",
                    new_callable=AsyncMock,
                    return_value=comments_resp,
                ),
            ):
                return await github.find_issue_comment_by_body_prefix(
                    "user/repo", 7, "🔄 Review in progress for PR #"
                )

        result = asyncio.run(run())
        assert result == 3
