"""Request / response models shared by the HTTP, SSE and WebSocket endpoints."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

from .scopes import SESSION_ID_PATTERN


class ChatRequest(BaseModel):
    prompt: str = Field(..., min_length=1, description="User task / message sent to the agent")
    session_id: Optional[str] = Field(
        default=None,
        pattern=SESSION_ID_PATTERN,
        description="Reuse an id to continue a conversation; omitted = new session",
    )

    @field_validator("prompt")
    @classmethod
    def _prompt_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("prompt must not be blank")
        return value


class ChatResponse(BaseModel):
    session_id: str
    final_response: str
    finish_reason: Optional[str] = None
    # Model-level failure reported by the last `turn/end` (e.g. invalid API key).
    error: Optional[str] = None


class HarnessStatus(BaseModel):
    started: bool
    busy: bool
    pending: int


class HealthResponse(BaseModel):
    status: str
    version: str
    sdk_version: str
    profile: str
    model: str
    harness: HarnessStatus


# ==================== session management ====================

SessionStatus = Literal["idle", "queued", "running"]


class SessionSummary(BaseModel):
    session_id: str
    title: Optional[str] = Field(None, description="Generated from the first prompt")
    created_at: Optional[str] = Field(None, description="ISO 8601, UTC")
    updated_at: Optional[str] = Field(None, description="Time of the last event, ISO 8601, UTC")
    turns: int = Field(..., description="Number of user prompts")
    status: SessionStatus = Field(..., description="Whether this service has queued/running tasks for it")


class SessionList(BaseModel):
    total: int
    sessions: list[SessionSummary]


class TokenUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    total_tokens: int = 0


class SessionDetail(SessionSummary):
    model: Optional[str] = None
    cwd: Optional[str] = Field(None, description="Working directory the session was created in")
    event_count: int
    last_finish_reason: Optional[str] = None
    last_error: Optional[str] = None
    usage: TokenUsage = Field(..., description="Summed over all model calls of the session")
    subagent_sessions: int = Field(..., description="Subagent sessions spawned from this session")


class ToolCall(BaseModel):
    id: Optional[str] = None
    name: str
    arguments: Optional[str] = Field(None, description="Raw JSON arguments as produced by the model")


class SessionMessage(BaseModel):
    seq: Optional[int] = None
    time: Optional[str] = None
    role: Literal["user", "assistant", "tool"]
    text: str = ""
    reasoning: Optional[str] = None
    tool_calls: Optional[list[ToolCall]] = None
    tool_call_id: Optional[str] = None
    is_error: Optional[bool] = None


class SessionMessages(BaseModel):
    session_id: str
    messages: list[SessionMessage]
