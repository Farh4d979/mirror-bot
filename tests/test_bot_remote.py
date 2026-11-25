from __future__ import annotations

import asyncio
from types import SimpleNamespace

from telegram.error import BadRequest, TelegramError

import bot
from mirror.config import Settings
from mirror.jobs import new_job
from mirror.remote import RemoteRecord


class FakeMessage:
    def __init__(self, message_id: int) -> None:
        self.message_id = message_id
        self.edits = []

    async def edit_text(self, text, **kwargs):
        self.edits.append((text, kwargs))


class FakeBot:
    def __init__(self) -> None:
        self.sent = []
        self.edits = []
        self.inline_edits = []
        self.deleted = []
        self.delete_result = True
        self.delete_error = None

    async def send_message(self, chat_id, text, **kwargs):
        message = FakeMessage(500 + len(self.sent))
        self.sent.append((chat_id, text, kwargs, message))
        return message

    async def edit_message_text(
        self,
        chat_id=None,
        message_id=None,
        text=None,
        *,
        inline_message_id=None,
        **kwargs,
    ):
        if inline_message_id:
            self.inline_edits.append((inline_message_id, text, kwargs))
        else:
            self.edits.append((chat_id, message_id, text, kwargs))

    async def delete_message(self, chat_id, message_id):
        if self.delete_error:
            raise self.delete_error
        self.deleted.append((chat_id, message_id))
        return self.delete_result


class FakeRemote:
    def __init__(self, error=None) -> None:
        self.dispatched = []
        self.inline_ids = []
        self.error = error

    def dispatch(self, job, *, message_id, inline_message_id=None):
        if self.error is not None:
            raise self.error
        self.dispatched.append((job, message_id))
        self.inline_ids.append(inline_message_id)
        job.update(status="running", stage="remote", detail="GitHub Actions")
        return SimpleNamespace(run_id=7)


def make_context(remote):
    settings = Settings(
        telegram_token="token",
        allowed_user_ids=frozenset({1}),
        remote_jobs=True,
        remote_job_key="unused",
    )
    return SimpleNamespace(
        application=SimpleNamespace(
            bot_data={
                "settings": settings,
                "remote": remote,
                "accepting_jobs": True,
            }
        ),
        bot=FakeBot(),
    )


def make_job(inline_message_id=None):
    return new_job(
        user_id=1,
        chat_id=2,
        source="https://example.com/file.zip",
        host="gofile",
        user_name="Launcher",
        inline_message_id=inline_message_id,
    )


def test_remote_start_creates_card_then_dispatches():
    remote = FakeRemote()
    context = make_context(remote)
    job = make_job()

    asyncio.run(bot._start_job(context, job, None))

    assert len(context.bot.sent) == 1
    chat_id, text, kwargs, message = context.bot.sent[0]
    assert chat_id == 2
    assert "GitHub Actions" in text
    assert kwargs["reply_markup"] is not None
    assert remote.dispatched == [(job, message.message_id)]
    assert context.bot.edits
    assert "GitHub Actions" in context.bot.edits[-1][2]
    assert job.status == "running"


def test_remote_start_reports_a_dispatch_failure():
    remote = FakeRemote(error=RuntimeError("gh is offline"))
    context = make_context(remote)
    job = make_job()

    asyncio.run(bot._start_job(context, job, None))

    assert job.status == "failed"
    assert "Could not start GitHub Actions" in (job.error or "")
    assert len(context.bot.sent) == 1
    assert context.bot.edits
    assert "Could not start GitHub Actions" in context.bot.edits[-1][2]
    assert context.bot.edits[-1][3]["reply_markup"] is None


def test_remote_start_reuses_an_existing_status_message():
    remote = FakeRemote()
    context = make_context(remote)
    job = make_job()
    message = FakeMessage(88)

    asyncio.run(bot._start_job(context, job, message))

    assert message.edits
    assert remote.dispatched[0][1] == 88
    assert context.bot.sent == []


