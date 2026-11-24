from __future__ import annotations

import asyncio
from types import SimpleNamespace

from telegram.error import TelegramError

from bot import _can_cancel, _deliver_final, render_final, render_job
from mirror.config import Settings
from mirror.jobs import new_job
from mirror.uploaders.base import UploadResult


def test_render_running_job():
    job = new_job(
        user_id=1,
        chat_id=2,
        source="https://example.com/a.zip",
        host="gofile",
        user_name="Farhad",
    )
    job.update(status="running", stage="downloading", percent=42.0, detail="1.2 GB / 2.8 GB · 24 MB/s")
    text = render_job(job)
    assert "⬇️" in text
    assert "Downloading" in text
    assert "42%" in text
    assert "Gofile" in text
    assert "1.2 GB" in text
    assert "24 MB/s" in text
    assert "Launched by" in text
    assert 'href="tg://user?id=1"' in text
    assert "Farhad" in text


def test_render_upload_stage_with_speed():
    job = new_job(user_id=1, chat_id=2, source="https://example.com/a.zip", host="pixeldrain")
    job.update(
        status="running",
        stage="uploading",
        percent=63.0,
        detail="3.7 GB / 5.9 GB · 24.6 MB/s · ETA 1m 30s",
    )
    text = render_job(job)
    assert "⬆️" in text
    assert "Uploading" in text
    assert "63%" in text
    assert "24.6 MB/s" in text
    assert "Pixeldrain" in text


def test_render_done_job():
    job = new_job(
        user_id=1,
        chat_id=2,
        source="https://example.com/a.zip",
        host="gofile",
        user_name="Farhad",
    )
    job.update(
        status="done",
        stage="done",
        percent=100.0,
        file_name="a.zip",
        file_size=1234,
        started_at=100.0,
        finished_at=130.0,
        result=UploadResult(url="https://gofile.io/d/abc", host="Gofile"),
    )
    text = render_final(job)
    assert "Job complete" in text
    assert "https://gofile.io/d/abc" in text
    assert "1.2 KB" in text
    assert "Took 30s" in text
    assert "Launched by" in text
    assert 'href="tg://user?id=1"' in text
    assert "Farhad" in text


def test_render_dump_job_lists_each_partition_and_missing():
    job = new_job(
        user_id=1,
        chat_id=2,
        source="https://example.com/ota.zip",
        host="gofile",
        user_name="Farhad",
        dump_partitions=("boot", "vendor"),
    )
    results = (
        UploadResult(
            url="https://gofile.io/d/boot",
            host="Gofile",
            file_name="boot.img",
            size=1024,
        ),
        UploadResult(
            url="https://gofile.io/d/vendor",
            host="Gofile",
            file_name="vendor_a.img",
            size=2048,
        ),
    )
    job.update(
        status="done",
        stage="done",
        percent=100.0,
        results=results,
        result=results[0],
        missing_partitions=("system",),
        file_name="boot.img, vendor_a.img",
        file_size=3072,
        started_at=100.0,
        finished_at=130.0,
    )
    text = render_final(job)
    assert "Partition dump complete" in text
    assert "https://gofile.io/d/boot" in text
    assert "https://gofile.io/d/vendor" in text
    assert "boot.img" in text
    assert "vendor_a.img" in text
    assert "Not found" in text
    assert "system" in text
    assert "Took 30s" in text


def test_render_extracting_stage():
    job = new_job(
        user_id=1,
        chat_id=2,
        source="https://example.com/ota.zip",
        host="gofile",
        dump_partitions=("boot", "vendor"),
    )
    job.update(
        status="running",
        stage="extracting",
        percent=50.0,
        detail="Extracting vendor.img · 1.0 GB / 2.0 GB",
    )
    text = render_job(job)
    assert "🧩" in text
    assert "Extracting partitions" in text
    assert "vendor.img" in text


def test_cancel_permissions():
    settings = Settings(telegram_token="x", allowed_user_ids=frozenset({99}))
    job = new_job(
        user_id=1,
        chat_id=2,
        source="https://example.com/a.zip",
        host="gofile",
        user_name="Launcher",
    )
    assert _can_cancel(job, 1, settings)  # launcher
    assert _can_cancel(job, 99, settings)  # admin
    assert not _can_cancel(job, 5, settings)  # unrelated user


def test_launcher_name_is_escaped():
    job = new_job(
        user_id=1,
        chat_id=2,
        source="https://example.com/a.zip",
        host="gofile",
        user_name="<b>bad</b>",
    )
    text = render_job(job)
    assert "&lt;b&gt;bad&lt;/b&gt;" in text
    assert "<b>bad</b>" not in text


class FakeStatusMessage:
    def __init__(self) -> None:
        self.deleted = False
        self.edited: str | None = None
        self.delete_error: TelegramError | None = None
        self.edit_error: TelegramError | None = None

    async def delete(self) -> None:
        if self.delete_error:
            raise self.delete_error
        self.deleted = True

    async def edit_text(self, text: str, **kwargs) -> None:
        if self.edit_error:
            raise self.edit_error
        self.edited = text


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs) -> None:
        self.sent.append((chat_id, text))


def _final_job():
    return new_job(user_id=1, chat_id=2, source="https://example.com/a.zip", host="gofile")


def test_deliver_final_deletes_progress_and_sends_new_message():
    message = FakeStatusMessage()
    bot_instance = FakeBot()

    asyncio.run(_deliver_final(SimpleNamespace(bot=bot_instance), _final_job(), message, "done"))

    assert message.deleted
    assert message.edited is None
    assert bot_instance.sent == [(2, "done")]


def test_deliver_final_edits_when_delete_is_not_allowed():
    message = FakeStatusMessage()
    message.delete_error = TelegramError("delete not allowed")
    bot_instance = FakeBot()

    asyncio.run(_deliver_final(SimpleNamespace(bot=bot_instance), _final_job(), message, "done"))

    assert message.edited == "done"
    assert bot_instance.sent == []


def test_deliver_final_sends_new_message_when_edit_also_fails():
    message = FakeStatusMessage()
    message.delete_error = TelegramError("delete not allowed")
    message.edit_error = TelegramError("edit not allowed")
    bot_instance = FakeBot()

    asyncio.run(_deliver_final(SimpleNamespace(bot=bot_instance), _final_job(), message, "done"))

    assert bot_instance.sent == [(2, "done")]


def test_render_failed_job_escapes_html():
    job = new_job(user_id=1, chat_id=2, source="https://example.com/a.zip", host="gofile")
    job.update(status="failed", stage="failed", error="bad <script> & stuff")
    text = render_final(job)
    assert "&lt;script&gt;" in text
    assert "&amp;" in text


def test_settings_defaults():
    settings = Settings(telegram_token="x")
    assert settings.max_concurrent_jobs == 2
    assert settings.gofile_token is None
