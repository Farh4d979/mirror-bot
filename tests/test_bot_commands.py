from __future__ import annotations

import asyncio
import datetime
import time
from types import SimpleNamespace

import pytest
from telegram import Chat, Document, Message, MessageEntity, Update, User
from telegram.constants import ChatType
from telegram.ext import Application, ApplicationHandlerStop

import bot
from mirror.config import Settings


class FakeUser:
    id = 1


class FakeChat:
    def __init__(self, chat_type: str = ChatType.PRIVATE) -> None:
        self.id = 2
        self.type = chat_type


class FakeMessage:
    def __init__(self) -> None:
        self.replies: list[str] = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


class FakeUpdate:
    def __init__(self, user_id: int = 1, chat_type: str = ChatType.PRIVATE) -> None:
        self.effective_user = SimpleNamespace(id=user_id)
        self.effective_chat = FakeChat(chat_type)
        self.effective_message = FakeMessage()
        self.callback_query = None
        self.inline_query = None


class FakeMember:
    def __init__(self, status: str) -> None:
        self.status = status


class FakeBot:
    def __init__(self, statuses: dict[int, str] | None = None) -> None:
        self.statuses = statuses or {}
        self.edits: list[dict] = []

    async def get_chat_member(self, chat_id: int, user_id: int) -> FakeMember:
        return FakeMember(self.statuses.get(user_id, "left"))

    async def edit_message_text(self, **kwargs):
        self.edits.append(kwargs)

    async def send_message(self, chat_id, text, **kwargs):
        return SimpleNamespace(message_id=1, chat_id=chat_id, text=text)


class FakeInlineQuery:
    def __init__(self, query: str) -> None:
        self.id = "inline-query-1"
        self.query = query
        self.answers: list[tuple[list, dict]] = []

    async def answer(self, results, **kwargs):
        self.answers.append((results, kwargs))


class FakeCallbackQuery:
    def __init__(
        self,
        data: str,
        *,
        user_id: int = 1,
        inline_message_id: str | None = "inline-message-1",
    ) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.inline_message_id = inline_message_id
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


class FakeContext:
    def __init__(
        self,
        settings: Settings,
        args: list[str],
        bot_instance: FakeBot | None = None,
    ) -> None:
        self.application = SimpleNamespace(bot_data={"settings": settings})
        self.args = args
        self.bot = bot_instance or FakeBot()


def run_command(
    monkeypatch,
    command: str,
    args: list[str],
    *,
    user_id: int = 1,
    chat_type: str = ChatType.PRIVATE,
    **settings_kwargs,
):
    settings = Settings(
        telegram_token="token",
        allowed_user_ids=frozenset({1}),
        **settings_kwargs,
    )
    update = FakeUpdate(user_id=user_id, chat_type=chat_type)
    context = FakeContext(settings, args)
    started = []

    async def fake_start(context, job, message):
        started.append(job)

    monkeypatch.setattr(bot, "_start_job", fake_start)
    if command == "px":
        asyncio.run(bot.cmd_px(update, context))
    else:
        asyncio.run(bot._cmd_link(update, context, command=command))
    return update, started


def test_gf_starts_gofile_job(monkeypatch):
    update, started = run_command(monkeypatch, "gf", ["https://example.com/file.zip"])
    assert len(started) == 1
    assert started[0].host == "gofile"
    assert started[0].source == "https://example.com/file.zip"
    assert not update.effective_message.replies


def test_mirror_starts_gofile_job(monkeypatch):
    update, started = run_command(
        monkeypatch, "mirror", ["https://example.com/file.zip"]
    )
    assert len(started) == 1
    assert started[0].host == "gofile"
    assert not update.effective_message.replies


def test_gf_dump_parses_comma_separated_partitions(monkeypatch):
    update, started = run_command(
        monkeypatch,
        "gf",
        ["https://example.com/ota.zip", "-dump", "boot,vendor"],
    )
    assert len(started) == 1
    assert started[0].dump_partitions == ("boot", "vendor")
    assert not update.effective_message.replies


def test_gf_dump_parses_space_separated_partitions(monkeypatch):
    update, started = run_command(
        monkeypatch,
        "gf",
        ["-dump", "boot", "vendor", "https://example.com/ota.zip"],
    )
    assert len(started) == 1
    assert started[0].dump_partitions == ("boot", "vendor")


def test_gf_dump_without_names_shows_usage(monkeypatch):
    update, started = run_command(
        monkeypatch, "gf", ["https://example.com/ota.zip", "-dump"]
    )
    assert not started
    assert "-dump" in update.effective_message.replies[0]


def test_gf_dump_rejects_unsafe_partition_name(monkeypatch):
    update, started = run_command(
        monkeypatch,
        "gf",
        ["https://example.com/ota.zip", "-dump", "../../boot"],
    )
    assert not started
    assert "valid partition name" in update.effective_message.replies[0]


