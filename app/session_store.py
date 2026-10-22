"""Read (and delete) sessions persisted by the dsh runtime under ``$DSH_HOME``.

On-disk layout written by ``@deepseek-ai/dsh-session-persistence-jsonl``::

    sessions/<project-key>/<session-id>/session.v<N>.jsonl.zstd   header line + one JSON event per line
    sessions/<project-key>/<session-id>/session.lock              flock(2)-held while a runtime has it open
    storages/session_projcache/sessions/<session-id>.json         derived projection cache (disposable)

``<project-key>`` is derived from the session's working directory, and the runtime
resolves a session id across all project directories (ids are unique per home).
The log is appended as independent zstd frames, so a reader may observe a frame that
is still being written: the trailing partial line is ignored.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

import zstandard

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

LOCK_FILENAME = "session.lock"
_LOG_RE = re.compile(r"^session\.v(\d+)\.jsonl(\.zstd)?$")
_SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_USAGE_KEYS = {
    "inputTokens": "input_tokens",
    "outputTokens": "output_tokens",
    "cacheReadTokens": "cache_read_tokens",
    "cacheWriteTokens": "cache_write_tokens",
    "totalTokens": "total_tokens",
}


class CorruptSessionError(Exception):
    pass


@dataclass(frozen=True)
class StoredSession:
    """Summary of one persisted session log."""

    id: str
    directory: Path
    log_path: Path
    parent_id: Optional[str]
    cwd: Optional[str]
    created_at: int  # epoch milliseconds
    updated_at: int
    title: Optional[str]
    turns: int
    event_count: int
    model: Optional[str]
    last_finish_reason: Optional[str]
    last_error: Optional[str]
    usage: dict[str, int] = field(default_factory=dict)


# ==================== parsing ====================


def read_log(path: Path) -> tuple[dict, list[dict]]:
    """Return ``(header, events)`` of a session log."""
    raw = path.read_bytes()
    data = _decompress(raw) if path.name.endswith(".zstd") else raw
    header: Optional[dict] = None
    events: list[dict] = []
    # The last element is either b"" or a line that is still being written.
    for line in data.split(b"\n")[:-1]:
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            logger.warning("skipping unparsable line in %s", path)
            continue
        if header is None:
            if not isinstance(obj, dict) or obj.get("type") != "session":
                raise CorruptSessionError(f"{path}: first line is not a session header")
            header = obj
        elif isinstance(obj, dict):
            events.append(obj)
    if header is None:
        raise CorruptSessionError(f"{path}: empty session log")
    return header, events


def _decompress(raw: bytes) -> bytes:
    out = io.BytesIO()
    reader = zstandard.ZstdDecompressor().stream_reader(io.BytesIO(raw), read_across_frames=True)
    try:
        while chunk := reader.read(1 << 16):
            out.write(chunk)
    except zstandard.ZstdError:
        logger.warning("zstd stream ended unexpectedly; using the decoded prefix")
    return out.getvalue()


def _content_text(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    return "".join(str(b.get("text") or "") for b in content if isinstance(b, dict) and b.get("type") == "text")


def _assistant_content(data: dict) -> list:
    message = data.get("message")
    owner = message if isinstance(message, dict) else data
    content = owner.get("content")
    return content if isinstance(content, list) else []


def _is_user_prompt(data: dict) -> bool:
    source = data.get("source")
    return isinstance(source, dict) and source.get("kind") == "user"


def summarize(header: dict, events: list[dict], directory: Path, log_path: Path) -> StoredSession:
    title = model = finish = error = None
    turns = 0
    usage: dict[str, int] = {}
    for event in events:
        etype = event.get("type")
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        if etype == "user/message" and _is_user_prompt(data):
            turns += 1
        elif etype == "session/title" and data.get("title"):
            title = str(data["title"])
        elif etype == "request/context" and data.get("model"):
            model = str(data["model"])
        elif etype == "assistant/message" and isinstance(data.get("usage"), dict):
            for key, name in _USAGE_KEYS.items():
                value = data["usage"].get(key)
                if isinstance(value, (int, float)):
                    usage[name] = usage.get(name, 0) + int(value)
        elif etype == "turn/end":
            reason = data.get("reason") if isinstance(data.get("reason"), dict) else {}
            finish = reason.get("kind")
            err = reason.get("error")
            error = str(err.get("message")) if finish == "error" and isinstance(err, dict) else None
    created = int(header.get("createdAt") or 0)
    last_time = next((e["time"] for e in reversed(events) if isinstance(e.get("time"), (int, float))), created)
    parent = header.get("parentSession")
    return StoredSession(
        id=str(header.get("id")),
        directory=directory,
        log_path=log_path,
        parent_id=str(parent) if parent else None,
        cwd=header.get("cwd"),
        created_at=created,
        updated_at=int(last_time),
        title=title,
        turns=turns,
        event_count=len(events),
        model=model,
        last_finish_reason=finish,
        last_error=error,
        usage=usage,
    )


def extract_messages(events: list[dict], *, include_tools: bool = False, include_reasoning: bool = False) -> list[dict]:
    """Conversation view of a session: user prompts, assistant replies and (optionally) tool traffic."""
    messages: list[dict] = []
    for event in events:
        etype = event.get("type")
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        base = {"seq": event.get("seq"), "time": event.get("time")}
        if etype == "user/message" and _is_user_prompt(data):
            messages.append({**base, "role": "user", "text": _content_text(data.get("content"))})
        elif etype == "assistant/message":
            content = _assistant_content(data)
            message = {**base, "role": "assistant", "text": _content_text(content)}
            if include_reasoning:
                reasoning = "".join(
                    str(b.get("text") or "") for b in content if isinstance(b, dict) and b.get("type") == "reasoning"
                )
                if reasoning:
                    message["reasoning"] = reasoning
            calls = [
                {"id": b.get("id"), "name": str(b.get("name") or ""), "arguments": b.get("arguments")}
                for b in content
                if isinstance(b, dict) and b.get("type") == "tool-call"
            ]
            if include_tools and calls:
                message["tool_calls"] = calls
            if message["text"] or "tool_calls" in message or "reasoning" in message:
                messages.append(message)
        elif etype == "tool/result" and include_tools:
            result = data.get("message") if isinstance(data.get("message"), dict) else {}
            messages.append(
                {
                    **base,
                    "role": "tool",
                    "text": _content_text(result.get("content")),
                    "tool_call_id": result.get("toolCallId"),
                    "is_error": bool(result.get("isError")),
                }
            )
    return messages


# ==================== store ====================


class SessionStore:
    def __init__(self, home: Path) -> None:
        self.home = home
        self.sessions_root = home / "sessions"
        self.projcache_dir = home / "storages" / "session_projcache" / "sessions"
        self._cache: dict[Path, tuple[int, int, StoredSession]] = {}
        self._lock = threading.Lock()

    # ---------- lookup ----------

    def list_all(self) -> list[StoredSession]:
        """Every readable session (root and subagent sessions)."""
        found: list[StoredSession] = []
        seen: set[Path] = set()
        for directory in self._session_dirs():
            record = self._load(directory)
            if record is not None:
                found.append(record)
                seen.add(record.log_path)
        with self._lock:
            for stale in [p for p in self._cache if p not in seen]:
                self._cache.pop(stale, None)
        return found

    def get(self, session_id: str) -> Optional[StoredSession]:
        if not _SAFE_KEY_RE.match(session_id) or session_id in (".", ".."):
            return None
        for project in self._project_dirs():
            record = self._load(project / session_id)
            if record is not None and record.id == session_id:
                return record
        return None

    def read_events(self, session: StoredSession) -> list[dict]:
        return read_log(session.log_path)[1]

    def with_descendants(self, root: StoredSession) -> list[StoredSession]:
        """``root`` followed by every subagent session spawned (transitively) from it."""
        children: dict[str, list[StoredSession]] = {}
        for record in self.list_all():
            if record.parent_id:
                children.setdefault(record.parent_id, []).append(record)
        result, stack, visited = [root], [root.id], {root.id}
        while stack:
            for child in children.get(stack.pop(), []):
                if child.id not in visited:
                    visited.add(child.id)
                    result.append(child)
                    stack.append(child.id)
        return result

    def is_open(self, session: StoredSession) -> bool:
        """Whether some process (normally the runtime) holds the session's write lock."""
        lock_path = session.directory / LOCK_FILENAME
        if fcntl is None:
            return True  # cannot tell: assume open
        try:
            fd = os.open(lock_path, os.O_RDONLY)
        except FileNotFoundError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        finally:
            os.close(fd)

    # ---------- mutation ----------

    def delete(self, sessions: Iterable[StoredSession]) -> None:
        for session in sessions:
            shutil.rmtree(session.directory, ignore_errors=True)
            if _SAFE_KEY_RE.match(session.id):
                # <id>.json plus its <id>.json.bak.<stamp> backups / temp files
                for cache_file in self.projcache_dir.glob(f"{glob_escape(session.id)}.json*"):
                    cache_file.unlink(missing_ok=True)
            with self._lock:
                self._cache.pop(session.log_path, None)
            logger.info("deleted session=%s dir=%s", session.id, session.directory)

    # ---------- helpers ----------

    def _project_dirs(self) -> list[Path]:
        try:
            return [p for p in self.sessions_root.iterdir() if p.is_dir()]
        except FileNotFoundError:
            return []

    def _session_dirs(self) -> list[Path]:
        dirs: list[Path] = []
        for project in self._project_dirs():
            try:
                dirs.extend(p for p in project.iterdir() if p.is_dir())
            except FileNotFoundError:
                continue
        return dirs

    @staticmethod
    def _current_log(directory: Path) -> Optional[Path]:
        best: Optional[tuple[int, Path]] = None
        try:
            entries = list(directory.iterdir())
        except (FileNotFoundError, NotADirectoryError):
            return None
        for entry in entries:
            match = _LOG_RE.match(entry.name)
            if match and entry.is_file():
                version = int(match.group(1))
                if best is None or version > best[0]:
                    best = (version, entry)
        return best[1] if best else None

    def _load(self, directory: Path) -> Optional[StoredSession]:
        log_path = self._current_log(directory)
        if log_path is None:
            return None
        try:
            stat = log_path.stat()
            with self._lock:
                cached = self._cache.get(log_path)
            if cached and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
                return cached[2]
            header, events = read_log(log_path)
            record = summarize(header, events, directory, log_path)
        except FileNotFoundError:
            return None
        except (OSError, CorruptSessionError) as exc:
            logger.warning("skipping unreadable session %s: %s", directory, exc)
            return None
        with self._lock:
            self._cache[log_path] = (stat.st_mtime_ns, stat.st_size, record)
        return record


def glob_escape(text: str) -> str:
    return re.sub(r"([*?\[])", r"[\1]", text)
