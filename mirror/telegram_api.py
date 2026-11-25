"""Small synchronous Telegram Bot API client used by the Actions worker."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

import requests

from .utils import CancelToken

logger = logging.getLogger(__name__)

API_ROOT = "https://api.telegram.org"


class TelegramAPIError(RuntimeError):
    """A Telegram Bot API call failed."""


class TelegramAPI:
    def __init__(
        self,
        token: str,
        *,
        session: Optional[requests.Session] = None,
        timeout: tuple[float, float] = (15, 60),
    ) -> None:
        if not token:
            raise TelegramAPIError("TELEGRAM_BOT_TOKEN is empty.")
        self._token = token
        self._api = f"{API_ROOT}/bot{token}"
        self._files = f"{API_ROOT}/file/bot{token}"
        self._session = session or requests.Session()
        self._timeout = timeout

    # ------------------------------------------------------------------ request
    def _post(self, method: str, params: dict[str, Any]) -> Any:
        payload = {key: value for key, value in params.items() if value is not None}
        for attempt in range(2):
            try:
                response = self._session.post(
                    f"{self._api}/{method}", data=payload, timeout=self._timeout
                )
            except requests.RequestException as exc:
                raise TelegramAPIError(
                    f"Telegram request failed ({type(exc).__name__})."
                ) from exc

            try:
                data = response.json()
            except ValueError as exc:
                raise TelegramAPIError(
                    f"Telegram returned HTTP {response.status_code} without JSON."
                ) from exc

            if response.status_code == 429 and attempt == 0:
                retry_after = 1
                try:
                    retry_after = int(
                        (data.get("parameters") or {}).get("retry_after") or 1
                    )
                except (TypeError, ValueError):
                    pass
                time.sleep(max(1, min(30, retry_after)))
                continue

            if not isinstance(data, dict) or not data.get("ok"):
                description = ""
                if isinstance(data, dict):
                    description = str(data.get("description") or "")
                raise TelegramAPIError(
                    f"Telegram: {description or f'HTTP {response.status_code}'}"
                )
            return data.get("result")
        raise TelegramAPIError("Telegram rate limit did not clear.")

    def call(self, method: str, **params: Any) -> Any:
        return self._post(method, params)

    # ----------------------------------------------------------------- messages
    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: Optional[dict[str, Any]] = None,
    ) -> int:
        result = self._post(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "reply_markup": (
                    json.dumps(reply_markup, separators=(",", ":"))
                    if reply_markup is not None
                    else None
                ),
            },
        )
        try:
            return int(result["message_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise TelegramAPIError("Telegram did not return a message id.") from exc

    def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        reply_markup: Optional[dict[str, Any]] = None,
    ) -> None:
        try:
            self._post(
                "editMessageText",
                {
                    "chat_id": chat_id,
                    "message_id": int(message_id),
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                    "reply_markup": (
                        json.dumps(reply_markup, separators=(",", ":"))
                        if reply_markup is not None
                        else None
                    ),
                },
            )
        except TelegramAPIError as exc:
            if "message is not modified" in str(exc).lower():
                return
            raise

    def edit_inline_message(
        self,
        inline_message_id: str,
        text: str,
        *,
        reply_markup: Optional[dict[str, Any]] = None,
    ) -> None:
        try:
            self._post(
                "editMessageText",
                {
                    "inline_message_id": inline_message_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                    "reply_markup": (
                        json.dumps(reply_markup, separators=(",", ":"))
                        if reply_markup is not None
                        else None
                    ),
                },
            )
        except TelegramAPIError as exc:
            if "message is not modified" in str(exc).lower():
                return
            raise

    def delete_message(self, chat_id: int, message_id: int) -> bool:
        try:
            self._post(
                "deleteMessage",
                {"chat_id": chat_id, "message_id": int(message_id)},
            )
        except TelegramAPIError as exc:
            if "message to delete not found" in str(exc).lower():
                return False
            raise
        return True

    # -------------------------------------------------------------------- files
    def download_file(
        self,
        file_id: str,
        destination: Path,
        *,
        max_size: int = 0,
        cancel: Optional[CancelToken] = None,
    ) -> Path:
        """Download one Telegram file by id, enforcing the Bot API's 20 MB limit."""
        result = self._post("getFile", {"file_id": file_id})
        if not isinstance(result, dict):
            raise TelegramAPIError("Telegram did not return file information.")
        file_path = str(result.get("file_path") or "")
        if not file_path:
            raise TelegramAPIError("Telegram did not return a file path.")
        expected = int(result.get("file_size") or 0)
        if max_size > 0 and expected > max_size:
            raise TelegramAPIError(
                f"The Telegram file is larger than the {max_size} byte limit."
            )

        destination.parent.mkdir(parents=True, exist_ok=True)
        url = f"{self._files}/{quote(file_path, safe='/')}"
        written = 0
        try:
            with self._session.get(
                url, stream=True, timeout=self._timeout
            ) as response:
                if response.status_code != 200:
                    raise TelegramAPIError(
                        f"Telegram file download failed (HTTP {response.status_code})."
                    )
                with destination.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if cancel is not None:
                            cancel.check()
                        if not chunk:
                            continue
                        written += len(chunk)
                        if max_size > 0 and written > max_size:
                            raise TelegramAPIError(
                                "The Telegram file exceeded the configured size limit."
                            )
                        handle.write(chunk)
        except requests.RequestException as exc:
            destination.unlink(missing_ok=True)
            raise TelegramAPIError(
                f"Telegram file download failed ({type(exc).__name__})."
            ) from exc
        except Exception:
            destination.unlink(missing_ok=True)
            raise

        if written == 0:
            destination.unlink(missing_ok=True)
            raise TelegramAPIError("Telegram sent an empty file.")
        return destination
