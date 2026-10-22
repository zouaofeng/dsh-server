"""Optional Bearer-token authentication for HTTP and WebSocket endpoints.

When tokens are configured, each token also gets its own session scope, so callers
using different tokens cannot continue each other's sessions.
"""

from __future__ import annotations

import hmac
from typing import Optional

from fastapi import HTTPException, Request, WebSocket, status

from .scopes import token_scope

WS_UNAUTHORIZED_CODE = 4401


def _match_key(token: str, keys: list[str]) -> Optional[str]:
    candidate = token.encode()
    matched: Optional[str] = None
    # Compare against every key so the timing does not reveal which one matched.
    for key in keys:
        if hmac.compare_digest(candidate, key.encode()):
            matched = key
    return matched


def _bearer_token(authorization: Optional[str]) -> str:
    scheme, _, token = (authorization or "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


async def require_api_key(request: Request) -> Optional[str]:
    """FastAPI dependency returning the caller's session scope.

    Returns ``None`` (no scope) when SERVICE_API_KEY is not configured.
    """
    keys = request.app.state.settings.service_api_keys
    if not keys:
        return None
    token = _bearer_token(request.headers.get("authorization"))
    matched = _match_key(token, keys) if token else None
    if matched is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token_scope(matched)


def verify_ws_token(websocket: WebSocket) -> tuple[bool, Optional[str]]:
    """Return ``(authorized, scope)``.

    Accepts ``Authorization: Bearer <token>`` or the ``?token=<token>`` query parameter.
    """
    keys = websocket.app.state.settings.service_api_keys
    if not keys:
        return True, None
    token = _bearer_token(websocket.headers.get("authorization")) or websocket.query_params.get("token", "")
    matched = _match_key(token, keys) if token else None
    if matched is None:
        return False, None
    return True, token_scope(matched)