def test_command_without_link_shows_usage(monkeypatch):
    update, started = run_command(monkeypatch, "gf", [])
    assert not started
    assert update.effective_message.replies
    assert "/gf" in update.effective_message.replies[0]


def test_px_starts_pixeldrain_job(monkeypatch):
    update, started = run_command(
        monkeypatch,
        "px",
        ["https://example.com/file.zip"],
        pixeldrain_api_key="secret",
    )
    assert len(started) == 1
    assert started[0].host == "pixeldrain"
    assert started[0].source == "https://example.com/file.zip"
    assert not update.effective_message.replies


def test_px_requires_api_key(monkeypatch):
    update, started = run_command(monkeypatch, "px", ["https://example.com/file.zip"])
    assert not started
    assert update.effective_message.replies
    assert "PIXELDRAIN_API_KEY" in update.effective_message.replies[0]


def test_owner_can_use_private_chat(monkeypatch):
    update, started = run_command(monkeypatch, "gf", ["https://example.com/file.zip"])
    assert len(started) == 1
    assert not update.effective_message.replies


def test_stranger_dm_is_denied(monkeypatch):
    update, started = run_command(
        monkeypatch,
        "gf",
        ["https://example.com/file.zip"],
        user_id=99,
    )
    assert not started
    assert update.effective_message.replies
    assert "private" in update.effective_message.replies[0].lower()


def test_group_members_can_use_the_bot(monkeypatch):
    update, started = run_command(
        monkeypatch,
        "gf",
        ["https://example.com/file.zip"],
        user_id=99,
        chat_type=ChatType.GROUP,
    )
    assert len(started) == 1
    assert not update.effective_message.replies


def test_supergroup_members_can_use_the_bot(monkeypatch):
    update, started = run_command(
        monkeypatch,
        "mirror",
        ["https://example.com/file.zip"],
        user_id=99,
        chat_type=ChatType.SUPERGROUP,
    )
    assert len(started) == 1
    assert not update.effective_message.replies


def test_channels_are_denied(monkeypatch):
    update, started = run_command(
        monkeypatch,
        "gf",
        ["https://example.com/file.zip"],
        user_id=99,
        chat_type=ChatType.CHANNEL,
    )
    assert not started
    assert update.effective_message.replies


def test_id_is_denied_for_strangers():
    settings = Settings(telegram_token="token", allowed_user_ids=frozenset({1}))
    update = FakeUpdate(user_id=99)
    context = FakeContext(settings, [])
    asyncio.run(bot.cmd_id(update, context))
    assert update.effective_message.replies
    assert "private" in update.effective_message.replies[0].lower()


def test_group_gate_allows_group_with_owner():
    settings = Settings(telegram_token="token", allowed_user_ids=frozenset({1}))
    update = FakeUpdate(user_id=99, chat_type=ChatType.GROUP)
    context = FakeContext(settings, [], FakeBot({1: "member"}))
    asyncio.run(bot.gate_group_access(update, context))


def test_group_gate_blocks_group_without_owner():
    settings = Settings(telegram_token="token", allowed_user_ids=frozenset({1}))
    update = FakeUpdate(user_id=99, chat_type=ChatType.SUPERGROUP)
    context = FakeContext(settings, [], FakeBot({1: "left"}))
    with pytest.raises(ApplicationHandlerStop):
        asyncio.run(bot.gate_group_access(update, context))


def test_group_gate_ignores_private_chats():
    settings = Settings(telegram_token="token", allowed_user_ids=frozenset({1}))
    update = FakeUpdate(user_id=99)
    context = FakeContext(settings, [], FakeBot({1: "left"}))
    asyncio.run(bot.gate_group_access(update, context))


def _build_update(
    chat_type: str,
    *,
    user_id: int = 1,
    text: str = "/start",
    update_id: int = 1,
) -> Update:
    command = text.split()[0]
    message = Message(
        message_id=update_id,
        date=datetime.datetime.now(datetime.timezone.utc),
        chat=Chat(id=user_id, type=chat_type),
        from_user=User(id=user_id, first_name="User", is_bot=False),
        text=text,
        entities=[
            MessageEntity(
                type=MessageEntity.BOT_COMMAND,
                offset=0,
                length=len(command),
            )
        ],
    )
    return Update(update_id=update_id, message=message)


def _build_dispatcher(monkeypatch, tmp_path, started):
    app = Application.builder().token("123:abc").build()
    app._initialized = True
    app.bot._bot_user = User(
        id=999, first_name="Bot", is_bot=True, username="testbot"
    )
    settings = Settings(
        telegram_token="token",
        allowed_user_ids=frozenset({1}),
        state_file=tmp_path / "telegram.json",
    )

    async def fake_start(update, context):
        started.append(update)

    monkeypatch.setattr(bot, "cmd_start", fake_start)
    bot._register(app, settings, object())
    return app


