"""One-shot GitHub Actions worker: run exactly one mirror job.

The PC controller creates the Telegram progress message and dispatches this
process with an encrypted payload.  The worker decrypts it, downloads/upload,
edits that message directly through the Bot API, and posts the final card.
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from dataclasses import replace
from typing import Any

from .config import Settings, load_settings
from .dumper import normalize_partitions
from .errors import DumpError, JobCancelled, JobTimeout, MirrorError
from .jobs import Job, JobManager
from .remote import PAYLOAD_VERSION, RemoteJobError, decrypt_payload
from .render import render_final, render_job
from .telegram_api import TelegramAPI, TelegramAPIError
from .utils import sanitize_filename

logger = logging.getLogger(__name__)

TELEGRAM_FILE_LIMIT = 20 * 1024 * 1024
PROGRESS_INTERVAL = 4.0
SOURCE_KINDS = frozenset({"url", "torrent_file", "telegram_file"})


def _cancel_keyboard(job_id: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": "Cancel job", "callback_data": f"cancel:{job_id}"}]
        ]
    }


def _empty_keyboard() -> dict[str, Any]:
    return {"inline_keyboard": []}


def _settings_from_payload(
    settings: Settings, raw: Any
) -> Settings:
    """Apply the controller's limits and upload credentials to the worker."""
    if not isinstance(raw, dict):
        return settings

    changes: dict[str, Any] = {}
    try:
        if "max_file_size" in raw:
            value = int(raw["max_file_size"])
            if value < 0:
                raise ValueError
            changes["max_file_size"] = value
        if "job_timeout" in raw:
            value = int(raw["job_timeout"])
            if value < 60:
                raise ValueError
            changes["job_timeout"] = value
        if str(raw.get("multi_file_mode") or "") in {"largest", "zip"}:
            changes["multi_file_mode"] = str(raw["multi_file_mode"])
        if "ytdlp_format" in raw and str(raw["ytdlp_format"] or "").strip():
            changes["ytdlp_format"] = str(raw["ytdlp_format"]).strip()
        # Empty payload credentials fall back to the worker's repository
        # secrets. This lets the existing GOFILE_TOKEN / PIXELDRAIN_API_KEY
        # secrets work even when the PC's .env does not repeat them.
        if "gofile_token" in raw:
            token = str(raw["gofile_token"] or "").strip()
            if token:
                changes["gofile_token"] = token
        if "pixeldrain_api_key" in raw:
            api_key = str(raw["pixeldrain_api_key"] or "").strip()
            if api_key:
                changes["pixeldrain_api_key"] = api_key
    except (TypeError, ValueError) as exc:
        raise RemoteJobError("The job payload has invalid settings.") from exc
    return replace(settings, **changes)


