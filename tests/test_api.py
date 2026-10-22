from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.config import DISABLE_SESSION_LOG_PATCH
from app.harness_service import HarnessService, QueueFullError, RunCancelledError
from app.main import create_app
from tests.conftest import BlockingHarness, parse_sse, read_call_log, read_launch_log


def receive_until_final(ws) -> list[dict]:
    events = []
    while True:
        event = ws.receive_json()
        events.append(event)
        if event["type"] in ("result", "error"):
            return events


# ==================== health ====================


def test_healthz_is_public_and_reports_harness_status(make_settings, make_client):
    client = make_client(make_settings(SERVICE_API_KEY="secret"))

    resp = client.get("/healthz")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["sdk_version"] == "0.2.1rc2"
    assert body["harness"] == {"started": False, "busy": False, "pending": 0}


# ==================== HTTP sync ====================


def test_chat_returns_final_response(make_settings, make_client):
    client = make_client(make_settings())

    resp = client.post("/v1/chat", json={"prompt": "hello", "session_id": "s-1"})

    assert resp.status_code == 200
    assert resp.json() == {"session_id": "s-1", "final_response": "echo: hello", "finish_reason": "completed", "error": None}


def test_chat_generates_session_id_when_omitted(make_settings, make_client):
    client = make_client(make_settings())

    resp = client.post("/v1/chat", json={"prompt": "hello"})

    assert resp.status_code == 200
    assert resp.json()["session_id"].startswith("session-")


def test_chat_surfaces_model_error_from_turn_end(make_settings, make_client):
    client = make_client(make_settings())

    resp = client.post("/v1/chat", json={"prompt": "fail-auth"})

    assert resp.status_code == 200
    assert resp.json()["finish_reason"] == "error"
    assert resp.json()["error"] == "Authentication Fails"


@pytest.mark.parametrize(
    "payload",
    [{"prompt": ""}, {"prompt": "   "}, {"prompt": "hi", "session_id": "../escape"}, {}],
)
def test_chat_rejects_invalid_request(make_settings, make_client, payload):
    client = make_client(make_settings())

    assert client.post("/v1/chat", json=payload).status_code == 422


# ==================== SSE ====================


def test_chat_stream_emits_start_notifications_and_result_in_order(make_settings, make_client):
    client = make_client(make_settings())

    resp = client.post("/v1/chat/stream", json={"prompt": "hello", "session_id": "s-sse"})

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["x-session-id"] == "s-sse"
    events = parse_sse(resp.text)
    types = [e["type"] for e in events]
    assert types[0] == "start"
    assert types[-1] == "result"
    assert set(types[1:-1]) == {"notification"}
    assert all(e["session_id"] == "s-sse" for e in events)
    statuses = [e["data"]["params"]["status"] for e in events if e["type"] == "notification" and e["data"]["method"] == "session.status"]
    assert statuses == ["running", "idle"]
    assert events[-1]["data"]["final_response"] == "echo: hello"


def test_chat_stream_drops_excluded_event_types(make_settings, make_client):
    client = make_client(make_settings())

    events = parse_sse(client.post("/v1/chat/stream", json={"prompt": "hello"}).text)

    event_types = [e["data"]["params"]["event"]["type"] for e in events if e["type"] == "notification" and e["data"]["method"] == "session.event"]
    assert "turn/end" in event_types
    assert "system/message" not in event_types


def test_chat_stream_reports_runtime_crash_as_error_event(make_settings, make_client):
    client = make_client(make_settings())

    events = parse_sse(client.post("/v1/chat/stream", json={"prompt": "crash"}).text)

    assert events[-1]["type"] == "error"
    assert events[-1]["data"]["code"] == "harness_error"


# ==================== WebSocket ====================


def test_ws_runs_multiple_turns_on_one_connection(make_settings, make_client):
    client = make_client(make_settings())

    with client.websocket_connect("/v1/ws") as ws:
        ws.send_json({"prompt": "first", "session_id": "s-ws"})
        first = receive_until_final(ws)
        ws.send_json({"prompt": "second", "session_id": "s-ws"})
        second = receive_until_final(ws)

    assert first[0]["type"] == "start"
    assert first[-1]["data"]["final_response"] == "echo: first"
    assert any(e["type"] == "notification" for e in first)
    assert second[-1]["data"]["final_response"] == "echo: second"


