"""Bearer token authentication for agent dispatch endpoints.

Defense-in-depth on top of the Tailscale boundary and `ALLOWED_ACTORS`
trust check. Only the dispatch endpoints (`/review`, `/implement`) require
the bearer token; `/health` and status endpoints stay open for monitoring.
"""

import os
import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

_security = HTTPBearer(auto_error=False)
_BEARER_DEP = Depends(_security)


def _expected_token() -> str:
    token = os.environ.get("AGENT_API_KEY", "")
    if not token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AGENT_API_KEY is not configured on the server",
        )
    return token


def require_bearer(
    creds: HTTPAuthorizationCredentials | None = _BEARER_DEP,
) -> None:
    """FastAPI dependency: 401 unless a valid bearer token is presented."""
    expected = _expected_token()
    if creds is None or not secrets.compare_digest(creds.credentials, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
