from __future__ import annotations

import asyncio
from types import SimpleNamespace

from telegram.constants import ChatType

import bot
from mirror.config import Settings


class FakeUser:
    def __init__(self, user_id: int) -> None:
        self.id = user_id


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
        self.effective_user = FakeUser(user_id)
        self.effective_chat = FakeChat(chat_type)
        self.effective_message = FakeMessage()
        self.callback_query = None


class FakeManager:
    def __init__(self, active=None) -> None:
        self._active = list(active or [])

    def active_jobs(self):
        return list(self._active)


def make_context(settings: Settings, manager: FakeManager, stop_event):
    return SimpleNamespace(
        application=SimpleNamespace(
            bot_data={
                "settings": settings,
                "manager": manager,
                "stop_event": stop_event,
            }
        )
    )


def run_handoff(settings: Settings, manager: FakeManager | None = None, **update_kwargs):
    stop_event = asyncio.Event()
    context = make_context(settings, manager or FakeManager(), stop_event)
    update = FakeUpdate(**update_kwargs)
    asyncio.run(bot.cmd_handoff(update, context))
    return update, stop_event


def test_handoff_stops_the_local_runner():
    settings = Settings(
        telegram_token="token",
        allowed_user_ids=frozenset({1}),
        local_mode=True,
    )
    update, stop_event = run_handoff(settings)
    assert stop_event.is_set()
    assert "GitHub Actions" in update.effective_message.replies[0]


def test_handoff_refuses_while_a_job_is_active():
    settings = Settings(
        telegram_token="token",
        allowed_user_ids=frozenset({1}),
        local_mode=True,
    )
    update, stop_event = run_handoff(
        settings, manager=FakeManager([object()])
    )
    assert not stop_event.is_set()
    assert "still running" in update.effective_message.replies[0]


def test_handoff_is_unavailable_on_actions():
    settings = Settings(
        telegram_token="token",
        allowed_user_ids=frozenset({1}),
        local_mode=False,
    )
    update, stop_event = run_handoff(settings)
    assert not stop_event.is_set()
    assert "only available" in update.effective_message.replies[0].lower()


def test_handoff_must_be_used_in_a_private_chat():
    settings = Settings(
        telegram_token="token",
        allowed_user_ids=frozenset({1}),
        local_mode=True,
    )
    update, stop_event = run_handoff(settings, chat_type=ChatType.GROUP)
    assert not stop_event.is_set()
    assert "private chat" in update.effective_message.replies[0]


def test_handoff_rejects_a_stranger_in_private():
    settings = Settings(
        telegram_token="token",
        allowed_user_ids=frozenset({1}),
        local_mode=True,
    )
    update, stop_event = run_handoff(settings, user_id=99)
    assert not stop_event.is_set()
    assert "private" in update.effective_message.replies[0].lower()
