"""Dispatch mirror jobs to GitHub Actions and track the one-shot runs."""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from cryptography.fernet import Fernet, InvalidToken

from .config import Settings
from .handoff import HandoffError, cancel_run, dispatch_job, view_run
from .jobs import Job
from .state import load_state, save_state
from .utils import human_eta

logger = logging.getLogger(__name__)

DEFAULT_STATE_FILE = Path(".state/remote-jobs.json")
PAYLOAD_VERSION = 1


class RemoteJobError(RuntimeError):
    """Raised when a remote job payload or dispatch is invalid."""


def generate_key() -> str:
    """Generate a Fernet key suitable for REMOTE_JOB_KEY."""
    return Fernet.generate_key().decode("ascii")


def encrypt_payload(key: str, payload: Mapping[str, Any]) -> str:
    try:
        cipher = Fernet(key.encode("ascii"))
    except (TypeError, ValueError) as exc:
        raise RemoteJobError("REMOTE_JOB_KEY is not a valid Fernet key.") from exc
    data = json.dumps(
        payload, separators=(",", ":"), ensure_ascii=False, sort_keys=True
    ).encode("utf-8")
    return cipher.encrypt(data).decode("ascii")


def decrypt_payload(key: str, token: str) -> dict[str, Any]:
    try:
        cipher = Fernet(key.encode("ascii"))
        value = json.loads(cipher.decrypt(token.encode("ascii")))
    except (TypeError, ValueError, InvalidToken, json.JSONDecodeError) as exc:
        raise RemoteJobError("Could not decrypt the job payload.") from exc
    if not isinstance(value, dict):
        raise RemoteJobError("The job payload is not an object.")
    return value


def payload_settings(settings: Settings) -> dict[str, Any]:
    """Settings that the worker should inherit from the PC controller.

    Upload credentials and size limits travel inside the encrypted payload, so
    the PC remains the source of truth and the worker does not need duplicate
    repository secrets.
    """
    return {
        "max_file_size": settings.max_file_size,
        "job_timeout": settings.job_timeout,
        "multi_file_mode": settings.multi_file_mode,
        "ytdlp_format": settings.ytdlp_format,
        "gofile_token": settings.gofile_token,
        "pixeldrain_api_key": settings.pixeldrain_api_key,
    }


def build_payload(
    job: Job,
    message_id: int,
    *,
    settings: Optional[Settings] = None,
    file_id: Optional[str] = None,
    file_name: Optional[str] = None,
    file_size: Optional[int] = None,
) -> dict[str, Any]:
    """Build the encrypted-at-rest description of one job."""
    return {
        "version": PAYLOAD_VERSION,
        "job_id": job.id,
        "user_id": job.user_id,
        "user_name": job.user_name,
        "chat_id": job.chat_id,
        "message_id": int(message_id),
        "source": job.source,
        "source_kind": job.source_kind,
        "host": job.host,
        "dump_partitions": list(job.dump_partitions),
        "file_id": file_id if file_id is not None else job.telegram_file_id,
        "file_name": file_name if file_name is not None else job.file_name,
        "file_size": file_size if file_size is not None else job.file_size,
        "settings": payload_settings(settings) if settings is not None else {},
    }


@dataclass
class RemoteRecord:
    job: Job
    run_id: int
    run_url: str
    message_id: int
    created_at: float
    state: str = "queued"

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job.id,
            "user_id": self.job.user_id,
            "user_name": self.job.user_name,
            "chat_id": self.job.chat_id,
            "source": self.job.source,
            "source_kind": self.job.source_kind,
            "host": self.job.host,
            "dump_partitions": list(self.job.dump_partitions),
            "message_id": self.message_id,
            "run_id": self.run_id,
            "run_url": self.run_url,
            "created_at": self.created_at,
            "state": self.state,
        }


@dataclass
class RemoteEvent:
    """A tracked Actions run that has reached a terminal state."""

    record: RemoteRecord
    conclusion: str


