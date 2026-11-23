"""Gofile uploader (new upload fleet API, guest uploads supported)."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

import requests
from requests_toolbelt.multipart.encoder import MultipartEncoder, MultipartEncoderMonitor

from ..errors import JobCancelled, UploadError
from ..utils import CancelToken, format_progress, SpeedMeter
from .base import ProgressCB, Uploader, UploadResult

logger = logging.getLogger(__name__)

UPLOAD_URL = "https://upload.gofile.io/uploadfile"


class _Retryable(Exception):
    pass


class GofileUploader(Uploader):
    name = "gofile"

    def __init__(self, token: Optional[str] = None, session: Optional[requests.Session] = None) -> None:
        self.token = token
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "mirror-bot/1.0"})

    def available(self) -> tuple[bool, str]:
        return True, ""

    def upload(self, path: Path, progress: ProgressCB, cancel: CancelToken) -> UploadResult:
        if not path.is_file():
            raise UploadError("The downloaded file disappeared before upload.")
        last_error: Optional[Exception] = None
        for attempt in range(3):
            try:
                return self._upload_once(path, progress, cancel)
            except (JobCancelled, UploadError):
                raise
            except _Retryable as exc:
                last_error = exc
                if attempt == 2:
                    break
                _sleep(2 ** attempt, cancel)
            except requests.RequestException as exc:
                last_error = exc
                if attempt == 2:
                    break
                _sleep(2 ** attempt, cancel)
        raise UploadError(f"Gofile upload failed: {last_error}")

    def _upload_once(self, path: Path, progress: ProgressCB, cancel: CancelToken) -> UploadResult:
        progress(0, "Contacting Gofile…")
        file_size = path.stat().st_size
        with path.open("rb") as handle:
            # The upload endpoint takes the account token as a form field (the
            # same way gofile.io's own web client sends it), not as a header.
            fields: dict = {}
            if self.token:
                fields["token"] = (None, self.token)
            fields["file"] = (path.name, handle, "application/octet-stream")
            encoder = MultipartEncoder(fields=fields)
            total = encoder.len
            meter = SpeedMeter()
            state = {"last": 0.0}

            def on_progress(monitor: MultipartEncoderMonitor) -> None:
                cancel.check()
                now = time.monotonic()
                complete = bool(total) and monitor.bytes_read >= total
                if not complete and now - state["last"] < 0.25:
                    return
                state["last"] = now
                # percent includes multipart overhead; show the real file size.
                if total:
                    done = min(file_size, int(monitor.bytes_read * file_size / total))
                else:
                    done = monitor.bytes_read
                speed, eta = meter.update(done, file_size)
                percent = monitor.bytes_read / total * 100 if total else None
                progress(percent, format_progress(done, file_size, speed, eta))

            monitor = MultipartEncoderMonitor(encoder, on_progress)
            response = self.session.post(
                UPLOAD_URL,
                data=monitor,
                headers={"Content-Type": monitor.content_type},
                timeout=(30, None),
            )

        if response.status_code in {429, 500, 502, 503, 504}:
            raise _Retryable(f"Gofile returned HTTP {response.status_code}")
        if response.status_code in {401, 403}:
            detail = _error_detail(response)
            if _payload_status(response) == "error-limits":
                raise UploadError(_limits_message(detail))
            if self.token:
                raise UploadError(
                    "Gofile rejected the account token. Check GOFILE_TOKEN or remove it "
                    f"to use guest uploads. ({detail})"
                )
            raise UploadError(
                f"Gofile refused the guest upload (HTTP {response.status_code}). {detail}"
            )
        if response.status_code >= 400:
            raise UploadError(
                f"Gofile returned HTTP {response.status_code}: {response.text[:200]}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise UploadError("Gofile returned an unexpected response.") from exc

        status = str(payload.get("status", "")).lower()
        if status != "ok":
            message = _payload_message(payload)
            if status == "error-limits":
                raise UploadError(_limits_message(message))
            if status in {"error-token", "error-account"}:
                if self.token:
                    raise UploadError(f"Gofile rejected the account token: {message}")
                raise UploadError(f"Gofile refused the guest upload: {message}")
            raise UploadError(f"Gofile upload failed: {message}")

        data = payload.get("data") or {}
        url = data.get("downloadPage") or data.get("link")
        if not url:
            code = data.get("code") or data.get("parentFolderCode")
            if code:
                url = f"https://gofile.io/d/{code}"
        if not url:
            raise UploadError("Gofile did not return a download link.")

        progress(100.0, format_progress(file_size, file_size))
        logger.info("Gofile upload complete: %s", url)
        return UploadResult(url=str(url), host="Gofile")


def _error_detail(response) -> str:
    """Extract Gofile's human-readable error from a response or JSON payload."""
    if hasattr(response, "json"):
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            return _payload_message(payload)
        if isinstance(payload, str) and payload:
            return payload
    text = getattr(response, "text", "") or ""
    return " ".join(text.split())[:200] or "no details"


def _payload_status(response) -> str:
    """Return Gofile's status string from a JSON response, if one is present."""
    if hasattr(response, "json"):
        try:
            payload = response.json()
        except ValueError:
            return ""
        if isinstance(payload, dict):
            return str(payload.get("status", "")).lower()
    return ""


def _limits_message(detail: str) -> str:
    return (
        f"Gofile refused the upload: {detail}. Gofile enforces these limits on "
        "its servers: guest 1 GB/file, verified free 5 GB/file, Pro 10 GB/file. "
        "Add GOFILE_TOKEN for your plan's limit, upgrade, or use /px "
        "(Pixeldrain free: 10 GB/file)."
    )


def _payload_message(payload: dict) -> str:
    data = payload.get("data")
    if isinstance(data, str) and data:
        return data
    for key in ("message", "error"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return str(payload.get("status") or "unknown error")


def _sleep(seconds: float, cancel: CancelToken) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        cancel.check()
        time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
