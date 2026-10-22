"""Stand-in for the bundled ``dsh`` runtime: speaks the SDK's newline-delimited JSON-RPC over stdio.

Prompt text selects the scenario:
  * ``fail-auth``  -> the turn ends with ``kind == "error"`` (like an invalid API key)
  * ``crash``      -> the process exits right after accepting the prompt
  * ``hang``       -> the prompt is accepted but the session never becomes idle
  * ``history``    -> the assistant answers ``history: <earlier prompts joined by |>``
  * ``tool``       -> the assistant calls a ``bash`` tool, gets a result, then answers
  * ``spawn``      -> like a normal answer, plus one persisted subagent session
  * anything else  -> the assistant answers ``echo: <prompt>``

Sessions use the real on-disk layout of ``@deepseek-ai/dsh-session-persistence-jsonl``:
``$DSH_HOME/sessions/<project-key>/<id>/session.v4.jsonl.zstd`` (one zstd frame per
append) and a ``session.lock`` that is flock-held while the session is open here.
Like the real runtime, a persisted session that is not open in this process must be
restored with ``session/resume`` before it accepts prompts.
Launches and session calls are appended to ``$FAKE_DSH_LOG`` (JSON lines).
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

import zstandard

_cwd = os.getcwd()
_open: dict[str, int] = {}  # session id -> fd holding the flock on session.lock
_workdir: dict[str, str] = {}  # session id -> effective working directory


def log(entry: dict) -> None:
    log_path = os.environ.get("FAKE_DSH_LOG")
    if log_path:
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")


# ==================== persistence ====================


def sessions_root() -> Path:
    return Path(os.environ["DSH_HOME"]) / "sessions"


def project_key(cwd: str) -> str:
    readable = re.sub(r"[/\\:]+", "-", cwd)
    readable = re.sub(r"[^A-Za-z0-9._-]", lambda m: f"~{ord(m.group()):04X}", readable)
    return f"--{readable.lstrip('-') or 'root'}--"


def find_dir(session_id: str) -> Path | None:
    matches = [p for p in sessions_root().glob(f"*/{session_id}") if (p / "session.v4.jsonl.zstd").is_file()]
    return matches[0] if matches else None


def now_ms() -> int:
    return int(time.time() * 1000)


def append(directory: Path, records: list[dict]) -> None:
    payload = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records).encode()
    with open(directory / "session.v4.jsonl.zstd", "ab") as fh:
        fh.write(zstandard.ZstdCompressor().compress(payload))


def read_records(directory: Path) -> list[dict]:
    import io

    raw = (directory / "session.v4.jsonl.zstd").read_bytes()
    reader = zstandard.ZstdDecompressor().stream_reader(io.BytesIO(raw), read_across_frames=True)
    return [json.loads(line) for line in reader.read().decode().splitlines() if line.strip()]


def take_lock(session_id: str, directory: Path) -> None:
    fd = os.open(directory / "session.lock", os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    _open[session_id] = fd


def create_session(session_id: str, parent: str | None = None) -> Path:
    directory = sessions_root() / project_key(_cwd) / session_id
    directory.mkdir(parents=True)
    header = {"type": "session", "version": 4, "id": session_id, "createdAt": now_ms(), "cwd": _cwd, "isSeeded": False}
    header["delegationDepth"] = 1 if parent else 0
    if parent:
        header["parentSession"] = parent
    append(directory, [header, {"type": "permission/preset", "seq": 0, "time": now_ms(), "data": {"preset": "workspace-write"}}])
    return directory


# ==================== protocol ====================


def send(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def send_error(request_id: object, message: str) -> None:
    send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603, "message": message}})


def notify(method: str, params: dict) -> None:
    send({"jsonrpc": "2.0", "method": method, "params": params})


class Turn:
    """Collects the events of one prompt, persists them and streams them as notifications."""

    def __init__(self, session_id: str, directory: Path) -> None:
        self.session_id = session_id
        self.directory = directory
        self.next_seq = len(read_records(directory)) - 1

    def event(self, event_type: str, data: dict) -> None:
        event = {"type": event_type, "seq": self.next_seq, "time": now_ms(), "data": data}
        self.next_seq += 1
        append(self.directory, [event])
        notify("session.event", {"sessionId": self.session_id, "event": event})


def user_prompts(directory: Path) -> list[str]:
    prompts = []
    for record in read_records(directory)[1:]:
        data = record.get("data") or {}
        if record.get("type") == "user/message" and (data.get("source") or {}).get("kind") == "user":
            prompts.append("".join(b.get("text", "") for b in data.get("content", [])))
    return prompts


def assistant(turn: Turn, blocks: list[dict]) -> None:
    usage = {"inputTokens": 10, "outputTokens": 5, "cacheReadTokens": 100, "cacheWriteTokens": 0, "totalTokens": 115}
    turn.event("assistant/message", {"turn": 1, "step": 1, "message": {"role": "assistant", "content": blocks}, "usage": usage})


def handle_resume(request_id: object, params: dict) -> None:
    session_id = params["sessionId"]
    log({"call": "resume", "session": session_id})
    directory = find_dir(session_id)
    if session_id in _open:
        send_error(request_id, f"SDK session is already open or opening: {session_id}")
    elif directory is None:
        send_error(request_id, f'session "{session_id}" not found')
    else:
        take_lock(session_id, directory)
        records = read_records(directory)
        moves = [r["data"]["path"] for r in records[1:] if r.get("type") == "working-directory/set"]
        _workdir[session_id] = moves[-1] if moves else records[0].get("cwd") or _cwd
        send({"jsonrpc": "2.0", "id": request_id, "result": {}})


def missing_dir_error(path: str) -> str:
    return f'working-directory: directory does not exist: "{path}"'


def handle_working_directory(request_id: object, method: str, params: dict) -> None:
    session_id = params["sessionId"]
    if session_id not in _open:
        send_error(request_id, f"fake runtime: session {session_id} is not open")
        return
    if method.endswith("/set"):
        path = os.path.normpath(os.path.join(_workdir[session_id], params["path"]))
        if not os.path.isdir(path):
            send_error(request_id, missing_dir_error(path))
            return
        _workdir[session_id] = path
        append(find_dir(session_id), [{"type": "working-directory/set", "seq": -1, "time": now_ms(), "data": {"path": path}}])
        log({"call": "set-cwd", "session": session_id})
    elif not os.path.isdir(_workdir[session_id]):
        send_error(request_id, missing_dir_error(_workdir[session_id]))
        return
    send({"jsonrpc": "2.0", "id": request_id, "result": {"cwd": _workdir[session_id]}})


def handle_prompt(request_id: object, params: dict) -> None:
    session_id = params["sessionId"]
    text = "".join(block.get("text", "") for block in params.get("contentBlocks", []))
    log({"call": "prompt", "session": session_id})
    if session_id in _open:
        directory = find_dir(session_id)
    elif find_dir(session_id) is not None:
        send_error(request_id, f'session "{session_id}" already exists')
        return
    else:
        directory = create_session(session_id)
        take_lock(session_id, directory)
        _workdir[session_id] = _cwd
    history = user_prompts(directory)

    message_id = str(uuid.uuid4())
    send({"jsonrpc": "2.0", "id": request_id, "result": {"messageId": message_id}})
    if text == "crash":
        sys.exit(3)
    turn = Turn(session_id, directory)
    turn.event("agent/inbox/spliced", {"inserted": [{"id": message_id, "role": "user"}]})
    notify("session.status", {"sessionId": session_id, "status": "running"})
    turn.event("turn/start", {"turn": 1})
    turn.event("system/message", {"message": {"role": "system", "content": [{"type": "text", "text": "x" * 64}]}})
    turn.event("user/message", {"role": "user", "id": message_id, "source": {"kind": "user"}, "content": [{"type": "text", "text": text}]})
    turn.event("user/message", {"role": "user", "source": {"kind": "runtime-context"}, "content": [{"type": "text", "text": "Current runtime context."}]})
    turn.event("request/context", {"provider": "deepseek-official", "model": "fake-model", "contextWindow": 1000})
    if not history:
        turn.event("session/title", {"title": text[:20], "source": {"kind": "fallback"}})
    if text == "hang":
        return
    if not os.path.isdir(_workdir[session_id]):
        reason = {"kind": "error", "error": {"message": missing_dir_error(_workdir[session_id])}}
    elif text == "fail-auth":
        reason = {"kind": "error", "error": {"message": "Authentication Fails", "code": "AUTH", "status": 401}}
    else:
        if text == "tool":
            call_id = "call-1"
            assistant(turn, [
                {"type": "reasoning", "text": "need to run ls"},
                {"type": "tool-call", "id": call_id, "name": "bash", "arguments": '{"command": "ls"}'},
            ])
            turn.event("tool/call", {"turn": 1, "step": 1, "callId": call_id, "name": "bash", "arguments": '{"command": "ls"}'})
            turn.event("tool/result", {"turn": 1, "step": 1, "message": {"role": "tool", "toolCallId": call_id, "isError": False, "content": [{"type": "text", "text": "hello.py"}]}})
        if text == "spawn":
            child = create_session(str(uuid.uuid4()), parent=session_id)
            append(child, [{"type": "turn/start", "seq": 1, "time": now_ms(), "data": {"turn": 1}}])
        if text == "history":
            answer = f"history: {'|'.join(history)}"
        elif text == "pwd":
            answer = f"pwd: {_workdir[session_id]}"
        else:
            answer = f"echo: {text}"
        assistant(turn, [{"type": "text", "text": answer}])
        reason = {"kind": "completed"}
    turn.event("turn/end", {"turn": 1, "reason": reason})
    notify("session.status", {"sessionId": session_id, "status": "idle"})


def main() -> None:
    global _cwd
    env = {k: os.environ.get(k) for k in ("DSH_HOME", "DSH_PERMISSION_MODE", "DEEPSEEK_API_KEY")}
    empty_env = sorted(k for k, v in os.environ.items() if k.startswith(("DSH_", "DEEPSEEK_")) and not v.strip())
    log({"argv": sys.argv[1:], "env": env, "empty_env": empty_env, "cwd": os.getcwd()})

    for line in sys.stdin:
        if not line.strip():
            continue
        message = json.loads(line)
        method, request_id = message.get("method"), message.get("id")
        params = message.get("params") or {}
        if method == "initialize":
            _cwd = params.get("cwd") or _cwd
            send({"jsonrpc": "2.0", "id": request_id, "result": {"serverInfo": {"name": "fake-dsh", "version": "0"}}})
        elif method == "session/prompt":
            handle_prompt(request_id, params)
        elif method == "session/resume":
            handle_resume(request_id, params)
        elif method in ("session/working-directory/get", "session/working-directory/set"):
            handle_working_directory(request_id, method, params)
        elif method == "shutdown":
            send({"jsonrpc": "2.0", "id": request_id, "result": {}})
            return
        elif request_id is not None:
            send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"unknown method {method}"}})


if __name__ == "__main__":
    main()