def test_ws_invalid_message_returns_error_and_keeps_connection(make_settings, make_client):
    client = make_client(make_settings())

    with client.websocket_connect("/v1/ws") as ws:
        ws.send_text("not json")
        invalid = ws.receive_json()
        ws.send_json({"prompt": ""})
        blank = ws.receive_json()
        ws.send_json({"prompt": "still alive"})
        events = receive_until_final(ws)

    assert invalid["type"] == "error" and invalid["data"]["code"] == "invalid_request"
    assert blank["type"] == "error" and "prompt" in blank["data"]["message"]
    assert events[-1]["data"]["final_response"] == "echo: still alive"


# ==================== auth ====================


def test_http_requires_bearer_token_when_configured(make_settings, make_client):
    client = make_client(make_settings(SERVICE_API_KEY="k1,k2"))

    missing = client.post("/v1/chat", json={"prompt": "hi"})
    wrong = client.post("/v1/chat", json={"prompt": "hi"}, headers={"Authorization": "Bearer nope"})
    ok = client.post("/v1/chat", json={"prompt": "hi"}, headers={"Authorization": "Bearer k2"})
    stream_missing = client.post("/v1/chat/stream", json={"prompt": "hi"})

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert ok.status_code == 200
    assert stream_missing.status_code == 401


def test_ws_rejects_bad_token_with_4401(make_settings, make_client):
    client = make_client(make_settings(SERVICE_API_KEY="k1"))

    with client.websocket_connect("/v1/ws?token=bad") as ws:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_json()
    assert exc_info.value.code == 4401

    with client.websocket_connect("/v1/ws?token=k1") as ws:
        ws.send_json({"prompt": "hi"})
        assert receive_until_final(ws)[-1]["type"] == "result"

    with client.websocket_connect("/v1/ws", headers={"Authorization": "Bearer k1"}) as ws:
        ws.send_json({"prompt": "hi"})
        assert receive_until_final(ws)[-1]["type"] == "result"


# ==================== queueing ====================


def test_full_queue_returns_429(make_settings, make_client):
    blocking = BlockingHarness()
    settings = make_settings(MAX_PENDING_RUNS=1)
    client = make_client(settings, HarnessService(settings, harness_factory=lambda: blocking))
    results: dict = {}

    worker = threading.Thread(target=lambda: results.setdefault("first", client.post("/v1/chat", json={"prompt": "a"})))
    worker.start()
    assert blocking.started.wait(5)

    rejected = client.post("/v1/chat/stream", json={"prompt": "b"})
    status_while_busy = client.get("/healthz").json()["harness"]
    blocking.release.set()
    worker.join(5)

    assert rejected.status_code == 429
    assert rejected.json()["code"] == "queue_full"
    assert status_while_busy == {"started": True, "busy": True, "pending": 1}
    assert results["first"].json()["final_response"] == "done: a"


def test_ws_full_queue_sends_error_event(make_settings, make_client):
    blocking = BlockingHarness()
    settings = make_settings(MAX_PENDING_RUNS=1)
    client = make_client(settings, HarnessService(settings, harness_factory=lambda: blocking))

    worker = threading.Thread(target=lambda: client.post("/v1/chat", json={"prompt": "a"}))
    worker.start()
    assert blocking.started.wait(5)
    with client.websocket_connect("/v1/ws") as ws:
        ws.send_json({"prompt": "b", "session_id": "s-full"})
        event = ws.receive_json()
    blocking.release.set()
    worker.join(5)

    assert event == {"type": "error", "session_id": "s-full", "data": {"code": "queue_full", "message": event["data"]["message"]}}


def test_runs_execute_one_at_a_time_and_release_slots(make_settings):
    blocking = BlockingHarness()
    service = HarnessService(make_settings(MAX_PENDING_RUNS=2), harness_factory=lambda: blocking)

    async def scenario() -> None:
        first = service.open_stream("a")
        second = service.open_stream("b")
        with pytest.raises(QueueFullError):
            service.open_stream("c")
        blocking.release.set()
        assert (await first.result())["final_response"] == "done: a"
        assert (await second.result())["final_response"] == "done: b"
        assert service.status()["pending"] == 0
        await service.close()

    asyncio.run(scenario())


def test_abandoned_stream_cancels_queued_run(make_settings):
    blocking = BlockingHarness()
    service = HarnessService(make_settings(MAX_PENDING_RUNS=2), harness_factory=lambda: blocking)

    async def scenario() -> None:
        first = service.open_stream("a")
        queued = service.open_stream("b")
        events = queued.events()
        assert (await events.__anext__())["type"] == "start"
        await events.aclose()  # consumer disconnects while the run is still queued
        await asyncio.sleep(0)
        assert service.status()["pending"] == 1
        blocking.release.set()
        await first.result()
        await service.close()

    asyncio.run(scenario())


# ==================== runtime lifecycle ====================


