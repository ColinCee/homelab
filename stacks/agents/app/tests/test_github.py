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


class TestParseDiffRightLines:
    def test_parses_added_lines(self):
        patch = "@@ -0,0 +1,3 @@\n+line1\n+line2\n+line3"
        assert github._parse_diff_right_lines(patch) == [1, 2, 3]

    def test_parses_context_lines(self):
        patch = "@@ -1,3 +1,4 @@\n existing\n+added\n existing2\n existing3"
        result = github._parse_diff_right_lines(patch)
        assert 1 in result  # context "existing"
        assert 2 in result  # added
        assert 3 in result  # context "existing2"
        assert 4 in result  # context "existing3"

    def test_skips_deleted_lines(self):
        patch = "@@ -1,3 +1,2 @@\n existing\n-removed\n existing2"
        result = github._parse_diff_right_lines(patch)
        # Only right-side lines: 1 (existing), 2 (existing2)
        assert result == [1, 2]

    def test_multiple_hunks(self):
        patch = "@@ -1,2 +1,2 @@\n context\n+added1\n@@ -10,2 +10,2 @@\n context2\n+added2"
        result = github._parse_diff_right_lines(patch)
        assert 1 in result  # first hunk context
        assert 2 in result  # first hunk added
        assert 10 in result  # second hunk context
        assert 11 in result  # second hunk added

    def test_empty_patch(self):
        assert github._parse_diff_right_lines("") == []

    def test_real_world_patch(self):
        patch = (
            "@@ -29,6 +29,7 @@ class ReviewComment(BaseModel):\n"
            "     path: str\n"
            "     line: int\n"
            "+    start_line: int | None = None\n"
            "     body: str\n"
        )
        result = github._parse_diff_right_lines(patch)
        assert 29 in result  # context
        assert 30 in result  # context
        assert 31 in result  # added (start_line)
        assert 32 in result  # context


class TestGetCommitCIStatus:
    def test_reports_success_when_checks_pass(self):
        status_resp = httpx.Response(
            200,
            json={"state": "pending", "statuses": [], "total_count": 0},
            request=httpx.Request("GET", "https://api.github.com/status"),
        )
        checks_resp = httpx.Response(
            200,
            json={
                "total_count": 1,
                "check_runs": [{"name": "check", "status": "completed", "conclusion": "success"}],
            },
            request=httpx.Request("GET", "https://api.github.com/check-runs"),
        )

        async def run():
            with (
                patch("github.get_token", new_callable=AsyncMock, return_value="token"),
                patch(
                    "httpx.AsyncClient.get",
                    new_callable=AsyncMock,
                    side_effect=[status_resp, checks_resp],
                ),
            ):
                return await github.get_commit_ci_status("user/repo", "abc123")

        result = asyncio.run(run())
        assert result["state"] == "success"
        assert result["description"] == "All required CI checks passed"

    def test_reports_pending_when_checks_are_running(self):
        status_resp = httpx.Response(
            200,
            json={"state": "pending", "statuses": [], "total_count": 0},
            request=httpx.Request("GET", "https://api.github.com/status"),
        )
        checks_resp = httpx.Response(
            200,
            json={
                "total_count": 1,
                "check_runs": [{"name": "check", "status": "queued", "conclusion": None}],
            },
            request=httpx.Request("GET", "https://api.github.com/check-runs"),
        )

        async def run():
            with (
                patch("github.get_token", new_callable=AsyncMock, return_value="token"),
                patch(
                    "httpx.AsyncClient.get",
                    new_callable=AsyncMock,
                    side_effect=[status_resp, checks_resp],
                ),
            ):
                return await github.get_commit_ci_status("user/repo", "abc123")

        result = asyncio.run(run())
        assert result["state"] == "pending"
        assert "still running" in result["description"]

    def test_reports_failure_when_a_check_fails(self):
        status_resp = httpx.Response(
            200,
            json={"state": "pending", "statuses": [], "total_count": 0},
            request=httpx.Request("GET", "https://api.github.com/status"),
        )
        checks_resp = httpx.Response(
            200,
            json={
                "total_count": 1,
                "check_runs": [{"name": "check", "status": "completed", "conclusion": "failure"}],
            },
            request=httpx.Request("GET", "https://api.github.com/check-runs"),
        )

        async def run():
            with (
                patch("github.get_token", new_callable=AsyncMock, return_value="token"),
                patch(
                    "httpx.AsyncClient.get",
                    new_callable=AsyncMock,
                    side_effect=[status_resp, checks_resp],
                ),
            ):
                return await github.get_commit_ci_status("user/repo", "abc123")

        result = asyncio.run(run())
        assert result["state"] == "failure"
        assert result["failing_checks"] == ["check"]

    def test_reports_none_when_no_ci_signals_exist(self):
        status_resp = httpx.Response(
            200,
            json={"state": "pending", "statuses": [], "total_count": 0},
            request=httpx.Request("GET", "https://api.github.com/status"),
        )
        checks_resp = httpx.Response(
            200,
            json={"total_count": 0, "check_runs": []},
            request=httpx.Request("GET", "https://api.github.com/check-runs"),
        )

        async def run():
            with (
                patch("github.get_token", new_callable=AsyncMock, return_value="token"),
                patch(
                    "httpx.AsyncClient.get",
                    new_callable=AsyncMock,
                    side_effect=[status_resp, checks_resp],
                ),
            ):
                return await github.get_commit_ci_status("user/repo", "abc123")

        result = asyncio.run(run())
        assert result["state"] == "none"

    def test_paginates_check_runs_before_reporting_success(self):
        status_resp = httpx.Response(
            200,
            json={"state": "pending", "statuses": [], "total_count": 0},
            request=httpx.Request("GET", "https://api.github.com/status"),
        )
        checks_page_one = httpx.Response(
            200,
            json={
                "total_count": 101,
                "check_runs": [
                    {"name": f"check-{i}", "status": "completed", "conclusion": "success"}
                    for i in range(100)
                ],
            },
            request=httpx.Request("GET", "https://api.github.com/check-runs?page=1"),
        )
        checks_page_two = httpx.Response(
            200,
            json={
                "total_count": 101,
                "check_runs": [{"name": "late-check", "status": "queued", "conclusion": None}],
            },
            request=httpx.Request("GET", "https://api.github.com/check-runs?page=2"),
        )

        async def run():
            with (
                patch("github.get_token", new_callable=AsyncMock, return_value="token"),
                patch(
                    "httpx.AsyncClient.get",
                    new_callable=AsyncMock,
                    side_effect=[status_resp, checks_page_one, checks_page_two],
                ),
            ):
                return await github.get_commit_ci_status("user/repo", "abc123")

        result = asyncio.run(run())
        assert result["state"] == "pending"
        assert result["pending_checks"] == ["late-check"]