def make_record(inline_message_id=""):
    job = make_job(inline_message_id=inline_message_id or None)
    job.update(status="running", stage="remote", detail="GitHub Actions")
    return RemoteRecord(
        job=job,
        run_id=7,
        run_url="https://github.com/owner/repo/actions/runs/7",
        message_id=55 if not inline_message_id else 0,
        inline_message_id=inline_message_id,
        created_at=0.0,
    )


def test_remote_failure_replaces_a_stuck_progress_card():
    bot_instance = FakeBot()
    record = make_record()

    asyncio.run(bot._deliver_remote_failure(bot_instance, record))

    assert bot_instance.deleted == [(2, 55)]
    assert record.job.status == "failed"
    assert len(bot_instance.sent) == 1
    text = bot_instance.sent[0][1]
    assert "Job failed" in text
    assert "before the worker could report details" in text
    assert "https://github.com/owner/repo/actions/runs/7" in text


def test_remote_failure_leaves_a_worker_final_card_alone():
    bot_instance = FakeBot()
    bot_instance.delete_error = BadRequest("Message to delete not found")
    record = make_record()

    asyncio.run(bot._deliver_remote_failure(bot_instance, record))

    assert bot_instance.deleted == []
    assert bot_instance.sent == []
    assert record.job.status == "running"


def test_remote_failure_does_nothing_when_delete_is_refused():
    bot_instance = FakeBot()
    bot_instance.delete_error = TelegramError("not enough rights")
    record = make_record()

    asyncio.run(bot._deliver_remote_failure(bot_instance, record))

    assert bot_instance.sent == []
    assert record.job.status == "running"


def test_remote_failure_does_nothing_when_delete_returns_false():
    bot_instance = FakeBot()
    bot_instance.delete_result = False
    record = make_record()

    asyncio.run(bot._deliver_remote_failure(bot_instance, record))

    assert bot_instance.sent == []
    assert record.job.status == "running"


def test_remote_start_edits_an_inline_card_and_passes_the_inline_id():
    remote = FakeRemote()
    context = make_context(remote)
    job = make_job(inline_message_id="inline-1")

    asyncio.run(bot._start_job(context, job, None))

    assert context.bot.sent == []
    assert remote.dispatched == [(job, 0)]
    assert remote.inline_ids == ["inline-1"]
    assert context.bot.inline_edits
    inline_message_id, text, kwargs = context.bot.inline_edits[-1]
    assert inline_message_id == "inline-1"
    assert "GitHub Actions" in text
    assert kwargs["reply_markup"] is not None


def test_remote_dispatch_failure_edits_the_inline_card():
    remote = FakeRemote(error=RuntimeError("gh is offline"))
    context = make_context(remote)
    job = make_job(inline_message_id="inline-1")

    asyncio.run(bot._start_job(context, job, None))

    assert job.status == "failed"
    assert context.bot.sent == []
    assert context.bot.inline_edits
    inline_message_id, text, kwargs = context.bot.inline_edits[-1]
    assert inline_message_id == "inline-1"
    assert "Could not start GitHub Actions" in text
    assert kwargs["reply_markup"].inline_keyboard == ()


def test_remote_failure_edits_an_inline_card():
    bot_instance = FakeBot()
    record = make_record(inline_message_id="inline-1")

    asyncio.run(bot._deliver_remote_failure(bot_instance, record))

    assert bot_instance.deleted == []
    assert bot_instance.sent == []
    assert bot_instance.inline_edits
    inline_message_id, text, kwargs = bot_instance.inline_edits[-1]
    assert inline_message_id == "inline-1"
    assert "Job failed" in text
    assert "https://github.com/owner/repo/actions/runs/7" in text
    assert kwargs["reply_markup"].inline_keyboard == ()


def test_local_final_edits_an_inline_card_and_clears_the_keyboard():
    bot_instance = FakeBot()
    context = SimpleNamespace(bot=bot_instance)
    job = make_job(inline_message_id="inline-1")

    asyncio.run(bot._deliver_final(context, job, None, "done"))

    assert len(bot_instance.inline_edits) == 1
    inline_message_id, text, kwargs = bot_instance.inline_edits[0]
    assert inline_message_id == "inline-1"
    assert text == "done"
    assert kwargs["reply_markup"].inline_keyboard == ()
