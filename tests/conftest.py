from __future__ import annotations

import json
import stat
import sys
import threading
from pathlib import Path
from typing import Callable, Iterator, Optional

import pytest
from deepseek_harness import RunResult
from fastapi.testclient import TestClient

from app.config import Settings
from app.harness_service import HarnessService
from app.main import create_app

FAKE_DSH = Path(__file__).with_name("fake_dsh.py")


@pytest.fixture
def fake_dsh_log(tmp_path: Path) -> Path:
    return tmp_path / "fake-dsh.log"


@pytest.fixture
def fake_dsh_bin(tmp_path: Path, fake_dsh_log: Path) -> str:
    """Executable that the real SDK launches as ``dsh_bin``."""
    wrapper = tmp_path / "dsh"
    wrapper.write_text(
        "#!/bin/sh\n"
        f"FAKE_DSH_LOG='{fake_dsh_log}' exec '{sys.executable}' '{FAKE_DSH}' \"$@\"\n",
        encoding="utf-8",
    )
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
    return str(wrapper)


@pytest.fixture
def make_settings(tmp_path: Path, fake_dsh_bin: str) -> Callable[..., Settings]:
    def factory(**overrides: object) -> Settings:
        values: dict[str, object] = {
            "DSH_HOME": str(tmp_path / "home"),
            "DSH_WORKSPACE": str(tmp_path / "workspace"),
            "DSH_BIN": fake_dsh_bin,
            "SERVICE_API_KEY": "",
            "STREAM_HEARTBEAT_SECONDS": 5,
            "INITIALIZE_TIMEOUT_SECONDS": 20,
        }
        values.update(overrides)
        return Settings(_env_file=None, **values)

    return factory


@pytest.fixture
def make_client() -> Iterator[Callable[..., TestClient]]:
    clients: list[TestClient] = []

    def factory(settings: Settings, service: Optional[HarnessService] = None) -> TestClient:
        client = TestClient(create_app(settings, service))
        client.__enter__()  # runs the lifespan
        clients.append(client)
        return client

    yield factory
    for client in clients:
        client.__exit__(None, None, None)


class BlockingHarness:
    """In-process harness whose runs block until ``release`` is set."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.started = threading.Event()

    def run(self, input: str, *, session_id: Optional[str] = None, on_notification=None) -> RunResult:
        self.started.set()
        self.release.wait(10)
        return RunResult(
            session_id=session_id or "",
            final_response=f"done: {input}",
            finish_reason="completed",
            events=[],
            notifications=[],
        )

    def close(self) -> None:
        self.release.set()


def parse_sse(text: str) -> list[dict]:
    events = []
    for block in text.split("\n\n"):
        data_lines = [line[len("data: "):] for line in block.splitlines() if line.startswith("data: ")]
        if data_lines:
            events.append(json.loads("\n".join(data_lines)))
    return events


def _read_log(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def read_launch_log(path: Path) -> list[dict]:
    """One entry per runtime process start."""
    return [entry for entry in _read_log(path) if "argv" in entry]


def read_call_log(path: Path) -> list[tuple[str, str]]:
    """``(call, session_id)`` for every ``session/prompt`` and ``session/resume`` received."""
    return [(entry["call"], entry["session"]) for entry in _read_log(path) if "call" in entry]
