"""Service configuration loaded from environment variables and an optional `.env` file."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
PATCHES_DIR = BASE_DIR / "patches"
DISABLE_SESSION_LOG_PATCH = PATCHES_DIR / "disable-session-log.patch.yml"


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # ==================== Server ====================
    host: str = Field(default="0.0.0.0", alias="HOST")
    port: int = Field(default=8080, alias="PORT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    # Comma separated Bearer tokens; empty disables authentication.
    service_api_key: str = Field(default="", alias="SERVICE_API_KEY")

    # ==================== Model / credentials ====================
    deepseek_api_key: Optional[str] = Field(default=None, alias="DEEPSEEK_API_KEY")
    deepseek_base_url: Optional[str] = Field(default=None, alias="DEEPSEEK_BASE_URL")
    dsh_provider: str = Field(default="deepseek-official", alias="DSH_PROVIDER")
    dsh_model: str = Field(default="deepseek-v4-flash", alias="DSH_MODEL")
    dsh_reasoning_effort: Optional[str] = Field(default=None, alias="DSH_REASONING_EFFORT")
    dsh_max_tokens: Optional[int] = Field(default=None, gt=0, alias="DSH_MAX_TOKENS")

    # ==================== Harness runtime ====================
    dsh_profile: str = Field(default="sdk", alias="DSH_PROFILE")
    dsh_home: Path = Field(default=BASE_DIR / "data" / "home", alias="DSH_HOME")
    dsh_workspace: Path = Field(default=BASE_DIR / "data" / "workspace", alias="DSH_WORKSPACE")
    dsh_permission_mode: Literal["read-only", "workspace-write", "danger-full-access"] = Field(
        default="workspace-write", alias="DSH_PERMISSION_MODE"
    )
    # Optional alternative dsh executable; defaults to the runtime bundled with the SDK.
    dsh_bin: Optional[str] = Field(default=None, alias="DSH_BIN")
    disable_session_log_upload: bool = Field(default=True, alias="DISABLE_SESSION_LOG_UPLOAD")
    # Comma separated absolute paths of additional cordis patch files.
    dsh_extra_patches: str = Field(default="", alias="DSH_EXTRA_PATCHES")

    initialize_timeout_seconds: float = Field(default=60.0, gt=0, alias="INITIALIZE_TIMEOUT_SECONDS")
    request_timeout_seconds: Optional[float] = Field(default=None, gt=0, alias="REQUEST_TIMEOUT_SECONDS")
    # Upper bound for one whole run; the runtime is restarted when it is exceeded. Empty = unlimited.
    run_timeout_seconds: Optional[float] = Field(default=None, gt=0, alias="RUN_TIMEOUT_SECONDS")

    # ==================== Scheduling / streaming ====================
    # Runs are executed one at a time; this bounds running + queued runs.
    max_pending_runs: int = Field(default=8, ge=1, alias="MAX_PENDING_RUNS")
    stream_heartbeat_seconds: float = Field(default=15.0, gt=0, alias="STREAM_HEARTBEAT_SECONDS")
    # session.event types not forwarded to SSE/WS clients (they are large and rarely useful).
    stream_excluded_event_types: str = Field(
        default="system/message,request/header", alias="STREAM_EXCLUDED_EVENT_TYPES"
    )

    @field_validator(
        "deepseek_api_key",
        "deepseek_base_url",
        "dsh_reasoning_effort",
        "dsh_max_tokens",
        "dsh_bin",
        "request_timeout_seconds",
        "run_timeout_seconds",
        mode="before",
    )
    @classmethod
    def _empty_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @property
    def service_api_keys(self) -> list[str]:
        return _split_csv(self.service_api_key)

    @property
    def home_dir(self) -> Path:
        return self.dsh_home.expanduser().resolve()

    @property
    def workspace_dir(self) -> Path:
        return self.dsh_workspace.expanduser().resolve()

    @property
    def excluded_event_types(self) -> frozenset[str]:
        return frozenset(_split_csv(self.stream_excluded_event_types))

    @property
    def patches(self) -> tuple[str, ...]:
        """Ordered cordis patch overlays passed to the SDK."""
        items: list[str] = []
        if self.disable_session_log_upload:
            items.append(str(DISABLE_SESSION_LOG_PATCH))
        items.extend(str(Path(p).expanduser().resolve()) for p in _split_csv(self.dsh_extra_patches))
        return tuple(items)


@lru_cache
def get_settings() -> Settings:
    return Settings()