def job_from_payload(
    payload: dict[str, Any], settings: Settings
) -> tuple[Job, int]:
    """Validate a decrypted payload and return the job plus its message id."""
    if int(payload.get("version") or 0) != PAYLOAD_VERSION:
        raise RemoteJobError("Unsupported job payload version.")

    try:
        job_id = str(payload["job_id"])
        user_id = int(payload["user_id"])
        chat_id = int(payload["chat_id"])
        message_id = int(payload["message_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RemoteJobError("The job payload is missing required fields.") from exc
    if not job_id or message_id <= 0:
        raise RemoteJobError("The job payload has an invalid job or message id.")

    source_kind = str(payload.get("source_kind") or "url")
    if source_kind not in SOURCE_KINDS:
        raise RemoteJobError("The job payload has an unknown source type.")
    source = str(payload.get("source") or "")
    if source_kind == "url" and not source:
        raise RemoteJobError("The job payload has no source link.")

    file_id = str(payload.get("file_id") or "") or None
    if source_kind in {"torrent_file", "telegram_file"} and not file_id:
        raise RemoteJobError("The job payload has no Telegram file id.")

    dump_partitions: tuple[str, ...] = ()
    raw_dump = payload.get("dump_partitions") or []
    if raw_dump:
        if not isinstance(raw_dump, list):
            raise RemoteJobError("The job payload has invalid dump partitions.")
        try:
            dump_partitions = normalize_partitions(raw_dump)
        except DumpError as exc:
            raise RemoteJobError(str(exc)) from exc

    file_name = str(payload.get("file_name") or "") or None
    if file_name:
        file_name = sanitize_filename(file_name, "file.bin")
    try:
        file_size = int(payload["file_size"]) if payload.get("file_size") else None
    except (TypeError, ValueError):
        file_size = None
    if file_size is not None and file_size < 0:
        file_size = None

    now = time.time()
    job = Job(
        id=job_id,
        user_id=user_id,
        chat_id=chat_id,
        source=source or (file_name or "file"),
        host=str(payload.get("host") or "gofile"),
        user_name=str(payload.get("user_name") or ""),
        source_kind=source_kind,
        telegram_file_id=file_id,
        status="running",
        stage="resolving",
        detail="Starting on GitHub Actions…",
        file_name=file_name,
        file_size=file_size,
        dump_partitions=dump_partitions,
        created_at=now,
        started_at=now,
    )
    return job, message_id


class ProgressReporter:
    """Throttled edits of the message created by the PC controller."""

    def __init__(
        self,
        api: TelegramAPI,
        job: Job,
        message_id: int,
        *,
        interval: float = PROGRESS_INTERVAL,
    ) -> None:
        self.api = api
        self.job = job
        self.message_id = message_id
        self.interval = interval
        self.last_edit = 0.0
        self.last_text = ""

    def __call__(self, *, force: bool = False) -> None:
        text = render_job(self.job)
        if text == self.last_text:
            return
        now = time.monotonic()
        if not force and now - self.last_edit < self.interval:
            return
        try:
            self.api.edit_message(
                self.job.chat_id,
                self.message_id,
                text,
                reply_markup=_cancel_keyboard(self.job.id),
            )
        except TelegramAPIError as exc:
            logger.warning("Could not update the progress message: %s", exc)
        else:
            self.last_text = text
        self.last_edit = now

    def edit_final(self, text: str) -> None:
        self.api.edit_message(
            self.job.chat_id,
            self.message_id,
            text,
            reply_markup=_empty_keyboard(),
        )


def _deliver_final(
    api: TelegramAPI, job: Job, message_id: int, text: str
) -> None:
    """Delete the live message and send a clean result card."""
    deleted = False
    try:
        deleted = api.delete_message(job.chat_id, message_id)
    except TelegramAPIError as exc:
        logger.warning("Could not delete the progress message: %s", exc)

    if not deleted:
        try:
            api.edit_message(
                job.chat_id,
                message_id,
                text,
                reply_markup=_empty_keyboard(),
            )
            return
        except TelegramAPIError as exc:
            logger.warning("Could not edit the progress message: %s", exc)

    try:
        api.send_message(job.chat_id, text)
    except TelegramAPIError as exc:
        logger.error("Could not deliver the final job card: %s", exc)


def _download_telegram_source(
    api: TelegramAPI,
    job: Job,
    settings: Settings,
    reporter: ProgressReporter,
) -> None:
    name = sanitize_filename(job.file_name or job.source or "file.bin", "file.bin")
    folder = settings.download_dir / "uploads" / job.id
    path = folder / name
    job.update(stage="resolving", detail=f"Downloading {name} from Telegram…")
    reporter(force=True)
    limit = settings.max_file_size or TELEGRAM_FILE_LIMIT
    api.download_file(
        job.telegram_file_id or "",
        path,
        max_size=limit,
        cancel=job.cancel,
    )
    size = path.stat().st_size
    job.update(local_path=path, file_name=name, file_size=size)
    reporter(force=True)


def _run_job(
    api: TelegramAPI,
    job: Job,
    message_id: int,
    settings: Settings,
) -> int:
    reporter = ProgressReporter(api, job, message_id)
    reporter(force=True)

    job.cancel.deadline = time.monotonic() + settings.job_timeout
    manager = JobManager(settings)
    exit_code = 1
    stop_monitor = threading.Event()

    def monitor() -> None:
        while not stop_monitor.wait(PROGRESS_INTERVAL):
            reporter()

    monitor_thread = threading.Thread(
        target=monitor, name=f"progress-{job.id}", daemon=True
    )

    try:
        if job.source_kind in {"telegram_file", "torrent_file"}:
            _download_telegram_source(api, job, settings, reporter)

        monitor_thread.start()
        manager._pipeline(job)
        job.update(status="done", stage="done", percent=100.0, detail="")
        exit_code = 0
        logger.info(
            "Job %s complete: %s",
            job.id,
            job.result.url if job.result else "(no result)",
        )
    except JobCancelled:
        job.update(status="cancelled", stage="cancelled", detail="Cancelled.")
        logger.info("Job %s cancelled", job.id)
    except JobTimeout:
        job.update(
            status="failed",
            stage="failed",
            error=f"Job timed out after {settings.job_timeout // 60} minutes.",
        )
        logger.warning("Job %s timed out", job.id)
    except MirrorError as exc:
        job.update(status="failed", stage="failed", error=str(exc))
        logger.warning("Job %s failed (%s): %s", job.id, type(exc).__name__, exc)
    except TelegramAPIError as exc:
        job.update(status="failed", stage="failed", error=str(exc))
        logger.warning("Job %s failed while talking to Telegram: %s", job.id, exc)
    except Exception:
        logger.exception("Job %s crashed", job.id)
        job.update(
            status="failed",
            stage="failed",
            error="Unexpected error. Check the GitHub Actions log for details.",
        )
    finally:
        stop_monitor.set()
        if monitor_thread.is_alive():
            monitor_thread.join(timeout=5)
        job.update(finished_at=time.time())

    _deliver_final(api, job, message_id, render_final(job))
    return exit_code


def main() -> int:
    logging.basicConfig(
        level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    payload_token = os.getenv("REMOTE_PAYLOAD", "").strip()
    expected_job_id = os.getenv("REMOTE_JOB_ID", "").strip()
    if not payload_token or not expected_job_id:
        logger.error("REMOTE_PAYLOAD and REMOTE_JOB_ID are required.")
        return 2

    try:
        settings = load_settings()
    except RuntimeError as exc:
        logger.error("%s", exc)
        return 2
    if not settings.remote_job_key:
        logger.error("REMOTE_JOB_KEY is not configured.")
        return 2

    try:
        payload = decrypt_payload(settings.remote_job_key, payload_token)
    except RemoteJobError as exc:
        logger.error("%s", exc)
        return 2
    if str(payload.get("job_id") or "") != expected_job_id:
        logger.error("The encrypted payload does not match REMOTE_JOB_ID.")
        return 2

    try:
        settings = _settings_from_payload(settings, payload.get("settings"))
        job, message_id = job_from_payload(payload, settings)
    except RemoteJobError as exc:
        logger.error("%s", exc)
        return 2

    api = TelegramAPI(settings.telegram_token)
    try:
        api.call("getMe")
    except TelegramAPIError as exc:
        logger.error("Telegram rejected the bot token: %s", exc)
        return 2

    logger.info(
        "Worker starting job %s (%s, host=%s, dump=%s)",
        job.id,
        job.source_kind,
        job.host,
        ",".join(job.dump_partitions) or "no",
    )
    try:
        return _run_job(api, job, message_id, settings)
    finally:
        shutil.rmtree(settings.download_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