def test_launch_uses_configured_profile_patches_and_env(make_settings, make_client, fake_dsh_log: Path, tmp_path: Path):
    client = make_client(make_settings(DSH_PROFILE="sdk-minimal", DSH_PERMISSION_MODE="read-only", DEEPSEEK_API_KEY="sk-test"))

    client.post("/v1/chat", json={"prompt": "hi"})

    [launch] = read_launch_log(fake_dsh_log)
    assert launch["argv"] == ["--profile", "sdk-minimal", "--patch", str(DISABLE_SESSION_LOG_PATCH)]
    assert launch["env"] == {"DSH_HOME": str(tmp_path / "home"), "DSH_PERMISSION_MODE": "read-only", "DEEPSEEK_API_KEY": "sk-test"}
    assert launch["cwd"] == str(tmp_path / "workspace")


def test_session_log_patch_can_be_disabled(make_settings, make_client, fake_dsh_log: Path):
    client = make_client(make_settings(DISABLE_SESSION_LOG_UPLOAD="false"))

    client.post("/v1/chat", json={"prompt": "hi"})

    assert read_launch_log(fake_dsh_log)[0]["argv"] == ["--profile", "sdk"]


def test_runtime_is_reused_and_restarted_after_crash(make_settings, make_client, fake_dsh_log: Path):
    client = make_client(make_settings())

    assert client.post("/v1/chat", json={"prompt": "one"}).status_code == 200
    assert client.post("/v1/chat", json={"prompt": "two"}).status_code == 200
    crashed = client.post("/v1/chat", json={"prompt": "crash"})
    recovered = client.post("/v1/chat", json={"prompt": "three"})

    assert crashed.status_code == 502
    assert crashed.json()["code"] == "harness_error"
    assert recovered.json()["final_response"] == "echo: three"
    assert len(read_launch_log(fake_dsh_log)) == 2


def test_run_timeout_restarts_runtime(make_settings, make_client, fake_dsh_log: Path):
    client = make_client(make_settings(RUN_TIMEOUT_SECONDS=1))

    timed_out = client.post("/v1/chat", json={"prompt": "hang"})
    recovered = client.post("/v1/chat", json={"prompt": "after"})

    assert timed_out.status_code == 504
    assert timed_out.json()["code"] == "timeout"
    assert recovered.json()["final_response"] == "echo: after"
    assert len(read_launch_log(fake_dsh_log)) == 2


# ==================== session persistence ====================


def test_new_session_is_created_without_resume(make_settings, make_client, fake_dsh_log: Path):
    client = make_client(make_settings())

    client.post("/v1/chat", json={"prompt": "hi", "session_id": "s-new"})

    assert read_call_log(fake_dsh_log) == [("prompt", "s-new")]


def test_session_continues_after_runtime_restart(make_settings, make_client, fake_dsh_log: Path):
    client = make_client(make_settings())

    client.post("/v1/chat", json={"prompt": "one", "session_id": "s-r"})
    assert client.post("/v1/chat", json={"prompt": "crash", "session_id": "s-c"}).status_code == 502
    resp = client.post("/v1/chat", json={"prompt": "history", "session_id": "s-r"})

    assert resp.status_code == 200
    assert resp.json()["final_response"] == "history: one"
    assert ("resume", "s-r") in read_call_log(fake_dsh_log)


def test_session_continues_after_service_restart(make_settings):
    settings = make_settings()
    with TestClient(create_app(settings)) as client:
        client.post("/v1/chat", json={"prompt": "one", "session_id": "s-svc"})

    with TestClient(create_app(settings)) as client:
        sse_events = parse_sse(client.post("/v1/chat/stream", json={"prompt": "two", "session_id": "s-svc"}).text)
        resp = client.post("/v1/chat", json={"prompt": "history", "session_id": "s-svc"})

    assert sse_events[-1]["type"] == "result"
    assert resp.json()["final_response"] == "history: one|two"


# ==================== per-token session scoping ====================


def test_sessions_are_scoped_per_token(make_settings, make_client, fake_dsh_log: Path):
    client = make_client(make_settings(SERVICE_API_KEY="k1,k2"))
    k1 = {"Authorization": "Bearer k1"}
    k2 = {"Authorization": "Bearer k2"}

    client.post("/v1/chat", json={"prompt": "one", "session_id": "shared"}, headers=k1)
    other = client.post("/v1/chat", json={"prompt": "history", "session_id": "shared"}, headers=k2)
    own = client.post("/v1/chat", json={"prompt": "history", "session_id": "shared"}, headers=k1)

    assert other.json()["session_id"] == "shared"
    assert other.json()["final_response"] == "history: "
    assert own.json()["session_id"] == "shared"
    assert own.json()["final_response"] == "history: one"
    runtime_ids = {session for _, session in read_call_log(fake_dsh_log)}
    assert len(runtime_ids) == 2
    assert all(rid.endswith("--shared") and rid != "shared" for rid in runtime_ids)


