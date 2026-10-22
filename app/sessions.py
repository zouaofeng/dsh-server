"""Session management: list, inspect, read history and delete persisted sessions.

Every operation is confined to the caller's scope (one per auth token), so a client
only ever sees and touches the sessions it could also continue through ``/v1/chat``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional

from .harness_service import HarnessService, SessionNotFoundError
from .scopes import public_session_id, scoped_session_id
from .session_store import SessionStore, StoredSession, extract_messages


def _iso(ms: Optional[int]) -> Optional[str]:
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class SessionManager:
    def __init__(self, store: SessionStore, service: HarnessService) -> None:
        self.store = store
        self.service = service

    async def list_sessions(self, scope: Optional[str], *, limit: int, offset: int) -> dict:
        records = await asyncio.to_thread(self.store.list_all)
        visible = []
        for record in records:
            public_id = public_session_id(scope, record.id) if record.parent_id is None else None
            if public_id is not None:
                visible.append((public_id, record))
        visible.sort(key=lambda item: item[1].updated_at, reverse=True)
        page = visible[offset: offset + limit]
        return {
            "total": len(visible),
            "sessions": [self._summary(public_id, record) for public_id, record in page],
        }

    async def get_session(self, scope: Optional[str], session_id: str) -> dict:
        record = await self._require(scope, session_id)
        descendants = await asyncio.to_thread(self.store.with_descendants, record)
        return {
            **self._summary(session_id, record),
            "model": record.model,
            "cwd": record.cwd,
            "event_count": record.event_count,
            "last_finish_reason": record.last_finish_reason,
            "last_error": record.last_error,
            "usage": record.usage,
            "subagent_sessions": len(descendants) - 1,
        }

    async def get_messages(
        self, scope: Optional[str], session_id: str, *, include_tools: bool, include_reasoning: bool
    ) -> dict:
        record = await self._require(scope, session_id)
        events = await asyncio.to_thread(self.store.read_events, record)
        messages = extract_messages(events, include_tools=include_tools, include_reasoning=include_reasoning)
        for message in messages:
            message["time"] = _iso(message.get("time"))
        return {"session_id": session_id, "messages": messages}

    async def delete_session(self, scope: Optional[str], session_id: str) -> int:
        return await self.service.delete_session(self.store, scoped_session_id(scope, session_id), session_id)

    # ---------- helpers ----------

    async def _require(self, scope: Optional[str], session_id: str) -> StoredSession:
        record = await asyncio.to_thread(self.store.get, scoped_session_id(scope, session_id))
        if record is None or record.parent_id is not None:
            raise SessionNotFoundError(f"session {session_id!r} not found")
        return record

    def _summary(self, public_id: str, record: StoredSession) -> dict:
        return {
            "session_id": public_id,
            "title": record.title,
            "created_at": _iso(record.created_at),
            "updated_at": _iso(record.updated_at),
            "turns": record.turns,
            "status": self.service.session_status(record.id),
        }
