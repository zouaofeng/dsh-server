from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import zstandard
from fastapi.testclient import TestClient

from app.harness_service import HarnessService
from app.main import create_app
from app.session_store import read_log
from tests.conftest import BlockingHarness, read_launch_log

ISO_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")


def chat(client, prompt: str, session_id: str, token: str | None = None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.post("/v1/chat", json={"prompt": prompt, "session_id": session_id}, headers=headers)


def session_dirs(home: Path) -> list[str]:
    return sorted(p.name for p in home.glob("sessions/*/*") if p.is_dir())


# ==================== list ====================


def test_list_is_empty_without_sessions(make_settings, make_client):
    client = make_client(make_settings())

    assert client.get("/v1/sessions").json() == {"total": 0, "sessions": []}


def test_list_returns_root_sessions_newest_first(make_settings, make_client):
    client = make_client(make_settings())
    chat(client, "first prompt", "s-old")
    chat(client, "spawn", "s-new")

    body = client.get("/v1/sessions").json()

    assert body["total"] == 2  # the subagent session spawned by s-new is not listed
    assert [s["session_id"] for s in body["sessions"]] == ["s-new", "s-old"]
    old = body["sessions"][1]
    assert old["title"] == "first prompt"
    assert old["turns"] == 1
    assert old["status"] == "idle"
    assert ISO_RE.match(old["created_at"]) and ISO_RE.match(old["updated_at"])


def test_list_supports_pagination(make_settings, make_client):
    client = make_client(make_settings())
    for i in range(3):
        chat(client, "hi", f"s-{i}")

    page = client.get("/v1/sessions", params={"limit": 2, "offset": 1}).json()

    assert page["total"] == 3
    assert [s["session_id"] for s in page["sessions"]] == ["s-1", "s-0"]
    assert client.get("/v1/sessions", params={"limit": 0}).status_code == 422


# ==================== detail ====================


def test_detail_reports_usage_turns_and_subagents(make_settings, make_client):
    client = make_client(make_settings())
    chat(client, "tool", "s-d")
    chat(client, "spawn", "s-d")

    body = client.get("/v1/sessions/s-d").json()

    assert body["session_id"] == "s-d"
    assert body["turns"] == 2
    assert body["model"] == "fake-model"
    assert body["last_finish_reason"] == "completed"
    assert body["last_error"] is None
    # three assistant messages (tool call, tool answer, spawn answer) of 115 tokens each
    assert body["usage"] == {"input_tokens": 30, "output_tokens": 15, "cache_read_tokens": 300, "cache_write_tokens": 0, "total_tokens": 345}
    assert body["subagent_sessions"] == 1
    assert body["event_count"] > 0


def test_detail_reports_last_error(make_settings, make_client):
    client = make_client(make_settings())
    chat(client, "fail-auth", "s-err")

    body = client.get("/v1/sessions/s-err").json()

    assert body["last_finish_reason"] == "error"
    assert body["last_error"] == "Authentication Fails"


def test_unknown_or_invalid_session_ids(make_settings, make_client):
    client = make_client(make_settings())

    missing = client.get("/v1/sessions/nope")
    assert missing.status_code == 404
    assert missing.json()["code"] == "session_not_found"
    assert client.get("/v1/sessions/nope/messages").status_code == 404
    assert client.delete("/v1/sessions/nope").status_code == 404
    assert client.get("/v1/sessions/bad%20id").status_code == 422
    assert client.get("/v1/sessions/.hidden").status_code == 422


# ==================== messages ====================


def test_messages_default_view_is_user_and_assistant_text(make_settings, make_client):
    client = make_client(make_settings())
    chat(client, "tool", "s-m")
    chat(client, "again", "s-m")

    body = client.get("/v1/sessions/s-m/messages").json()

    assert body["session_id"] == "s-m"
    assert [(m["role"], m["text"]) for m in body["messages"]] == [
        ("user", "tool"),
        ("assistant", "echo: tool"),
        ("user", "again"),
        ("assistant", "echo: again"),
    ]
    assert all(ISO_RE.match(m["time"]) and isinstance(m["seq"], int) for m in body["messages"])
    assert all(set(m) == {"seq", "time", "role", "text"} for m in body["messages"])


def test_messages_can_include_tools_and_reasoning(make_settings, make_client):
    client = make_client(make_settings())
    chat(client, "tool", "s-t")

    messages = client.get(
        "/v1/sessions/s-t/messages", params={"include_tools": "true", "include_reasoning": "true"}
    ).json()["messages"]

    assert [m["role"] for m in messages] == ["user", "assistant", "tool", "assistant"]
    call = messages[1]
    assert call["text"] == ""
    assert call["reasoning"] == "need to run ls"
    assert call["tool_calls"] == [{"id": "call-1", "name": "bash", "arguments": '{"command": "ls"}'}]
    assert messages[2] == {**messages[2], "role": "tool", "text": "hello.py", "tool_call_id": "call-1", "is_error": False}


# ==================== delete ====================


def test_delete_unloaded_session_removes_files_subagents_and_cache(make_settings, tmp_path: Path):
    settings = make_settings()
    home = tmp_path / "home"
    with TestClient(create_app(settings)) as client:
        chat(client, "spawn", "s-del")
        chat(client, "hi", "s-keep")
    cache_dir = home / "storages" / "session_projcache" / "sessions"
    cache_dir.mkdir(parents=True)
    (cache_dir / "s-del.json").write_text("{}")
    (cache_dir / "s-del.json.bak.123").write_text("{}")
    (cache_dir / "s-del-other.json").write_text("{}")
    assert len(session_dirs(home)) == 3

    with TestClient(create_app(settings)) as client:  # fresh runtime: nothing is loaded
        resp = client.delete("/v1/sessions/s-del")
        listed = [s["session_id"] for s in client.get("/v1/sessions").json()["sessions"]]

    assert resp.status_code == 204
    assert listed == ["s-keep"]
    assert session_dirs(home) == ["s-keep"]
    assert sorted(p.name for p in cache_dir.iterdir()) == ["s-del-other.json"]


def test_delete_loaded_session_restarts_runtime_and_keeps_others(make_settings, make_client, fake_dsh_log: Path, tmp_path: Path):
    client = make_client(make_settings())
    chat(client, "one", "s-other")
    chat(client, "hi", "s-loaded")

    resp = client.delete("/v1/sessions/s-loaded")
    other = chat(client, "history", "s-other")
    reused = chat(client, "history", "s-loaded")

    assert resp.status_code == 204
    assert other.json()["final_response"] == "history: one"  # restored from disk by the new runtime
    assert reused.json()["final_response"] == "history: "  # the id starts over as a new session
    assert len(read_launch_log(fake_dsh_log)) == 2


def test_delete_busy_session_returns_409(make_settings, make_client):
    blocking = BlockingHarness()
    settings = make_settings()
    client = make_client(settings, HarnessService(settings, harness_factory=lambda: blocking))
    import threading

    worker = threading.Thread(target=lambda: chat(client, "work", "s-busy"))
    worker.start()
    assert blocking.started.wait(5)

    resp = client.delete("/v1/sessions/s-busy")
    blocking.release.set()
    worker.join(5)

    assert resp.status_code == 409
    assert resp.json()["code"] == "session_busy"


def test_session_status_reports_running_and_queued(make_settings):
    blocking = BlockingHarness()
    service = HarnessService(make_settings(), harness_factory=lambda: blocking)

    async def scenario() -> None:
        first = service.open_stream("a", "s-a")
        second = service.open_stream("b", "s-b")
        await asyncio.to_thread(blocking.started.wait, 5)
        assert service.session_status("s-a") == "running"
        assert service.session_status("s-b") == "queued"
        assert service.session_status("s-c") == "idle"
        blocking.release.set()
        await first.result()
        await second.result()
        assert service.session_status("s-a") == "idle"
        await service.close()

    asyncio.run(scenario())


# ==================== scoping ====================


def test_session_api_is_scoped_per_token(make_settings, make_client):
    client = make_client(make_settings(SERVICE_API_KEY="k1,k2"))
    k1 = {"Authorization": "Bearer k1"}
    k2 = {"Authorization": "Bearer k2"}
    chat(client, "mine", "s-1", token="k1")

    k1_list = client.get("/v1/sessions", headers=k1).json()
    k2_list = client.get("/v1/sessions", headers=k2).json()

    assert [s["session_id"] for s in k1_list["sessions"]] == ["s-1"]
    assert k2_list["total"] == 0
    assert client.get("/v1/sessions/s-1", headers=k1).json()["session_id"] == "s-1"
    assert client.get("/v1/sessions/s-1", headers=k2).status_code == 404
    assert client.get("/v1/sessions/s-1/messages", headers=k2).status_code == 404
    assert client.delete("/v1/sessions/s-1", headers=k2).status_code == 404
    assert client.get("/v1/sessions").status_code == 401
    assert client.delete("/v1/sessions/s-1", headers=k1).status_code == 204


def test_unscoped_listing_hides_token_scoped_sessions(make_settings, tmp_path: Path):
    with TestClient(create_app(make_settings(SERVICE_API_KEY="k1"))) as client:
        chat(client, "scoped", "s-scoped", token="k1")
    with TestClient(create_app(make_settings())) as client:
        chat(client, "plain", "s-plain")
        listed = [s["session_id"] for s in client.get("/v1/sessions").json()["sessions"]]

    assert listed == ["s-plain"]


# ==================== store ====================


def test_read_log_ignores_frame_still_being_written(tmp_path: Path):
    compressor = zstandard.ZstdCompressor()
    header = {"type": "session", "version": 4, "id": "x", "createdAt": 1}
    complete = compressor.compress((json.dumps(header) + "\n" + json.dumps({"type": "turn/start", "seq": 0}) + "\n").encode())
    partial = compressor.compress(b'{"type": "turn/end", "seq": 1}\n{"type": "assist')[:-4]
    log = tmp_path / "session.v4.jsonl.zstd"
    log.write_bytes(complete + partial)

    parsed_header, events = read_log(log)

    assert parsed_header["id"] == "x"
    assert [e["type"] for e in events][:1] == ["turn/start"]
    assert all(e["type"] != "assist" for e in events)
