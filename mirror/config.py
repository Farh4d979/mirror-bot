"""Configuration loaded from environment variables / .env."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from .utils import parse_size

load_dotenv()


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_ids(name: str) -> frozenset[int]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return frozenset()
    ids: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            ids.add(int(part))
    return frozenset(ids)


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    allowed_user_ids: frozenset[int] = field(default_factory=frozenset)
    max_file_size: int = 0
    max_concurrent_jobs: int = 2
    job_timeout: int = 2 * 60 * 60
    run_seconds: int = 0
    local_mode: bool = False
    auto_handoff: bool = False
    actions_repo: str | None = None
    actions_ref: str | None = None
    remote_jobs: bool = False
    remote_job_key: str | None = None
    remote_workflow: str = "job.yml"
    remote_state_file: Path = Path(".state/remote-jobs.json")
    download_dir: Path = Path("downloads")
    state_file: Path = Path(".state/telegram.json")
    gofile_token: str | None = None
    pixeldrain_api_key: str | None = None
    cookies_file: Path | None = None
    multi_file_mode: str = "largest"
    ytdlp_format: str = "bv*+ba/b"
    aria2c_path: str = "aria2c"
    payload_dumper_path: str = "payload-dumper-go"
    allow_private_urls: bool = False
    log_level: str = "INFO"


def load_settings() -> Settings:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is not set. Create a bot with @BotFather and put the token "
            "in your environment or .env file."
        )

    multi_file_mode = os.getenv("MULTI_FILE_MODE", "largest").strip().lower()
    if multi_file_mode not in {"largest", "zip"}:
        multi_file_mode = "largest"

    cookies = os.getenv("COOKIES_FILE", "").strip()
    download_dir = Path(os.getenv("DOWNLOAD_DIR", "downloads")).expanduser()
    state_file = Path(os.getenv("STATE_FILE", ".state/telegram.json")).expanduser()
    remote_state_file = Path(
        os.getenv("REMOTE_STATE_FILE", ".state/remote-jobs.json")
    ).expanduser()
    local_mode = _env_bool("LOCAL_MODE", False)

    return Settings(
        telegram_token=token,
        allowed_user_ids=_env_ids("ALLOWED_USER_IDS"),
        max_file_size=parse_size(os.getenv("MAX_FILE_SIZE"), 0),
        max_concurrent_jobs=max(1, _env_int("MAX_CONCURRENT_JOBS", 2)),
        job_timeout=max(60, _env_int("JOB_TIMEOUT", 2 * 60 * 60)),
        run_seconds=max(0, _env_int("BOT_RUN_SECONDS", 0)),
        local_mode=local_mode,
        auto_handoff=_env_bool("AUTO_HANDOFF", local_mode),
        actions_repo=os.getenv("ACTIONS_REPO", "").strip() or None,
        actions_ref=os.getenv("ACTIONS_REF", "").strip() or None,
        remote_jobs=_env_bool("REMOTE_JOBS", False),
        remote_job_key=os.getenv("REMOTE_JOB_KEY", "").strip() or None,
        remote_workflow=os.getenv("REMOTE_WORKFLOW", "").strip() or "job.yml",
        remote_state_file=remote_state_file,
        download_dir=download_dir,
        state_file=state_file,
        gofile_token=os.getenv("GOFILE_TOKEN", "").strip() or None,
        pixeldrain_api_key=os.getenv("PIXELDRAIN_API_KEY", "").strip() or None,
        cookies_file=Path(cookies).expanduser() if cookies else None,
        multi_file_mode=multi_file_mode,
        ytdlp_format=os.getenv("YTDLP_FORMAT", "bv*+ba/b").strip() or "bv*+ba/b",
        aria2c_path=os.getenv("ARIA2C_PATH", "aria2c").strip() or "aria2c",
        payload_dumper_path=os.getenv("PAYLOAD_DUMPER_PATH", "payload-dumper-go").strip()
        or "payload-dumper-go",
        allow_private_urls=_env_bool("ALLOW_PRIVATE_URLS", False),
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
    )
