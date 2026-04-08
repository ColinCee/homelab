"""Tests for GitHub App authentication."""

import asyncio
import time
from unittest.mock import AsyncMock, mock_open, patch

import httpx

import github_app


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

        token = github_app._generate_jwt("12345", pem)

        public_key = private_key.public_key()
        decoded = jwt.decode(token, public_key, algorithms=["RS256"])
        assert decoded["iss"] == "12345"
        assert decoded["exp"] - decoded["iat"] == 660  # 60s past + 10min


class TestGetInstallationToken:
    def test_returns_token_from_api(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()

        github_app.reset_token_cache()

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
                return await github_app.get_installation_token()

        token = asyncio.run(run())
        assert token == "ghs_test_token_123"

    def test_returns_cached_token(self):
        github_app._cached_token = "cached_token"
        github_app._token_expires_at = time.time() + 3600

        async def run():
            return await github_app.get_installation_token()

        token = asyncio.run(run())
        assert token == "cached_token"

        github_app.reset_token_cache()
