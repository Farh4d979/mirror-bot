"""Pixeldrain uploader (raw PUT, free account API key required)."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import requests

from ..errors import JobCancelled, UploadError
from ..utils import CancelToken, human_size
from .base import ProgressCB, Uploader, UploadResult

logger = logging.getLogger(__name__)

UPLOAD_URL = "https://pixeldrain.com/api/file"
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class _Retryable(Exception):
    pass


class _ProgressReader:
    """File wrapper that reports upload progress to requests/urllib3."""

    def __init__(
        self,
        handle,
        total: int,
        progress: ProgressCB,
        cancel: CancelToken,
    ) -> None:
        self.handle = handle
        self.total = total
        self.progress = progress
        self.cancel = cancel
        self.done = 0

    def read(self, size: int = -1) -> bytes:
        self.cancel.check()
        chunk = self.handle.read(size)
        if chunk:
            self.done += len(chunk)
            percent = self.done / self.total * 100 if self.total else None
            self.progress(
                percent,
                f"{human_size(self.done)} / {human_size(self.total)}",
            )
        return chunk

    def __len__(self) -> int:
        return self.total


class PixeldrainUploader(Uploader):
    name = "pixeldrain"

    def __init__(
        self,
        api_key: Optional[str] = None,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.api_key = (api_key or "").strip() or None
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "mirror-bot/1.0"})

    def available(self) -> tuple[bool, str]:
        if not self.api_key:
            return (
                False,
                "PIXELDRAIN_API_KEY is not set. Pixeldrain requires an account API "
                "key for uploads, but the account is free and allows 10 GB per file. "
                "Create a key at https://pixeldrain.com/user/api_keys and add it as "
                "a repository secret to use /px.",
            )
        return True, ""

    def upload(self, path: Path, progress: ProgressCB, cancel: CancelToken) -> UploadResult:
        if not self.api_key:
            raise UploadError("PIXELDRAIN_API_KEY is not set.")
        if not path.is_file():
            raise UploadError("The downloaded file disappeared before upload.")

        last_error: Optional[Exception] = None
        for attempt in range(4):
            try:
                return self._upload_once(path, progress, cancel)
            except (JobCancelled, UploadError):
                raise
            except _Retryable as exc:
                last_error = exc
                if attempt == 3:
                    break
                _sleep(min(60, 5 * (2**attempt)), cancel)
            except requests.RequestException as exc:
                last_error = exc
                if attempt == 3:
                    break
                _sleep(min(60, 5 * (2**attempt)), cancel)
        raise UploadError(f"Pixeldrain upload failed: {last_error}")

    def _upload_once(
        self,
        path: Path,
        progress: ProgressCB,
        cancel: CancelToken,
    ) -> UploadResult:
        progress(0, "Contacting Pixeldrain…")
        total = path.stat().st_size
        url = f"{UPLOAD_URL}/{quote(path.name, safe='')}"

        with path.open("rb") as handle:
            reader = _ProgressReader(handle, total, progress, cancel)
            response = self.session.put(
                url,
                data=reader,
                headers={
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(total),
                },
                auth=("", self.api_key),
                timeout=(30, None),
            )

        if response.status_code in RETRYABLE_STATUS:
            raise _Retryable(
                f"Pixeldrain returned HTTP {response.status_code}: "
                f"{_response_snippet(response)}"
            )
        if response.status_code in {401, 403}:
            detail = _response_snippet(response)
            logger.warning(
                "Pixeldrain rejected the upload (HTTP %s): %s",
                response.status_code,
                detail,
            )
            raise UploadError(
                f"Pixeldrain rejected the request (HTTP {response.status_code}): "
                f"{detail}. Check PIXELDRAIN_API_KEY and its permissions."
            )
        if response.status_code == 413:
            raise UploadError(
                "Pixeldrain rejected the file as too large for this plan "
                "(free: 10 GB/file, Pro: 100 GB/file)."
            )
        if response.status_code >= 400:
            raise UploadError(
                f"Pixeldrain returned HTTP {response.status_code}: "
                f"{_response_snippet(response)}"
            )

        body = _response_snippet(response)
        try:
            payload = response.json()
        except ValueError as exc:
            logger.warning(
                "Pixeldrain returned non-JSON (HTTP %s): %s",
                response.status_code,
                body,
            )
            raise UploadError(
                f"Pixeldrain returned an unexpected response "
                f"(HTTP {response.status_code}): {body}"
            ) from exc

        if not isinstance(payload, dict):
            logger.warning(
                "Pixeldrain returned an unexpected JSON shape (HTTP %s): %s",
                response.status_code,
                body,
            )
            raise UploadError(
                f"Pixeldrain returned an unexpected response "
                f"(HTTP {response.status_code}): {body}"
            )

        if not payload.get("success"):
            message = _payload_error(payload) or body
            logger.warning(
                "Pixeldrain upload failed (HTTP %s): %s",
                response.status_code,
                body,
            )
            raise UploadError(
                f"Pixeldrain upload failed (HTTP {response.status_code}): {message}"
            )

        file_id = payload.get("id")
        if not file_id:
            raise UploadError("Pixeldrain did not return a file ID.")

        link = f"https://pixeldrain.com/u/{file_id}"
        logger.info("Pixeldrain upload complete: %s", link)
        return UploadResult(url=link, host="Pixeldrain")


def _response_snippet(response, limit: int = 300) -> str:
    """Return a compact, single-line snippet of a response body."""
    text = (getattr(response, "text", "") or "").strip()
    if not text:
        return "empty body"
    return " ".join(text.split())[:limit]


def _payload_error(payload: dict) -> str:
    """Extract the most useful error string from a Pixeldrain JSON payload."""
    for key in ("message", "value", "error", "detail"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, dict):
            nested = value.get("message") or value.get("value")
            if isinstance(nested, str) and nested:
                return nested
    errors = payload.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        first = errors[0]
        nested = first.get("message") or first.get("value")
        if isinstance(nested, str) and nested:
            return nested
    return ""


def _sleep(seconds: float, cancel: CancelToken) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        cancel.check()
        time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