class TestMergePullRequest:
    def test_uses_squash_merge_with_head_sha(self):
        merge_resp = httpx.Response(
            200,
            json={"merged": True, "sha": "merge123"},
            request=httpx.Request("PUT", "https://api.github.com/merge"),
        )

        async def run():
            with (
                patch("github.get_token", new_callable=AsyncMock, return_value="token"),
                patch(
                    "httpx.AsyncClient.put",
                    new_callable=AsyncMock,
                    return_value=merge_resp,
                ) as mock_put,
            ):
                result = await github.merge_pull_request("user/repo", 7, sha="abc123")
                return result, mock_put

        result, mock_put = asyncio.run(run())
        assert result["merged"] is True
        assert mock_put.await_args.kwargs["json"] == {
            "merge_method": "squash",
            "sha": "abc123",
        }

    def test_returns_manual_attention_payload_for_merge_rejection(self):
        merge_resp = httpx.Response(
            405,
            json={"message": "Pull Request is not mergeable"},
            request=httpx.Request("PUT", "https://api.github.com/merge"),
        )

        async def run():
            with (
                patch("github.get_token", new_callable=AsyncMock, return_value="token"),
                patch("httpx.AsyncClient.put", new_callable=AsyncMock, return_value=merge_resp),
            ):
                return await github.merge_pull_request("user/repo", 7, sha="abc123")

        result = asyncio.run(run())
        assert result["merged"] is False
        assert result["status_code"] == 405
        assert result["message"] == "Pull Request is not mergeable"

    def test_returns_manual_attention_payload_for_forbidden_merge(self):
        merge_resp = httpx.Response(
            403,
            json={"message": "Resource not accessible by integration"},
            request=httpx.Request("PUT", "https://api.github.com/merge"),
        )

        async def run():
            with (
                patch("github.get_token", new_callable=AsyncMock, return_value="token"),
                patch("httpx.AsyncClient.put", new_callable=AsyncMock, return_value=merge_resp),
            ):
                return await github.merge_pull_request("user/repo", 7, sha="abc123")

        result = asyncio.run(run())
        assert result["merged"] is False
        assert result["status_code"] == 403
        assert result["message"] == "Resource not accessible by integration"


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