def test_ws_uses_the_token_scope(make_settings, make_client):
    client = make_client(make_settings(SERVICE_API_KEY="k1,k2"))
    client.post("/v1/chat", json={"prompt": "one", "session_id": "shared"}, headers={"Authorization": "Bearer k1"})

    with client.websocket_connect("/v1/ws?token=k1") as ws:
        ws.send_json({"prompt": "history", "session_id": "shared"})
        events = receive_until_final(ws)

    assert {e["session_id"] for e in events} == {"shared"}
    assert events[-1]["data"]["final_response"] == "history: one"


def test_generated_session_id_is_returned_without_scope_prefix(make_settings, make_client):
    client = make_client(make_settings(SERVICE_API_KEY="k1"))

    resp = client.post("/v1/chat", json={"prompt": "hi"}, headers={"Authorization": "Bearer k1"})

    assert resp.json()["session_id"].startswith("session-")


def test_session_id_is_unchanged_without_auth(make_settings, make_client, fake_dsh_log: Path):
    client = make_client(make_settings())

    client.post("/v1/chat", json={"prompt": "hi", "session_id": "s-plain"})

    assert read_call_log(fake_dsh_log) == [("prompt", "s-plain")]


def test_blank_runtime_env_vars_are_not_inherited(make_settings, make_client, fake_dsh_log: Path, monkeypatch):
    # docker compose `env_file` exports blank .env entries (e.g. `DEEPSEEK_BASE_URL=`) as empty
    # variables; the runtime would read them as values (an empty base URL is an invalid URL).
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "")
    monkeypatch.setenv("DSH_REASONING_EFFORT", "  ")
    monkeypatch.setenv("DSH_MODEL", "deepseek-v4-flash")
    client = make_client(make_settings())

    resp = client.post("/v1/chat", json={"prompt": "hi"})

    assert resp.status_code == 200
    assert read_launch_log(fake_dsh_log)[0]["empty_env"] == []


def test_resumed_session_moves_to_current_workspace_when_its_directory_is_gone(make_settings, tmp_path: Path):
    old_ws, new_ws = tmp_path / "old-ws", tmp_path / "new-ws"
    with TestClient(create_app(make_settings(DSH_WORKSPACE=str(old_ws)))) as client:
        assert chat_ok(client, "one", "s-moved") == "echo: one"
    old_ws.rmdir()  # e.g. created outside docker, now resumed inside the container

    with TestClient(create_app(make_settings(DSH_WORKSPACE=str(new_ws)))) as client:
        history = chat_ok(client, "history", "s-moved")
        pwd = chat_ok(client, "pwd", "s-moved")

    assert history == "history: one"
    assert pwd == f"pwd: {new_ws}"


def test_resumed_session_keeps_existing_working_directory(make_settings, tmp_path: Path, fake_dsh_log: Path):
    old_ws, new_ws = tmp_path / "old-ws", tmp_path / "new-ws"
    with TestClient(create_app(make_settings(DSH_WORKSPACE=str(old_ws)))) as client:
        chat_ok(client, "one", "s-kept")

    with TestClient(create_app(make_settings(DSH_WORKSPACE=str(new_ws)))) as client:
        pwd = chat_ok(client, "pwd", "s-kept")

    assert pwd == f"pwd: {old_ws}"
    assert ("set-cwd", "s-kept") not in read_call_log(fake_dsh_log)


def chat_ok(client, prompt: str, session_id: str) -> str:
    resp = client.post("/v1/chat", json={"prompt": prompt, "session_id": session_id})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["finish_reason"] == "completed", body
    return body["final_response"]


def test_sync_chat_drops_queued_run_when_client_disconnects(make_settings):
    from app.routes import result_unless_disconnected

    class DisconnectedRequest:
        async def receive(self) -> dict:
            return {"type": "http.disconnect"}

    blocking = BlockingHarness()
    service = HarnessService(make_settings(MAX_PENDING_RUNS=2), harness_factory=lambda: blocking)

    async def scenario() -> None:
        first = service.open_stream("a", "s-first", include_notifications=False)
        queued = service.open_stream("b", "s-queued", include_notifications=False)
        with pytest.raises(RunCancelledError):
            await result_unless_disconnected(queued, DisconnectedRequest())
        await asyncio.sleep(0)
        assert service.status()["pending"] == 1
        blocking.release.set()
        assert (await first.result())["final_response"] == "done: a"
        await service.close()

    asyncio.run(scenario())
