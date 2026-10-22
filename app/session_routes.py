"""Session management endpoints, scoped to the caller's auth token."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Path, Query, Request, Response, status

from .auth import require_api_key
from .scopes import SESSION_ID_PATTERN
from .schemas import SessionDetail, SessionList, SessionMessages
from .sessions import SessionManager

router = APIRouter(prefix="/v1/sessions", tags=["sessions"])

SessionIdPath = Path(..., pattern=SESSION_ID_PATTERN, description="Client-facing session id")


def _manager(request: Request) -> SessionManager:
    return request.app.state.session_manager


@router.get("", response_model=SessionList, summary="List sessions, most recently updated first")
async def list_sessions(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    scope: Optional[str] = Depends(require_api_key),
) -> dict:
    return await _manager(request).list_sessions(scope, limit=limit, offset=offset)


@router.get("/{session_id}", response_model=SessionDetail, summary="Session metadata and token usage")
async def get_session(
    request: Request,
    session_id: str = SessionIdPath,
    scope: Optional[str] = Depends(require_api_key),
) -> dict:
    return await _manager(request).get_session(scope, session_id)


@router.get(
    "/{session_id}/messages",
    response_model=SessionMessages,
    response_model_exclude_none=True,
    summary="Conversation history of a session",
)
async def get_session_messages(
    request: Request,
    session_id: str = SessionIdPath,
    include_tools: bool = Query(False, description="Include assistant tool calls and tool results"),
    include_reasoning: bool = Query(False, description="Include the model's reasoning text"),
    scope: Optional[str] = Depends(require_api_key),
) -> dict:
    return await _manager(request).get_messages(
        scope, session_id, include_tools=include_tools, include_reasoning=include_reasoning
    )


@router.delete(
    "/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Delete a session, its subagent sessions and cached data",
)
async def delete_session(
    request: Request,
    session_id: str = SessionIdPath,
    scope: Optional[str] = Depends(require_api_key),
) -> Response:
    await _manager(request).delete_session(scope, session_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
