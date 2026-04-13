"""Tests for GitHub API — auth, REST, and GraphQL helpers."""

import asyncio
import time
from unittest.mock import AsyncMock, mock_open, patch

import httpx

import github


class TestGenerateJWT:
    def test_generates_valid_jwt(self):
        import jwt
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()

        token = github._generate_jwt("12345", pem)

        public_key = private_key.public_key()
        decoded = jwt.decode(token, public_key, algorithms=["RS256"])
        assert decoded["iss"] == "12345"
        assert decoded["exp"] - decoded["iat"] == 660  # 60s past + 10min


class TestGetToken:
    def test_returns_token_from_api(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()

        github.reset_token_cache()

        env = {
            "GITHUB_APP_ID": "12345",
            "GITHUB_APP_INSTALLATION_ID": "67890",
            "GITHUB_APP_PRIVATE_KEY_PATH": "/fake/key.pem",
        }

        mock_resp = httpx.Response(
            200,
            json={"token": "ghs_test_token_123"},
            request=httpx.Request("POST", "https://api.github.com/test"),
        )

        async def run():
            with (
                patch.dict("os.environ", env),
                patch("builtins.open", mock_open(read_data=pem)),
                patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp),
            ):
                return await github.get_token()

        token = asyncio.run(run())
        assert token == "ghs_test_token_123"

    def test_returns_cached_token(self):
        github._cached_token = "cached_token"
        github._token_expires_at = time.time() + 3600

        async def run():
            return await github.get_token()

        token = asyncio.run(run())
        assert token == "cached_token"

        github.reset_token_cache()


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
                patch("github.get_token", new_callable=AsyncMock, return_value="token"),
                patch(
                    "httpx.AsyncClient.get",
                    new_callable=AsyncMock,
                    return_value=pr_resp,
                ),
            ):
                return await github.find_pr_by_branch("user/repo", "user:agent/issue-1")

        result = asyncio.run(run())
        assert result is not None
        assert result["number"] == 42

    def test_returns_none_when_no_pr(self):
        pr_resp = httpx.Response(
            200,
            json=[],
            request=httpx.Request("GET", "https://api.github.com/pulls"),
        )

        async def run():
            with (
                patch("github.get_token", new_callable=AsyncMock, return_value="token"),
                patch(
                    "httpx.AsyncClient.get",
                    new_callable=AsyncMock,
                    return_value=pr_resp,
                ),
            ):
                return await github.find_pr_by_branch("user/repo", "user:agent/issue-1")

        result = asyncio.run(run())
        assert result is None


class TestIssueComments:
    def test_comment_on_issue_returns_comment_id(self):
        comment_resp = httpx.Response(
            201,
            json={"id": 12345},
            request=httpx.Request("POST", "https://api.github.com/comment"),
        )

        async def run():
            with (
                patch("github.get_token", new_callable=AsyncMock, return_value="token"),
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
                patch("github.get_token", new_callable=AsyncMock, return_value="token"),
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
                patch("github.get_token", new_callable=AsyncMock, return_value="token"),
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