class RemoteJobManager:
    """PC-side view of jobs that are running on GitHub Actions."""

    def __init__(
        self,
        settings: Settings,
        *,
        repo: str,
        ref: str,
        workflow: Optional[str] = None,
        state_file: Optional[Path] = None,
        dispatch: Callable[..., tuple[int, str]] = dispatch_job,
        cancel: Callable[[str, int], bool] = cancel_run,
        view: Callable[[str, int], dict[str, Any]] = view_run,
    ) -> None:
        if not settings.remote_job_key:
            raise RemoteJobError("REMOTE_JOB_KEY is not configured.")
        try:
            Fernet(settings.remote_job_key.encode("ascii"))
        except (TypeError, ValueError) as exc:
            raise RemoteJobError("REMOTE_JOB_KEY is not a valid Fernet key.") from exc
        if not repo:
            raise RemoteJobError("ACTIONS_REPO is not configured.")
        self.settings = settings
        self.repo = repo
        self.ref = ref
        self.workflow = workflow or settings.remote_workflow
        self.state_file = state_file or settings.remote_state_file or DEFAULT_STATE_FILE
        self._dispatch = dispatch
        self._cancel = cancel
        self._view = view
        self._lock = threading.RLock()
        self.records: dict[str, RemoteRecord] = {}
        self.load()

    # ------------------------------------------------------------- persistence
    def load(self) -> None:
        data = load_state(self.state_file)
        raw_jobs = data.get("jobs")
        if not isinstance(raw_jobs, list):
            return
        loaded: dict[str, RemoteRecord] = {}
        for item in raw_jobs:
            try:
                created = float(item.get("created_at") or time.time())
                job = Job(
                    id=str(item["job_id"]),
                    user_id=int(item["user_id"]),
                    chat_id=int(item["chat_id"]),
                    source=str(item.get("source") or ""),
                    host=str(item.get("host") or "gofile"),
                    user_name=str(item.get("user_name") or ""),
                    source_kind=str(item.get("source_kind") or "url"),
                    dump_partitions=tuple(item.get("dump_partitions") or ()),
                    status="running",
                    stage="remote",
                    detail="GitHub Actions",
                    created_at=created,
                    started_at=created,
                )
                record = RemoteRecord(
                    job=job,
                    run_id=int(item["run_id"]),
                    run_url=str(item.get("run_url") or ""),
                    message_id=int(item.get("message_id") or 0),
                    created_at=created,
                    state=str(item.get("state") or "running"),
                )
            except (KeyError, TypeError, ValueError):
                continue
            loaded[job.id] = record
        with self._lock:
            self.records = loaded

    def save(self) -> None:
        with self._lock:
            payload = {
                "version": 1,
                "jobs": [record.as_dict() for record in self.records.values()],
            }
        save_state(self.state_file, payload)

    # ---------------------------------------------------------------- dispatch
    def dispatch(
        self,
        job: Job,
        *,
        message_id: int,
        file_id: Optional[str] = None,
        file_name: Optional[str] = None,
        file_size: Optional[int] = None,
    ) -> RemoteRecord:
        payload = encrypt_payload(
            self.settings.remote_job_key or "",
            build_payload(
                job,
                message_id,
                settings=self.settings,
                file_id=file_id,
                file_name=file_name,
                file_size=file_size,
            ),
        )
        run_id, run_url = self._dispatch(
            self.repo,
            self.ref,
            payload,
            job.id,
            workflow=self.workflow,
        )
        now = time.time()
        job.update(
            status="running",
            stage="remote",
            started_at=now,
            detail="Waiting for a GitHub Actions runner…",
        )
        record = RemoteRecord(
            job=job,
            run_id=run_id,
            run_url=run_url,
            message_id=int(message_id),
            created_at=now,
        )
        with self._lock:
            self.records[job.id] = record
        self.save()
        return record

    # -------------------------------------------------------------------- query
    def record(self, job_id: str) -> Optional[RemoteRecord]:
        with self._lock:
            return self.records.get(job_id)

    def get(self, job_id: str) -> Optional[Job]:
        record = self.record(job_id)
        return record.job if record is not None else None

    def active_jobs(self) -> list[Job]:
        with self._lock:
            return [
                record.job
                for record in self.records.values()
                if not record.job.finished
            ]

    def active_for(self, user_id: int) -> list[Job]:
        return [job for job in self.active_jobs() if job.user_id == user_id]

    def records_for(self, user_id: int) -> list[RemoteRecord]:
        with self._lock:
            return [
                record
                for record in self.records.values()
                if record.job.user_id == user_id and not record.job.finished
            ]

    # ------------------------------------------------------------------- cancel
    def cancel_job(self, job_id: str, *, user_id: Optional[int] = None) -> bool:
        with self._lock:
            record = self.records.get(job_id)
            if record is None or record.job.finished:
                return False
            if user_id is not None and record.job.user_id != user_id:
                return False
            if record.state == "cancelling":
                return True
        if not self._cancel(self.repo, record.run_id):
            return False
        with self._lock:
            current = self.records.get(job_id)
            if current is not record:
                return False
            record.state = "cancelling"
            record.job.update(
                stage="remote", detail="Cancelling on GitHub Actions…"
            )
        self.save()
        return True

    def cancel_user(self, user_id: int) -> int:
        count = 0
        for record in self.records_for(user_id):
            if self.cancel_job(record.job.id, user_id=user_id):
                count += 1
        return count

    # ------------------------------------------------------------------ refresh
    def refresh(self) -> list[RemoteEvent]:
        """Ask GitHub for the state of each tracked run.

        Completed runs are removed from the tracking file and returned so the
        caller can deliver a cancellation card when needed. Successful and
        failed runs are normally finalized by the worker itself.
        """
        with self._lock:
            snapshots = list(self.records.items())
        events: list[RemoteEvent] = []
        changed = False
        now = time.time()

        for job_id, record in snapshots:
            try:
                info = self._view(self.repo, record.run_id)
            except HandoffError as exc:
                logger.warning("Could not refresh run %s: %s", record.run_id, exc)
                if now - record.created_at > 12 * 60 * 60:
                    with self._lock:
                        if self.records.get(job_id) is record:
                            self.records.pop(job_id, None)
                            changed = True
                continue

            status = str(info.get("status") or "")
            if status == "completed":
                conclusion = str(info.get("conclusion") or "")
                with self._lock:
                    if self.records.get(job_id) is not record:
                        continue
                    self.records.pop(job_id, None)
                events.append(RemoteEvent(record=record, conclusion=conclusion))
                changed = True
                continue

            elapsed = human_eta(max(0.0, now - record.job.created_at))
            with self._lock:
                if self.records.get(job_id) is not record:
                    continue
                if record.state != "cancelling":
                    record.state = "running" if status != "queued" else "queued"
                record.job.update(
                    status="running",
                    stage="remote",
                    detail=(
                        "Cancelling on GitHub Actions…"
                        if record.state == "cancelling"
                        else (
                            f"GitHub Actions · {elapsed}"
                            if elapsed
                            else "GitHub Actions"
                        )
                    ),
                )
            changed = True

        if changed:
            self.save()
        return events