def _dispatch(app: Application, update: Update) -> None:
    update.message.set_bot(app.bot)
    asyncio.run(app.process_update(update))


def _build_attachment_update(update_id: int = 1) -> Update:
    document = Document(
        file_id="file-1",
        file_unique_id="unique-1",
        file_name="hello.txt",
    )
    message = Message(
        message_id=update_id,
        date=datetime.datetime.now(datetime.timezone.utc),
        chat=Chat(id=1, type=ChatType.PRIVATE),
        from_user=User(id=1, first_name="User", is_bot=False),
        document=document,
    )
    return Update(update_id=update_id, message=message)


def test_unsolicited_attachment_is_ignored(monkeypatch, tmp_path):
    started = []
    app = _build_dispatcher(monkeypatch, tmp_path, started)
    submitted = []

    async def fake_submit(*args, **kwargs):
        submitted.append((args, kwargs))

    monkeypatch.setattr(bot, "_submit_job", fake_submit)
    _dispatch(app, _build_attachment_update())

    assert started == []
    assert submitted == []


def test_gate_does_not_swallow_private_commands(monkeypatch, tmp_path):
    started = []
    app = _build_dispatcher(monkeypatch, tmp_path, started)
    _dispatch(app, _build_update(ChatType.PRIVATE))
    assert len(started) == 1


def test_gate_blocks_group_commands_before_handlers(monkeypatch, tmp_path):
    started = []
    app = _build_dispatcher(monkeypatch, tmp_path, started)

    async def no_owner(context, chat_id, settings):
        return False

    monkeypatch.setattr(bot, "_group_has_owner", no_owner)
    _dispatch(app, _build_update(ChatType.GROUP, user_id=99))
    assert not started


def test_parse_inline_request_accepts_a_bare_link():
    parsed, host, error = bot._parse_inline_request(
        "https://example.com/file.bin"
    )

    assert error is None
    assert host == "gofile"
    assert parsed["source"] == "https://example.com/file.bin"
    assert parsed["dump_partitions"] == ()


def test_parse_inline_request_accepts_px_and_dump():
    parsed, host, error = bot._parse_inline_request(
        "/px@Mirror979Bot https://example.com/a.bin -dump boot,vendor"
    )

    assert error is None
    assert host == "pixeldrain"
    assert parsed["dump_partitions"] == ("boot", "vendor")


def test_parse_inline_request_rejects_a_missing_link():
    parsed, host, error = bot._parse_inline_request("/gf")

    assert parsed is None
    assert host == "gofile"
    assert "Usage:" in error


def test_parse_inline_request_rejects_an_empty_dump_list():
    parsed, host, error = bot._parse_inline_request(
        "/gf https://example.com/a.bin -dump"
    )

    assert parsed is None
    assert host == "gofile"
    assert "-dump boot,vendor" in error


def test_parse_inline_request_rejects_an_unknown_command():
    parsed, host, error = bot._parse_inline_request(
        "/status https://example.com/a.bin"
    )

    assert parsed is None
    assert host == "gofile"
    assert "Usage:" in error


def test_inline_result_attaches_the_start_keyboard():
    result = bot._inline_result(
        "gofile",
        "Mirror to Gofile",
        "https://example.com/file.bin",
        "Preparing",
        bot._inline_pending_keyboard("abc123"),
    )

    button = result.reply_markup.inline_keyboard[0][0]
    assert button.callback_data == "inline:start:abc123"


def test_inline_query_stores_a_pending_start_request(tmp_path):
    settings = Settings(
        telegram_token="token",
        allowed_user_ids=frozenset({1}),
        state_file=tmp_path / "telegram.json",
    )
    update = FakeUpdate(user_id=1)
    update.inline_query = FakeInlineQuery(
        "/px https://example.com/ota.zip -dump boot,vendor"
    )
    context = FakeContext(settings, [])

    asyncio.run(bot.on_inline_query(update, context))

    results, kwargs = update.inline_query.answers[0]
    assert kwargs["cache_time"] == 0
    button = results[0].reply_markup.inline_keyboard[0][0]
    prefix = "inline:start:"
    assert button.callback_data.startswith(prefix)
    token = button.callback_data[len(prefix) :]

    pending = bot._read_inline_pending(settings)
    assert pending[token]["user_id"] == 1
    assert pending[token]["source"] == "https://example.com/ota.zip"
    assert pending[token]["host"] == "pixeldrain"
    assert pending[token]["dump_partitions"] == ["boot", "vendor"]


