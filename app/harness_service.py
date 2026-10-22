"""Single shared DeepSeek Harness instance used by the HTTP, SSE and WebSocket endpoints.

The official SDK is synchronous and not documented as thread-safe, so every SDK call
runs on one dedicated worker thread: runs are executed strictly one at a time in FIFO
order while the asyncio event loop stays responsive. Notifications emitted by the SDK
on the worker thread are handed to the event loop with ``call_soon_threadsafe``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator, Callable, Optional, Protocol

from deepseek_harness.errors import JsonRpcError

from .config import Settings
from .scopes import scoped_session_id

if TYPE_CHECKING:
    from .session_store import SessionStore, StoredSession

logger = logging.getLogger(__name__)

MAX_ERROR_MESSAGE_CHARS = 4000
CLOSE_TIMEOUT_SECONDS = 15.0
# Runtime error text when a session's working directory does not exist (dsh-working-directory)
_MISSING_DIRECTORY = "directory does not exist"


# ==================== Errors ====================


class ServiceError(Exception):
    status_code = 500
    code = "internal_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class QueueFullError(ServiceError):
    status_code = 429
    code = "queue_full"


class ServiceClosedError(ServiceError):
    status_code = 503
    code = "service_closed"


class RunCancelledError(ServiceError):
    status_code = 503
    code = "cancelled"


class RunTimeoutError(ServiceError):
    status_code = 504
    code = "timeout"


class HarnessRunError(ServiceError):
    status_code = 502
    code = "harness_error"


class SessionNotFoundError(ServiceError):
    status_code = 404
    code = "session_not_found"


class SessionBusyError(ServiceError):
    status_code = 409
    code = "session_busy"


# ==================== Events ====================


class HarnessLike(Protocol):
    def run(
        self,
        input: str,
        *,
        session_id: Optional[str] = None,
        on_notification: Optional[Callable[[Any], None]] = None,
    ) -> Any: ...

    def resume_session(self, session_id: str) -> Any: ...

    def close(self) -> None: ...


HarnessFactory = Callable[[], HarnessLike]
NotificationCallback = Callable[[Any], None]

_DONE = object()


def new_session_id() -> str:
    return f"session-{uuid.uuid4().hex}"


def build_event(event_type: str, session_id: Optional[str], data: Optional[dict] = None) -> dict:
    return {"type": event_type, "session_id": session_id, "data": data or {}}


def error_event(session_id: Optional[str], exc: BaseException) -> dict:
    if isinstance(exc, ServiceError):
        code, message = exc.code, exc.message
    else:
        code, message = "internal_error", _truncate(f"{type(exc).__name__}: {exc}")
    return build_event("error", session_id, {"code": code, "message": message})


def _truncate(text: str, limit: int = MAX_ERROR_MESSAGE_CHARS) -> str:
    return text if len(text) <= limit else text[:limit] + "...(truncated)"


def _turn_error(events: Any) -> Optional[str]:
    """Error message of the last ``turn/end`` when it ended with ``kind == "error"``."""
    for event in reversed(events or []):
        if not isinstance(event, dict) or event.get("type") != "turn/end":
            continue
        data = event.get("data")
        reason = data.get("reason") if isinstance(data, dict) else None
        if not isinstance(reason, dict) or reason.get("kind") != "error":
            return None
        error = reason.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        return "run ended with an error"
    return None


def result_to_dict(result: Any, session_id: str) -> dict:
    return {
        "session_id": session_id,
        "final_response": result.final_response or "",
        "finish_reason": result.finish_reason,
        "error": _turn_error(getattr(result, "events", None)),
    }


def _is_persisted_session_error(exc: JsonRpcError, session_id: str) -> bool:
    """The runtime refuses to recreate a session that an earlier runtime process persisted."""
    return f'session "{session_id}" already exists' in str(exc)


def _drop_blank_runtime_env() -> None:
    """Remove blank ``DSH_*`` / ``DEEPSEEK_*`` variables before the runtime inherits them.

    ``docker compose`` ``env_file`` exports ``KEY=`` lines from ``.env`` as empty
    variables. Settings already treat them as unset, but the runtime reads them
    verbatim (e.g. an empty ``DEEPSEEK_BASE_URL`` fails as an invalid URL).
    """
    blank = [k for k, v in os.environ.items() if k.startswith(("DSH_", "DEEPSEEK_")) and not v.strip()]
    for key in blank:
        del os.environ[key]
    if blank:
        logger.info("ignoring blank environment variables: %s", ", ".join(sorted(blank)))


class RunStream:
    """A submitted run. Iterate :meth:`events` to stream it, or await :meth:`result`."""

    def __init__(
        self,
        session_id: str,
        future: "asyncio.Future[dict]",
        queue: "asyncio.Queue[Any]",
        heartbeat_seconds: float,
    ) -> None:
        self.session_id = session_id
        self._future = future
        self._queue = queue
        self._heartbeat = heartbeat_seconds
        # Scheduled after every notification already queued via call_soon_threadsafe,
        # so the sentinel is always the last item of the stream.
        future.add_done_callback(lambda _f: queue.put_nowait(_DONE))

    async def result(self) -> dict:
        """Wait for the final result; raises a :class:`ServiceError` on failure.

        Cancelling the awaiting task (e.g. the HTTP client disconnected) also drops
        the run if it has not started yet.
        """
        return await self._future

    def cancel(self) -> None:
        """Drop the run if it is still queued; a run that already started keeps going."""
        self._future.cancel()

    async def events(self) -> AsyncIterator[dict]:
        """Yield ``start``, ``notification``/``ping`` events, then one ``result`` or ``error``."""
        getter: Optional[asyncio.Future] = None
        try:
            yield build_event("start", self.session_id)
            while True:
                if getter is None:
                    getter = asyncio.ensure_future(self._queue.get())
                # Waiting on the same getter (instead of wait_for) never loses a queued item.
                done, _ = await asyncio.wait({getter}, timeout=self._heartbeat)
                if not done:
                    yield build_event("ping", self.session_id)
                    continue
                item = getter.result()
                getter = None
                if item is _DONE:
                    break
                yield item
            yield self._final_event()
        finally:
            if getter is not None and not getter.done():
                getter.cancel()
            if not self._future.done():
                # The consumer went away: drop the run if it is still queued.
                # A run that already started keeps going and its session is persisted.
                self._future.cancel()

    def _final_event(self) -> dict:
        if self._future.cancelled():
            return error_event(self.session_id, RunCancelledError("run was cancelled"))
        exc = self._future.exception()
        if exc is not None:
            return error_event(self.session_id, exc)
        return build_event("result", self.session_id, self._future.result())


# ==================== Service ====================


class HarnessService:
    def __init__(self, settings: Settings, harness_factory: Optional[HarnessFactory] = None) -> None:
        self._settings = settings
        self._factory: HarnessFactory = harness_factory or self._create_sdk_harness
        self._excluded_event_types = settings.excluded_event_types
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dsh-harness")
        self._harness: Optional[HarnessLike] = None  # only touched on the worker thread (and close)
        self._pending = 0
        self._pending_lock = threading.Lock()
        self._active_sessions: Counter[str] = Counter()  # runtime session id -> queued + running runs
        self._deleting_sessions: set[str] = set()
        self._running_session: Optional[str] = None
        self._busy = False
        self._closed = False
        self._prepare_filesystem()

    # ---------- public API (event loop thread) ----------

    def status(self) -> dict:
        return {"started": self._harness is not None, "busy": self._busy, "pending": self._pending}

    def session_status(self, runtime_session_id: str) -> str:
        """``running``, ``queued`` or ``idle`` from this service's point of view."""
        if self._running_session == runtime_session_id:
            return "running"
        return "queued" if self._active_sessions.get(runtime_session_id) else "idle"

    def open_stream(
        self,
        prompt: str,
        session_id: Optional[str] = None,
        *,
        scope: Optional[str] = None,
        include_notifications: bool = True,
    ) -> RunStream:
        """Queue a run and return its stream. Raises :class:`QueueFullError` immediately when full.

        ``session_id`` is what the client sees; ``scope`` (one per auth token) keeps
        clients with different tokens from reaching each other's sessions.
        """
        if self._closed:
            raise ServiceClosedError("service is shutting down")
        sid = session_id or new_session_id()
        runtime_sid = scoped_session_id(scope, sid)
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Any] = asyncio.Queue()
        callback = self._make_notification_callback(loop, queue, sid) if include_notifications else None

        with self._pending_lock:
            if runtime_sid in self._deleting_sessions:
                raise SessionBusyError(f"session {sid!r} is being deleted")
            if self._pending >= self._settings.max_pending_runs:
                raise QueueFullError(
                    f"too many pending runs (MAX_PENDING_RUNS={self._settings.max_pending_runs}), retry later"
                )
            self._pending += 1
            self._active_sessions[runtime_sid] += 1
        try:
            cfuture = self._executor.submit(self._execute, prompt, sid, runtime_sid, callback)
        except RuntimeError as exc:  # executor already shut down
            self._release_slot(runtime_sid)
            raise ServiceClosedError("service is shutting down") from exc
        cfuture.add_done_callback(lambda _f: self._release_slot(runtime_sid))
        return RunStream(sid, asyncio.wrap_future(cfuture, loop=loop), queue, self._settings.stream_heartbeat_seconds)

    async def run(self, prompt: str, session_id: Optional[str] = None, *, scope: Optional[str] = None) -> dict:
        return await self.open_stream(prompt, session_id, scope=scope, include_notifications=False).result()

    async def delete_session(self, store: "SessionStore", runtime_session_id: str, public_id: str) -> int:
        """Delete a root session plus its subagent sessions; returns how many logs were removed.

        Raises :class:`SessionBusyError` while the session has queued/running runs, and
        :class:`SessionNotFoundError` when it does not exist (or is a subagent session).
        """
        with self._pending_lock:
            if self._active_sessions.get(runtime_session_id) or runtime_session_id in self._deleting_sessions:
                raise SessionBusyError(f"session {public_id!r} has queued or running tasks; retry when it is idle")
            self._deleting_sessions.add(runtime_session_id)
        try:
            root = await asyncio.to_thread(store.get, runtime_session_id)
            if root is None or root.parent_id is not None:
                raise SessionNotFoundError(f"session {public_id!r} not found")
            sessions = await asyncio.to_thread(store.with_descendants, root)
            loaded = await asyncio.to_thread(lambda: any(store.is_open(s) for s in sessions))
            if loaded:
                # The runtime keeps the session in memory; only a runtime restart can drop it,
                # which must not interrupt another session's run.
                if self._closed:
                    raise ServiceClosedError("service is shutting down")
                cfuture = self._executor.submit(self._delete_loaded, store, sessions, public_id)
                await asyncio.wrap_future(cfuture)
            else:
                await asyncio.to_thread(store.delete, sessions)
            return len(sessions)
        finally:
            with self._pending_lock:
                self._deleting_sessions.discard(runtime_session_id)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        harness = self._harness
        if harness is not None:
            # Closing from outside the worker also unblocks a run that is still in progress.
            try:
                await asyncio.wait_for(asyncio.to_thread(harness.close), timeout=CLOSE_TIMEOUT_SECONDS)
            except Exception:
                logger.warning("closing the harness runtime failed or timed out", exc_info=True)
        self._executor.shutdown(wait=False, cancel_futures=True)
        logger.info("harness service closed")

    # ---------- worker thread ----------

    def _execute(
        self,
        prompt: str,
        session_id: str,
        runtime_session_id: str,
        on_notification: Optional[NotificationCallback],
    ) -> dict:
        self._busy = True
        self._running_session = runtime_session_id
        started = time.monotonic()
        logger.info("run start session=%s prompt_chars=%d", runtime_session_id, len(prompt))
        timed_out = threading.Event()
        timer: Optional[threading.Timer] = None
        try:
            if self._harness is None:
                self._harness = self._factory()
            harness = self._harness
            timer = self._start_watchdog(harness, runtime_session_id, timed_out)
            result = self._run_session(harness, prompt, runtime_session_id, on_notification)
            payload = result_to_dict(result, session_id)
            logger.info(
                "run done session=%s finish_reason=%s elapsed=%.1fs",
                runtime_session_id,
                payload["finish_reason"],
                time.monotonic() - started,
            )
            return payload
        except Exception as exc:
            if timed_out.is_set():
                self._discard_harness()
                raise RunTimeoutError(
                    f"run exceeded RUN_TIMEOUT_SECONDS={self._settings.run_timeout_seconds}; runtime restarted"
                ) from exc
            # A JSON-RPC error is an answer from a healthy runtime; anything else
            # (transport closed, protocol error, startup failure...) gets a fresh runtime next time.
            if not isinstance(exc, JsonRpcError):
                self._discard_harness()
            logger.warning(
                "run failed session=%s elapsed=%.1fs error=%s: %s",
                runtime_session_id,
                time.monotonic() - started,
                type(exc).__name__,
                _truncate(str(exc), 500),
            )
            raise HarnessRunError(_truncate(f"{type(exc).__name__}: {exc}")) from exc
        finally:
            if timer is not None:
                timer.cancel()
            self._running_session = None
            self._busy = False

    def _delete_loaded(self, store: "SessionStore", sessions: list["StoredSession"], public_id: str) -> None:
        """Worker thread: restart the runtime to release the sessions, then delete them.

        Other sessions loaded in the runtime are restored from disk on their next run.
        """
        if any(store.is_open(s) for s in sessions):
            if self._harness is not None:
                logger.info("restarting runtime to release session=%s before deletion", sessions[0].id)
                self._discard_harness()
            if any(store.is_open(s) for s in sessions):
                raise SessionBusyError(f"session {public_id!r} is held by another process")
        store.delete(sessions)

    def _run_session(
        self,
        harness: HarnessLike,
        prompt: str,
        session_id: str,
        on_notification: Optional[NotificationCallback],
    ) -> Any:
        """Run in a session that is new, open in this runtime, or persisted by an earlier one.

        A runtime process only knows sessions it created or restored itself; after a
        service or runtime restart, a persisted session must be resumed first.
        """
        try:
            return harness.run(prompt, session_id=session_id, on_notification=on_notification)
        except JsonRpcError as exc:
            if not _is_persisted_session_error(exc, session_id):
                raise
        logger.info("restoring persisted session=%s", session_id)
        session = harness.resume_session(session_id)
        self._ensure_working_directory(session, session_id)
        return session.run(prompt, on_notification=on_notification)

    def _ensure_working_directory(self, session: Any, session_id: str) -> None:
        """Move a restored session whose working directory no longer exists to DSH_WORKSPACE.

        A session keeps the directory it was created in; that path is gone when, for
        example, a session created by a local run is continued inside the container.
        """
        try:
            session.get_working_directory()
        except JsonRpcError as exc:
            if _MISSING_DIRECTORY not in str(exc):
                raise
            workspace = str(self._settings.workspace_dir)
            session.set_working_directory(workspace)
            logger.warning(
                "session=%s working directory is gone (%s); moved to %s",
                session_id,
                _truncate(str(exc), 200),
                workspace,
            )

    def _start_watchdog(
        self, harness: HarnessLike, session_id: str, flag: threading.Event
    ) -> Optional[threading.Timer]:
        timeout = self._settings.run_timeout_seconds
        if not timeout:
            return None

        def abort() -> None:
            flag.set()
            logger.warning("run timed out after %ss session=%s; closing runtime", timeout, session_id)
            try:
                harness.close()  # fails the blocked SDK call with TransportClosedError
            except Exception:
                logger.exception("failed to close timed-out runtime")

        timer = threading.Timer(timeout, abort)
        timer.daemon = True
        timer.start()
        return timer

    def _discard_harness(self) -> None:
        harness, self._harness = self._harness, None
        if harness is None:
            return
        try:
            harness.close()
        except Exception:
            logger.exception("failed to close harness runtime")

    # ---------- helpers ----------

    def _make_notification_callback(
        self, loop: asyncio.AbstractEventLoop, queue: "asyncio.Queue[Any]", session_id: str
    ) -> NotificationCallback:
        def on_notification(notification: Any) -> None:
            event = self._notification_event(session_id, notification)
            if event is None:
                return
            try:
                loop.call_soon_threadsafe(queue.put_nowait, event)
            except RuntimeError:  # event loop closed (shutdown); nobody is listening anymore
                pass

        return on_notification

    def _notification_event(self, session_id: str, notification: Any) -> Optional[dict]:
        method = getattr(notification, "method", "")
        payload = getattr(notification, "payload", None) or {}
        if method == "session.event":
            event = payload.get("event")
            if isinstance(event, dict) and event.get("type") in self._excluded_event_types:
                return None
        return build_event("notification", session_id, {"method": method, "params": payload})

    def _release_slot(self, runtime_session_id: str) -> None:
        with self._pending_lock:
            self._pending -= 1
            self._active_sessions[runtime_session_id] -= 1
            if self._active_sessions[runtime_session_id] <= 0:
                del self._active_sessions[runtime_session_id]

    def _prepare_filesystem(self) -> None:
        s = self._settings
        s.home_dir.mkdir(parents=True, exist_ok=True)
        s.workspace_dir.mkdir(parents=True, exist_ok=True)
        for patch in s.patches:
            if not Path(patch).is_file():
                raise FileNotFoundError(f"patch file not found: {patch}")
        _drop_blank_runtime_env()

    def _create_sdk_harness(self) -> HarnessLike:
        from deepseek_harness import DeepSeekHarness

        s = self._settings
        logger.info(
            "creating DeepSeek Harness profile=%s provider=%s model=%s home=%s workspace=%s permission=%s",
            s.dsh_profile,
            s.dsh_provider,
            s.dsh_model,
            s.home_dir,
            s.workspace_dir,
            s.dsh_permission_mode,
        )
        return DeepSeekHarness(
            provider=s.dsh_provider,
            model=s.dsh_model,
            reasoning_effort=s.dsh_reasoning_effort,
            max_tokens=s.dsh_max_tokens,
            cwd=str(s.workspace_dir),
            dsh_home=str(s.home_dir),
            profile=s.dsh_profile,
            patches=s.patches,
            dsh_bin=s.dsh_bin,
            env={"DSH_PERMISSION_MODE": s.dsh_permission_mode},
            initialize_timeout_seconds=s.initialize_timeout_seconds,
            request_timeout_seconds=s.request_timeout_seconds,
            base_url=s.deepseek_base_url,
            api_key=s.deepseek_api_key,
        )
