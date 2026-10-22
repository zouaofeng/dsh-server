"""HTTP (sync + SSE) and WebSocket endpoints."""

from __future__ import annotations

import asyncio
import json
import logging
from importlib import metadata
from typing import Any, AsyncIterator, Optional, Union

from fastapi import APIRouter, Depends, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from . import __version__
from .auth import WS_UNAUTHORIZED_CODE, require_api_key, verify_ws_token
from .harness_service import HarnessService, RunCancelledError, RunStream, ServiceError, build_event, error_event
from .schemas import ChatRequest, ChatResponse, HarnessStatus, HealthResponse

logger = logging.getLogger(__name__)

try:
    SDK_VERSION = metadata.version("deepseek-harness-sdk")
except metadata.PackageNotFoundError:  # pragma: no cover
    SDK_VERSION = "unknown"

SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

router = APIRouter()


def _service(conn: Union[Request, WebSocket]) -> HarnessService:
    return conn.app.state.harness_service


@router.get("/healthz", response_model=HealthResponse, tags=["health"])
async def healthz(request: Request) -> HealthResponse:
    settings = request.app.state.settings
    return HealthResponse(
        status="ok",
        version=__version__,
        sdk_version=SDK_VERSION,
        profile=settings.dsh_profile,
        model=settings.dsh_model,
        harness=HarnessStatus(**_service(request).status()),
    )


@router.post(
    "/v1/chat",
    response_model=ChatResponse,
    tags=["chat"],
    summary="Run a prompt and wait for the final answer",
)
async def chat(
    body: ChatRequest, request: Request, scope: Optional[str] = Depends(require_api_key)
) -> ChatResponse:
    stream = _service(request).open_stream(body.prompt, body.session_id, scope=scope, include_notifications=False)
    result = await result_unless_disconnected(stream, request)
    return ChatResponse(**result)


async def result_unless_disconnected(stream: RunStream, request: Any) -> dict:
    """Wait for the run's result, dropping the run if the client disconnects while it is queued.

    The server does not cancel a request handler when its client goes away, so the
    disconnect has to be observed explicitly. A run that already started keeps going.
    """
    result = asyncio.ensure_future(stream.result())
    disconnect = asyncio.ensure_future(_wait_for_disconnect(request))
    try:
        done, _ = await asyncio.wait({result, disconnect}, return_when=asyncio.FIRST_COMPLETED)
        if result in done:
            return result.result()
        stream.cancel()
        logger.info("client disconnected; dropped queued run session=%s", stream.session_id)
        raise RunCancelledError("client disconnected")
    finally:
        for task in (result, disconnect):
            if not task.done():
                task.cancel()


async def _wait_for_disconnect(request: Any) -> None:
    while (await request.receive())["type"] != "http.disconnect":
        pass


@router.post(
    "/v1/chat/stream",
    tags=["chat"],
    summary="Run a prompt and stream events as Server-Sent Events",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def chat_stream(
    body: ChatRequest, request: Request, scope: Optional[str] = Depends(require_api_key)
) -> StreamingResponse:
    # Submitting before the response starts lets a full queue surface as a plain HTTP 429.
    stream = _service(request).open_stream(body.prompt, body.session_id, scope=scope)
    return StreamingResponse(
        _sse_lines(stream),
        media_type="text/event-stream",
        headers={**SSE_HEADERS, "X-Session-Id": stream.session_id},
    )


async def _sse_lines(stream: RunStream) -> AsyncIterator[str]:
    async for event in stream.events():
        if event["type"] == "ping":
            yield ": ping\n\n"  # SSE comment keeps proxies from timing out idle streams
            continue
        yield f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"


@router.websocket("/v1/ws")
async def chat_ws(websocket: WebSocket) -> None:
    await websocket.accept()
    authorized, scope = verify_ws_token(websocket)
    if not authorized:
        await websocket.close(code=WS_UNAUTHORIZED_CODE, reason="unauthorized")
        return
    service = _service(websocket)
    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            raw = message.get("text")
            if raw is None:
                raw = (message.get("bytes") or b"").decode("utf-8", errors="replace")
            try:
                body = ChatRequest.model_validate_json(raw)
            except ValidationError as exc:
                await websocket.send_json(
                    build_event("error", None, {"code": "invalid_request", "message": _validation_message(exc)})
                )
                continue
            try:
                stream = service.open_stream(body.prompt, body.session_id, scope=scope)
            except ServiceError as exc:
                await websocket.send_json(error_event(body.session_id, exc))
                continue
            events = stream.events()
            try:
                async for event in events:
                    await websocket.send_json(event)
            finally:
                await events.aclose()
    except WebSocketDisconnect:
        logger.info("websocket client disconnected")


def _validation_message(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors(include_url=False):
        loc = ".".join(str(item) for item in error.get("loc", ()))
        parts.append(f"{loc}: {error.get('msg')}" if loc else str(error.get("msg")))
    return "; ".join(parts) or "invalid request"