def _inline_pending_entry(**overrides):
    entry = {
        "user_id": 1,
        "source": "https://example.com/ota.zip",
        "host": "gofile",
        "dump_partitions": ["boot"],
        "created_at": time.time(),
    }
    entry.update(overrides)
    return entry


class FakeManager:
    def __init__(self, jobs: list) -> None:
        self.jobs = jobs

    def active_jobs(self) -> list:
        return list(self.jobs)


def _run_inline_start(
    monkeypatch, tmp_path, *, token, entry, user_id=1, manager_jobs=None
):
    settings = Settings(
        telegram_token="token",
        allowed_user_ids=frozenset({1}),
        state_file=tmp_path / "telegram.json",
    )
    if entry is not None:
        bot._write_inline_pending(settings, {token: entry})

    update = FakeUpdate(user_id=user_id)
    update.callback_query = FakeCallbackQuery(f"inline:start:{token}", user_id=user_id)
    bot_instance = FakeBot()
    context = FakeContext(settings, [], bot_instance=bot_instance)
    if manager_jobs is not None:
        context.application.bot_data["manager"] = FakeManager(manager_jobs)

    started = []

    async def fake_start(context, job, message):
        started.append(job)

    monkeypatch.setattr(bot, "_start_job", fake_start)
    asyncio.run(bot.on_callback(update, context))
    return started, update.callback_query, bot_instance, settings


def test_inline_start_button_starts_the_job(monkeypatch, tmp_path):
    token = "start-token"
    started, query, _bot_instance, settings = _run_inline_start(
        monkeypatch, tmp_path, token=token, entry=_inline_pending_entry()
    )

    assert len(started) == 1
    job = started[0]
    assert job.source == "https://example.com/ota.zip"
    assert job.host == "gofile"
    assert job.inline_message_id == "inline-message-1"
    assert job.dump_partitions == ("boot",)
    assert query.answers[-1][0] == "Starting…"
    assert token not in bot._read_inline_pending(settings)


def test_inline_start_button_is_launcher_only(monkeypatch, tmp_path):
    token = "start-token"
    started, query, _bot_instance, settings = _run_inline_start(
        monkeypatch,
        tmp_path,
        token=token,
        entry=_inline_pending_entry(user_id=2),
    )

    assert started == []
    assert query.answers[-1] == (
        "Only the person who created this mirror can start it.",
        True,
    )
    assert token in bot._read_inline_pending(settings)


def test_inline_start_button_rejects_an_expired_request(monkeypatch, tmp_path):
    started, query, bot_instance, _settings = _run_inline_start(
        monkeypatch, tmp_path, token="missing", entry=None
    )

    assert started == []
    assert query.answers[-1] == (
        "This mirror request expired. Run the inline query again.",
        True,
    )
    assert bot_instance.edits[-1]["text"] == bot._inline_expired_text()
    assert not bot_instance.edits[-1]["reply_markup"].inline_keyboard


def test_inline_start_button_does_not_duplicate_a_running_job(monkeypatch, tmp_path):
    existing = bot.new_job(
        user_id=1,
        chat_id=0,
        source="https://example.com/ota.zip",
        host="gofile",
        user_name="User 1",
        inline_message_id="inline-message-1",
    )
    started, query, bot_instance, _settings = _run_inline_start(
        monkeypatch,
        tmp_path,
        token="start-token",
        entry=_inline_pending_entry(),
        manager_jobs=[existing],
    )

    assert started == []
    assert query.answers[-1] == ("This mirror is already running.", False)
    button = bot_instance.edits[-1]["reply_markup"].inline_keyboard[0][0]
    assert button.callback_data == f"cancel:{existing.id}"


def test_chosen_inline_result_does_not_duplicate_a_started_job(monkeypatch, tmp_path):
    settings = Settings(
        telegram_token="token",
        allowed_user_ids=frozenset({1}),
        state_file=tmp_path / "telegram.json",
    )
    existing = bot.new_job(
        user_id=1,
        chat_id=0,
        source="https://example.com/ota.zip",
        host="gofile",
        user_name="User 1",
        inline_message_id="inline-message-1",
    )
    update = FakeUpdate(user_id=1)
    update.chosen_inline_result = SimpleNamespace(
        inline_message_id="inline-message-1",
        query="/gf https://example.com/other.zip",
    )
    bot_instance = FakeBot()
    context = FakeContext(settings, [], bot_instance=bot_instance)
    context.application.bot_data["manager"] = FakeManager([existing])
    started = []

    async def fake_start(context, job, message):
        started.append(job)

    monkeypatch.setattr(bot, "_start_job", fake_start)
    asyncio.run(bot.on_chosen_inline_result(update, context))

    assert started == []
    button = bot_instance.edits[-1]["reply_markup"].inline_keyboard[0][0]
    assert button.callback_data == f"cancel:{existing.id}"
