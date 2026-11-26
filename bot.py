"""Telegram mirror bot entry point."""

from __future__ import annotations

import asyncio
import html
import logging
import mimetypes
import secrets
import shutil
import signal
import time
from pathlib import Path
from typing import Optional, Sequence

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
    Message,
    Update,
)
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    ChosenInlineResultHandler,
    CommandHandler,
    ContextTypes,
    InlineQueryHandler,
    MessageHandler,
    TypeHandler,
    filters,
)

from mirror.config import Settings, load_settings
from mirror.dumper import normalize_partitions, parse_dump_args
from mirror.errors import DumpError
from mirror.handoff import (
    HandoffError,
    current_branch,
    dispatch_handoff,
    read_offset,
    resolve_repo,
    sync_offset_from_origin,
    take_over,
)
from mirror.jobs import Job, JobManager, new_job
from mirror.remote import RemoteJobManager, RemoteRecord
from mirror.render import STAGE_STYLES, render_final, render_job
from mirror.state import load_state, save_state
from mirror.uploaders import get_uploader
from mirror.utils import extract_url, human_size, sanitize_filename

logger = logging.getLogger(__name__)

# Telegram Bot API only serves files up to 20 MB to cloud bots.
TELEGRAM_FILE_LIMIT = 20 * 1024 * 1024

# --------------------------------------------------------------------------- UI
GROUP_CHAT_TYPES = frozenset({ChatType.GROUP, ChatType.SUPERGROUP})
GROUP_ACCESS_TTL = 5 * 60


def _authorized(update: Update, settings: Settings) -> bool:
    """Private chats are owner-only; trusted groups are open to their members."""
    user = update.effective_user
    if user is None:
        return False
    chat = getattr(update, "effective_chat", None)
    if chat is not None and getattr(chat, "type", None) in GROUP_CHAT_TYPES:
        return True
    return not settings.allowed_user_ids or user.id in settings.allowed_user_ids


async def _group_has_owner(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    settings: Settings,
) -> bool:
    """A group is trusted only while one of the allowed users is a member."""
    if not settings.allowed_user_ids:
        return True

    cache: dict[int, tuple[bool, float]] = context.application.bot_data.setdefault(
        "group_access", {}
    )
    now = time.monotonic()
    cached = cache.get(chat_id)
    if cached is not None and now - cached[1] < GROUP_ACCESS_TTL:
        return cached[0]

    allowed = False
    for owner_id in settings.allowed_user_ids:
        try:
            member = await context.bot.get_chat_member(chat_id, owner_id)
        except TelegramError:
            continue
        if getattr(member, "status", None) not in {"left", "kicked"}:
            allowed = True
            break

    cache[chat_id] = (allowed, now)
    return allowed


async def gate_group_access(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Ignore groups that none of the allowed users belong to."""
    chat = update.effective_chat
    if chat is None or getattr(chat, "type", None) not in GROUP_CHAT_TYPES:
        return
    settings: Settings = context.application.bot_data["settings"]
    if await _group_has_owner(context, chat.id, settings):
        return
    logger.info("Ignoring group %s: no allowed user is a member", chat.id)
    raise ApplicationHandlerStop


async def _deny(update: Update, settings: Settings) -> None:
    user = update.effective_user
    if not user:
        return
    text = (
        "This bot is private.\n"
        f"Your Telegram ID is <code>{user.id}</code> — add it to ALLOWED_USER_IDS to get access."
    )
    if update.callback_query:
        await update.callback_query.answer("Not authorized.", show_alert=True)
    elif update.effective_message:
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


def _cancel_keyboard(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Cancel job", callback_data=f"cancel:{job_id}")]]
    )


def _can_cancel(job: Job, user_id: int, settings: Settings) -> bool:
    """Only the job's launcher and configured admins may cancel it."""
    return job.user_id == user_id or user_id in settings.allowed_user_ids


# --------------------------------------------------------------- inline mode
INLINE_PENDING_TTL = 24 * 60 * 60


def _inline_pending_path(settings: Settings) -> Path:
    return settings.state_file.with_name("inline-pending.json")


def _inline_lock(context: ContextTypes.DEFAULT_TYPE) -> asyncio.Lock:
    return context.application.bot_data.setdefault("inline_lock", asyncio.Lock())


def _read_inline_pending(settings: Settings) -> dict[str, dict]:
    data = load_state(_inline_pending_path(settings))
    pending = data.get("pending")
    if not isinstance(pending, dict):
        return {}
    cutoff = time.time() - INLINE_PENDING_TTL
    cleaned: dict[str, dict] = {}
    for token, value in pending.items():
        if not isinstance(value, dict):
            continue
        try:
            created_at = float(value.get("created_at", 0))
        except (TypeError, ValueError):
            continue
        if created_at >= cutoff:
            cleaned[str(token)] = value
    return cleaned


def _write_inline_pending(settings: Settings, pending: dict[str, dict]) -> None:
    save_state(_inline_pending_path(settings), {"pending": pending})


def _job_for_inline_message(
    context: ContextTypes.DEFAULT_TYPE, inline_message_id: str
) -> Optional[Job]:
    remote = _remote_manager(context)
    if remote is not None:
        jobs = remote.active_jobs()
    else:
        manager: Optional[JobManager] = context.application.bot_data.get("manager")
        jobs = manager.active_jobs() if manager is not None else []
    for job in jobs:
        if job.inline_message_id == inline_message_id:
            return job
    return None


def _inline_usage_text() -> str:
    return (
        "Usage: <code>/gf &lt;link&gt; [-dump boot,vendor]</code>\n"
        "Use <code>/compress-gf</code> or <code>/compress-px</code> to pack the "
        "result into a maximum-compression 7z archive.\n"
        "Inline mode can only mirror a direct link. Reply-to-file is not "
        "available in inline mode."
    )


def _inline_pending_text(
    host: str, dump_partitions: Sequence[str], compress: bool = False
) -> str:
    lines = [
        "☁️ <b>GitHub Actions</b>",
        "<i>Preparing the mirror job…</i>",
        f"📦 {'Pixeldrain' if host == 'pixeldrain' else 'Gofile'}",
    ]
    if compress:
        lines.append("🗜 7-Zip · maximum compression")
    if dump_partitions:
        lines.append(f"🧩 {html.escape(', '.join(dump_partitions))}")
    lines.append("")
    lines.append("Tap <b>▶️ Start mirroring</b> if it does not start automatically.")
    return "\n".join(lines)


def _inline_expired_text() -> str:
    return (
        "⌛ <b>This mirror request expired.</b>\n"
        "Run the inline query again to start a new job."
    )


def _inline_result(
    result_id: str,
    title: str,
    description: str,
    text: str,
    keyboard: Optional[InlineKeyboardMarkup] = None,
) -> InlineQueryResultArticle:
    return InlineQueryResultArticle(
        id=result_id,
        title=title,
        description=description[:200],
        input_message_content=InputTextMessageContent(
            text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        ),
        reply_markup=keyboard,
    )


def _inline_pending_keyboard(token: str) -> InlineKeyboardMarkup:
    """Telegram only returns inline_message_id for messages with a keyboard."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "▶️ Start mirroring", callback_data=f"inline:start:{token}"
                )
            ]
        ]
    )


def _parse_inline_request(text: str):
    """Parse the text after ``@bot`` in an inline query."""
    tokens = (text or "").strip().split()
    command = "gf"
    host = "gofile"
    compress = False
    args = tokens
    if tokens and tokens[0].startswith("/"):
        first = tokens[0].split("@", 1)[0].lower()
        command = first[1:]
        if command not in {
            "gf",
            "mirror",
            "px",
            "compress-gf",
            "compress-px",
            "compress_gf",
            "compress_px",
        }:
            return None, host, _inline_usage_text()
        compress = command.startswith("compress")
        host = "pixeldrain" if command.endswith("px") else "gofile"
        args = tokens[1:]

    source = None
    source_index = None
    for index, arg in enumerate(args):
        found = extract_url(arg)
        if found:
            source = found
            source_index = index
            break
    if source is None:
        return None, host, _inline_usage_text()

    remaining = [arg for index, arg in enumerate(args) if index != source_index]
    _cleaned, dump_values, dump_requested = parse_dump_args(remaining)
    try:
        dump_partitions = normalize_partitions(dump_values)
    except DumpError as exc:
        return None, host, html.escape(str(exc))
    if dump_requested and not dump_partitions:
        return (
            None,
            host,
            "Add partition names after <code>-dump</code>, e.g. "
            "<code>/gf &lt;link&gt; -dump boot,vendor</code>.",
        )
    return (
        {
            "source": source,
            "host": host,
            "dump_partitions": dump_partitions,
            "compress": compress,
        },
        host,
        None,
    )


# ---------------------------------------------------------------------- helpers
def _discard_upload(path: Optional[Path]) -> None:
    if not path:
        return
    path.unlink(missing_ok=True)
    try:
        path.parent.rmdir()
    except OSError:
        pass


def _attachment_filename(message: Message, attachment) -> str:
    """Pick a stable, safe filename for a file sent in Telegram."""
    name = getattr(attachment, "file_name", None)
    if name:
        return sanitize_filename(name, "file.bin")

    mime_type = (getattr(attachment, "mime_type", None) or "").split(";")[0].strip().lower()
    extension = mimetypes.guess_extension(mime_type) if mime_type else None
    if extension == ".jpe":
        extension = ".jpg"

    unique = getattr(attachment, "file_unique_id", None) or secrets.token_hex(4)
    if message.photo:
        prefix, extension = "photo", extension or ".jpg"
    elif message.video:
        prefix, extension = "video", extension or ".mp4"
    elif message.audio:
        prefix, extension = "audio", extension or ".mp3"
    elif message.voice:
        # Telegram voice notes are always OGG/Opus, even though mimetypes may
        # suggest the equivalent ".oga" extension.
        prefix, extension = "voice", ".ogg"
    elif message.video_note:
        prefix, extension = "video_note", extension or ".mp4"
    elif message.animation:
        prefix, extension = "animation", extension or ".mp4"
    elif message.sticker:
        prefix = "sticker"
        if message.sticker.is_animated:
            extension = ".tgs"
        elif message.sticker.is_video:
            extension = ".webm"
        else:
            extension = extension or ".webp"
    else:
        prefix, extension = "file", extension or ".bin"

    return sanitize_filename(f"{prefix}_{unique}{extension}", f"{prefix}{extension or '.bin'}")


def _message_attachment(message: Message):
    """Return the file-like attachment of a Telegram message, if any."""
    if message.photo:
        return max(
            message.photo,
            key=lambda photo: photo.file_size or (photo.width * photo.height),
        )
    for attribute in (
        "document",
        "video",
        "audio",
        "voice",
        "video_note",
        "animation",
        "sticker",
    ):
        attachment = getattr(message, attribute, None)
        if attachment:
            return attachment
    return None


async def _save_telegram_file(
    message: Message,
    attachment,
    filename: str,
    status: Message,
    settings: Settings,
) -> Optional[Path]:
    uploads = (settings.download_dir / "uploads").resolve()
    uploads.mkdir(parents=True, exist_ok=True)
    folder = uploads / secrets.token_hex(8)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / filename

    try:
        telegram_file = await attachment.get_file()
        await telegram_file.download_to_drive(custom_path=str(path))
    except (TelegramError, OSError) as exc:
        _discard_upload(path)
        error = f"Could not download that file: {html.escape(str(exc))}"
        try:
            await status.edit_text(error, parse_mode=ParseMode.HTML)
        except TelegramError:
            await message.reply_text(error, parse_mode=ParseMode.HTML)
        return None

    if not path.exists() or path.stat().st_size == 0:
        _discard_upload(path)
        error = "The file Telegram sent was empty."
        try:
            await status.edit_text(error)
        except TelegramError:
            await message.reply_text(error)
        return None

    return path


def _remote_manager(context: ContextTypes.DEFAULT_TYPE) -> Optional[RemoteJobManager]:
    return context.application.bot_data.get("remote")


async def _start_remote_job(
    context: ContextTypes.DEFAULT_TYPE,
    remote: RemoteJobManager,
    job: Job,
    message: Optional[Message],
) -> None:
    """Create the progress card, then dispatch the work to GitHub Actions."""
    job.update(
        status="running",
        stage="remote",
        started_at=time.time(),
        detail="Starting a GitHub Actions runner…",
    )
    text = render_job(job)
    if job.inline_message_id:
        await _edit_inline_card(
            context.bot,
            job.inline_message_id,
            text,
            keyboard=_cancel_keyboard(job.id),
        )
        message_id = 0
    else:
        status_message = message
        try:
            if status_message is None:
                status_message = await context.bot.send_message(
                    job.chat_id,
                    text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=_cancel_keyboard(job.id),
                )
            else:
                await status_message.edit_text(
                    text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=_cancel_keyboard(job.id),
                )
        except TelegramError:
            logger.warning("Could not create the remote job status message", exc_info=True)
            try:
                status_message = await context.bot.send_message(
                    job.chat_id,
                    text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=_cancel_keyboard(job.id),
                )
            except TelegramError:
                logger.exception("Could not send the remote job status message")
                _discard_upload(job.local_path)
                return

        message_id = int(getattr(status_message, "message_id", 0) or 0)
        if message_id <= 0:
            logger.error("The Telegram status message has no id; not dispatching job %s", job.id)
            _discard_upload(job.local_path)
            return

    try:
        dispatch_kwargs = {"message_id": message_id}
        if job.inline_message_id:
            dispatch_kwargs["inline_message_id"] = job.inline_message_id
        await asyncio.to_thread(remote.dispatch, job, **dispatch_kwargs)
    except Exception as exc:
        reason = str(exc) or type(exc).__name__
        logger.error("Could not dispatch job %s to GitHub Actions: %s", job.id, reason)
        job.update(
            status="failed",
            stage="failed",
            error=f"Could not start GitHub Actions: {reason}",
            finished_at=time.time(),
        )
        _discard_upload(job.local_path)
        if job.inline_message_id:
            await _edit_inline_card(
                context.bot,
                job.inline_message_id,
                render_final(job),
                keyboard=InlineKeyboardMarkup([]),
            )
        else:
            await _edit_remote_card(
                context.bot,
                job.chat_id,
                message_id,
                render_final(job),
                keyboard=None,
            )
        return

    if job.inline_message_id:
        await _edit_inline_card(
            context.bot,
            job.inline_message_id,
            render_job(job),
            keyboard=_cancel_keyboard(job.id),
        )
    else:
        await _edit_remote_card(
            context.bot,
            job.chat_id,
            message_id,
            render_job(job),
            keyboard=_cancel_keyboard(job.id),
        )


async def _edit_inline_card(
    bot,
    inline_message_id: str,
    text: str,
    *,
    keyboard,
) -> None:
    try:
        await bot.edit_message_text(
            inline_message_id=inline_message_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            logger.debug("Inline status edit failed: %s", exc)
    except TelegramError:
        logger.debug("Inline message is no longer editable", exc_info=True)


async def _edit_remote_card(
    bot,
    chat_id: int,
    message_id: int,
    text: str,
    *,
    keyboard,
    inline_message_id: str = "",
) -> None:
    if inline_message_id:
        await _edit_inline_card(
            bot,
            inline_message_id,
            text,
            keyboard=keyboard,
        )
        return
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            logger.debug("Remote status edit failed: %s", exc)
    except TelegramError:
        logger.debug("Remote status message is no longer editable", exc_info=True)


REMOTE_FAILURE_CONCLUSIONS = {"failure", "timed_out", "startup_failure"}


async def _deliver_remote_failure(bot, record: RemoteRecord) -> None:
    """Finalize a failed run when its worker never posted a card.

    A normal worker failure deletes the progress message and sends its own
    detailed card, so ``delete_message`` reports "not found" and we leave it
    alone.  If the worker died before that (bad secret, runner shutdown, failed
    dependency install), the still-existing progress card is replaced here.
    """
    job = record.job
    if record.inline_message_id:
        job.update(
            status="failed",
            stage="failed",
            error="GitHub Actions stopped before the worker could report details.",
            finished_at=time.time(),
        )
        text = render_final(job)
        if record.run_url:
            url = html.escape(record.run_url, quote=True)
            text += f'\n\n🔗 <a href="{url}">Actions run</a>'
        await _edit_inline_card(
            bot,
            record.inline_message_id,
            text,
            keyboard=InlineKeyboardMarkup([]),
        )
        return
    if not record.message_id:
        return
    try:
        deleted = await bot.delete_message(job.chat_id, record.message_id)
    except BadRequest as exc:
        if "not found" in str(exc).lower():
            return
        logger.debug(
            "Could not remove the failed remote card for job %s", job.id, exc_info=True
        )
        return
    except TelegramError:
        logger.debug(
            "Could not remove the failed remote card for job %s", job.id, exc_info=True
        )
        return
    if not deleted:
        return

    job.update(
        status="failed",
        stage="failed",
        error="GitHub Actions stopped before the worker could report details.",
        finished_at=time.time(),
    )
    text = render_final(job)
    if record.run_url:
        url = html.escape(record.run_url, quote=True)
        text += f'\n\n🔗 <a href="{url}">Actions run</a>'
    try:
        await bot.send_message(
            job.chat_id,
            text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except TelegramError:
        logger.exception("Could not deliver the failed card for job %s", job.id)


async def _deliver_remote_cancelled(bot, record: RemoteRecord) -> None:
    """Post the final cancelled card after GitHub finishes stopping a run."""
    job = record.job
    job.update(
        status="cancelled",
        stage="cancelled",
        detail="Cancelled.",
        finished_at=time.time(),
    )
    final = render_final(job)

    if record.inline_message_id:
        await _edit_inline_card(
            bot,
            record.inline_message_id,
            final,
            keyboard=InlineKeyboardMarkup([]),
        )
        return

    deleted = False
    if record.message_id:
        try:
            await bot.delete_message(job.chat_id, record.message_id)
            deleted = True
        except TelegramError:
            logger.debug(
                "Could not delete the remote progress message for job %s",
                job.id,
                exc_info=True,
            )

    if not deleted and record.message_id:
        try:
            await bot.edit_message_text(
                chat_id=job.chat_id,
                message_id=record.message_id,
                text=final,
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([]),
                disable_web_page_preview=True,
            )
            return
        except TelegramError:
            logger.debug(
                "Could not edit the remote progress message for job %s",
                job.id,
                exc_info=True,
            )

    try:
        await bot.send_message(
            job.chat_id,
            final,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except TelegramError:
        logger.exception("Could not deliver the cancelled card for job %s", job.id)


async def _start_job(
    context: ContextTypes.DEFAULT_TYPE,
    job: Job,
    message: Optional[Message],
) -> None:
    if not context.application.bot_data.get("accepting_jobs", True):
        _discard_upload(job.local_path)
        text = "The bot is finishing its current run. Send the file again in a few minutes."
        if job.inline_message_id:
            await _edit_inline_card(
                context.bot,
                job.inline_message_id,
                text,
                keyboard=InlineKeyboardMarkup([]),
            )
            return
        if message is not None:
            try:
                await message.edit_text(text)
                return
            except TelegramError:
                pass
        await context.bot.send_message(job.chat_id, text)
        return

    remote = _remote_manager(context)
    if remote is not None:
        await _start_remote_job(context, remote, job, message)
        return

    manager: JobManager = context.application.bot_data["manager"]
    await manager.submit(job)

    if job.inline_message_id:
        await _edit_inline_card(
            context.bot,
            job.inline_message_id,
            render_job(job),
            keyboard=_cancel_keyboard(job.id),
        )
        reporters: set[asyncio.Task] = context.application.bot_data.setdefault(
            "reporters", set()
        )
        task = asyncio.create_task(_report_job(context, job, None))
        reporters.add(task)
        task.add_done_callback(reporters.discard)
        return

    text = render_job(job)
    status_message = message
    try:
        if status_message is None:
            status_message = await context.bot.send_message(
                job.chat_id,
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=_cancel_keyboard(job.id),
            )
        else:
            await status_message.edit_text(
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=_cancel_keyboard(job.id),
            )
    except TelegramError:
        logger.warning("Could not update the status message", exc_info=True)
        try:
            status_message = await context.bot.send_message(
                job.chat_id,
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=_cancel_keyboard(job.id),
            )
        except TelegramError:
            logger.exception("Could not send job status message")
            return

    reporters: set[asyncio.Task] = context.application.bot_data.setdefault("reporters", set())
    task = asyncio.create_task(_report_job(context, job, status_message))
    reporters.add(task)
    task.add_done_callback(reporters.discard)


async def _report_job(
    context: ContextTypes.DEFAULT_TYPE,
    job: Job,
    message: Optional[Message],
) -> None:
    last_text = ""
    while not job.finished:
        await asyncio.sleep(4)
        text = render_job(job)
        if text == last_text:
            continue
        try:
            if job.inline_message_id:
                await _edit_inline_card(
                    context.bot,
                    job.inline_message_id,
                    text,
                    keyboard=_cancel_keyboard(job.id),
                )
            elif message is not None:
                await message.edit_text(
                    text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=_cancel_keyboard(job.id),
                )
            else:
                continue
            last_text = text
        except BadRequest as exc:
            if "not modified" not in str(exc).lower():
                logger.debug("Status edit failed: %s", exc)
        except TelegramError:
            logger.debug("Status message is no longer editable", exc_info=True)
            break

    await asyncio.sleep(0.4)
    await _deliver_final(context, job, message, render_final(job))


async def _deliver_final(
    context: ContextTypes.DEFAULT_TYPE,
    job: Job,
    message: Optional[Message],
    final: str,
) -> None:
    """Delete the live progress message and post a clean completion message."""
    if job.inline_message_id:
        await _edit_inline_card(
            context.bot,
            job.inline_message_id,
            final,
            keyboard=InlineKeyboardMarkup([]),
        )
        return
    if message is None:
        return
    try:
        await message.delete()
    except TelegramError:
        logger.debug(
            "Could not delete the progress message for job %s; editing it instead",
            job.id,
            exc_info=True,
        )
        try:
            await message.edit_text(
                final,
                parse_mode=ParseMode.HTML,
                reply_markup=None,
                disable_web_page_preview=True,
            )
            return
        except TelegramError:
            logger.debug(
                "Could not edit the progress message for job %s; sending a new message",
                job.id,
                exc_info=True,
            )

    try:
        await context.bot.send_message(
            job.chat_id,
            final,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except TelegramError:
        logger.exception("Could not deliver final status for job %s", job.id)


async def _submit_job(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    source: str,
    source_kind: str = "url",
    local_path: Optional[Path] = None,
    telegram_file_id: Optional[str] = None,
    file_name: Optional[str] = None,
    file_size: Optional[int] = None,
    status_message: Optional[Message] = None,
    host: str = "gofile",
    dump_partitions: Sequence[str] = (),
    compress: bool = False,
    inline_message_id: Optional[str] = None,
) -> None:
    settings: Settings = context.application.bot_data["settings"]
    message = update.effective_message
    remote = _remote_manager(context)
    if remote is not None and local_path is not None:
        # Remote workers download Telegram attachments themselves; do not keep
        # a second copy on the PC controller.
        _discard_upload(local_path)
        local_path = None

    async def reject(text: str) -> None:
        _discard_upload(local_path)
        if status_message is not None:
            try:
                await status_message.edit_text(text, parse_mode=ParseMode.HTML)
                return
            except TelegramError:
                pass
        await message.reply_text(text, parse_mode=ParseMode.HTML)

    if not context.application.bot_data.get("accepting_jobs", True):
        await reject("The bot is finishing its current run. Send the file again in a few minutes.")
        return

    if remote is None:
        # The PC owns local uploads. A remote worker uses the credentials in
        # the encrypted payload or its repository secrets, so the controller
        # cannot reliably check them here.
        usable, reason = get_uploader(host, settings).available()
        if not usable:
            await reject(f"⚠️ {html.escape(reason)}")
            return

    user = update.effective_user
    job = new_job(
        user_id=user.id,
        chat_id=update.effective_chat.id,
        source=source,
        host=host,
        user_name=(
            getattr(user, "full_name", "")
            or getattr(user, "first_name", "")
            or f"User {user.id}"
        ),
        source_kind=source_kind,
        local_path=local_path,
        telegram_file_id=telegram_file_id,
        inline_message_id=inline_message_id,
        dump_partitions=dump_partitions,
        compress=compress,
    )
    job.update(file_name=file_name, file_size=file_size)
    await _start_job(context, job, status_message)


# --------------------------------------------------------------------- inline
async def on_inline_query(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    settings: Settings = context.application.bot_data["settings"]
    query = update.inline_query
    user = update.effective_user
    if query is None or user is None:
        return

    if not _authorized(update, settings):
        results = [
            _inline_result(
                "denied",
                "This bot is private",
                "Tap to see your Telegram ID",
                "This bot is private.\n"
                f"Your Telegram ID is <code>{user.id}</code> — add it to "
                "ALLOWED_USER_IDS to get access.",
            )
        ]
        await query.answer(results, cache_time=0, is_personal=True)
        return

    parsed, host, error = _parse_inline_request(query.query or "")
    if error is not None:
        results = [
            _inline_result(
                "usage",
                "Usage: /gf <link>",
                "Example: /gf https://example.com/file.zip",
                error,
            )
        ]
    else:
        token = secrets.token_urlsafe(12)
        async with _inline_lock(context):
            pending = _read_inline_pending(settings)
            pending[token] = {
                "user_id": user.id,
                "source": parsed["source"],
                "host": host,
                "dump_partitions": list(parsed["dump_partitions"]),
                "compress": bool(parsed.get("compress")),
                "created_at": time.time(),
            }
            _write_inline_pending(settings, pending)
        title = (
            ("Compress to Pixeldrain" if parsed.get("compress") else "Mirror to Pixeldrain")
            if host == "pixeldrain"
            else ("Compress to Gofile" if parsed.get("compress") else "Mirror to Gofile")
        )
        description = parsed["source"]
        if parsed["dump_partitions"]:
            description += " · dump " + ", ".join(parsed["dump_partitions"])
        if parsed.get("compress"):
            description += " · 7z"
        results = [
            _inline_result(
                host,
                title,
                description,
                _inline_pending_text(
                    host,
                    parsed["dump_partitions"],
                    bool(parsed.get("compress")),
                ),
                _inline_pending_keyboard(token),
            )
        ]

    await query.answer(results, cache_time=0, is_personal=True)


async def on_chosen_inline_result(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    settings: Settings = context.application.bot_data["settings"]
    result = update.chosen_inline_result
    user = update.effective_user
    if result is None or user is None:
        return

    inline_message_id = getattr(result, "inline_message_id", None)
    if not inline_message_id:
        logger.warning(
            "Chosen inline result has no inline_message_id; enable inline "
            "feedback in BotFather"
        )
        return

    if not _authorized(update, settings):
        await _edit_inline_card(
            context.bot,
            inline_message_id,
            "This bot is private.\n"
            f"Your Telegram ID is <code>{user.id}</code> — add it to "
            "ALLOWED_USER_IDS to get access.",
            keyboard=InlineKeyboardMarkup([]),
        )
        return

    parsed, host, error = _parse_inline_request(result.query or "")
    if error is not None:
        await _edit_inline_card(
            context.bot,
            inline_message_id,
            error,
            keyboard=InlineKeyboardMarkup([]),
        )
        return

    existing = _job_for_inline_message(context, inline_message_id)
    if existing is not None:
        await _edit_inline_card(
            context.bot,
            inline_message_id,
            render_job(existing),
            keyboard=_cancel_keyboard(existing.id),
        )
        return

    job = new_job(
        user_id=user.id,
        chat_id=0,
        source=parsed["source"],
        host=host,
        user_name=(
            getattr(user, "full_name", "")
            or getattr(user, "first_name", "")
            or f"User {user.id}"
        ),
        inline_message_id=inline_message_id,
        dump_partitions=parsed["dump_partitions"],
        compress=bool(parsed.get("compress")),
    )
    await _start_job(context, job, None)


# -------------------------------------------------------------------- commands
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not _authorized(update, settings):
        await _deny(update, settings)
        return
    await update.effective_message.reply_text(
        "<b>Mirror bot</b>\n\n"
        "Send me a link or a magnet link. "
        + (
            "A GitHub Actions worker downloads it and uploads it to Gofile or "
            "Pixeldrain, then replies with the public link. This PC only handles "
            "Telegram commands.\n\n"
            if settings.remote_jobs
            else "I download it and upload it to Gofile or Pixeldrain, then reply "
            "with the public link.\n\n"
        )
        + (
            f"<b>Inline mode</b>\n"
            f"Type <code>@{context.bot.username} /gf &lt;link&gt;</code> in any "
            "chat. The bot posts an inline card there and updates it with "
            "progress and the final link. Inline mode only accepts direct "
            "links. Telegram caps inline queries at 256 characters; for long "
            "links with <code>-dump</code>, send the command as a normal "
            "message in the private chat.\n\n"
            if context.bot.username
            else ""
        )
        + "To mirror a file or GIF that is already in the chat, reply to it with "
        "<code>/gf</code> or <code>/px</code>. Other attachments are ignored. "
        "Telegram bots can download replied attachments up to 20 MB; send a link "
        "for anything larger.\n\n"
        "Any file type, Gofile share links, pages with a download button, video sites, "
        "image galleries, and public MEGA links are supported.\n\n"
        "<b>Commands</b>\n"
        "/gf &lt;link&gt; — upload to Gofile, or reply to a file with /gf\n"
        "/mirror &lt;link&gt; — same as /gf\n"
        "/px &lt;link&gt; — upload to Pixeldrain, or reply to a file with /px "
        "(requires a free Pixeldrain API key)\n"
        "/compress-gf &lt;link&gt; — download, pack into a maximum-compression "
        "7z archive, upload to Gofile, and report the archive SHA-256\n"
        "/compress-px &lt;link&gt; — same, but upload the 7z to Pixeldrain\n"
        "/status — active jobs\n"
        "/cancel — cancel your jobs\n"
        "/id — show your Telegram ID\n"
        + (
            "/handoff — stop the PC bot and start the GitHub Actions fallback\n"
            if settings.local_mode
            else ""
        )
        + "\n<b>Android partition dump</b>\n"
        "/gf &lt;link&gt; -dump boot,vendor — download an OTA zip, fastboot ROM, "
        "or 7z/RAR archive, extract only those partition images from payload.bin "
        "or super.img, and upload them. Use <code>-dump</code> with spaces too: "
        "<code>-dump boot vendor</code>.\n"
        "/compress-gf &lt;link&gt; -dump boot,vendor — extract those images, "
        "pack them into one maximum-compression 7z archive, upload it, and "
        "report its SHA-256. <code>/compress-px</code> does the same for "
        "Pixeldrain. Compression needs 7-Zip on the worker (set "
        "<code>SEVEN_ZIP_PATH</code> if it is not on PATH).",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, context)


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not _authorized(update, settings):
        await _deny(update, settings)
        return
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return
    await update.effective_message.reply_text(
        f"User ID: <code>{user.id}</code>\nChat ID: <code>{chat.id}</code>",
        parse_mode=ParseMode.HTML,
    )


async def _cmd_link(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    command: str,
    host: str = "gofile",
    compress: bool = False,
) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not _authorized(update, settings):
        await _deny(update, settings)
        return

    raw_args = list(context.args or [])
    source = None
    source_index: Optional[int] = None
    for index, arg in enumerate(raw_args):
        found = extract_url(arg)
        if found:
            source = found
            source_index = index
            break

    remaining = [
        arg for index, arg in enumerate(raw_args) if index != source_index
    ]
    _cleaned, dump_values, dump_requested = parse_dump_args(remaining)
    try:
        dump_partitions = normalize_partitions(dump_values)
    except DumpError as exc:
        await update.effective_message.reply_text(
            html.escape(str(exc)), parse_mode=ParseMode.HTML
        )
        return

    if dump_requested and not dump_partitions:
        await update.effective_message.reply_text(
            f"Add partition names after <code>-dump</code>, e.g. "
            f"<code>/{command} &lt;link&gt; -dump boot,vendor</code>.",
            parse_mode=ParseMode.HTML,
        )
        return

    replied = getattr(update.effective_message, "reply_to_message", None)
    if not source and replied is not None:
        attachment = _message_attachment(replied)
        if attachment is not None:
            await _mirror_attachment(
                update,
                context,
                replied,
                attachment,
                host=host,
                dump_partitions=dump_partitions,
                compress=compress,
            )
            return
        if replied.text:
            source = extract_url(replied.text)

    if not source:
        await update.effective_message.reply_text(
            f"Usage: <code>/{command} &lt;link&gt; [-dump boot,vendor]</code>\n"
            f"Or reply to a file or link with "
            f"<code>/{command} [-dump boot,vendor]</code>.",
            parse_mode=ParseMode.HTML,
        )
        return

    await _submit_job(
        update,
        context,
        source=source,
        host=host,
        dump_partitions=dump_partitions,
        compress=compress,
    )


async def cmd_mirror(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _cmd_link(update, context, command="mirror")


async def cmd_gf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _cmd_link(update, context, command="gf")


async def cmd_px(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _cmd_link(update, context, command="px", host="pixeldrain")


async def cmd_compress(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route /compress-gf and /compress-px.

    Telegram command names cannot contain hyphens, so some clients report only
    ``/compress`` in the command entity. The full message text still contains
    the suffix, so route on that.
    """
    message = update.effective_message
    text = (getattr(message, "text", None) or "").strip()
    token = text.split()[0].split("@", 1)[0].lower() if text.split() else ""
    if token in {"/compress-px", "/compress_px"}:
        await _cmd_link(
            update,
            context,
            command="compress-px",
            host="pixeldrain",
            compress=True,
        )
        return
    if token in {"/compress-gf", "/compress_gf"}:
        await _cmd_link(
            update,
            context,
            command="compress-gf",
            compress=True,
        )
        return
    if message is not None:
        await message.reply_text(
            "Usage: <code>/compress-gf &lt;link&gt; [-dump boot,vendor]</code>\n"
            "<code>/compress-px &lt;link&gt; [-dump boot,vendor]</code>",
            parse_mode=ParseMode.HTML,
        )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not _authorized(update, settings):
        await _deny(update, settings)
        return
    manager: JobManager = context.application.bot_data["manager"]
    jobs = manager.active_for(update.effective_user.id)
    if not jobs:
        await update.effective_message.reply_text("No active jobs.")
        return

    lines = ["<b>Active jobs</b>"]
    for job in jobs[:10]:
        snap = job.snapshot()
        icon, title = STAGE_STYLES.get(snap["stage"], ("⚙️", snap["stage"].title()))
        percent = f" · {snap['percent']:.0f}%" if snap["percent"] is not None else ""
        lines.append(f"{icon} <code>{snap['id']}</code> · <b>{title}</b>{percent}")
        if snap["detail"]:
            lines.append(f"<i>{html.escape(str(snap['detail']))}</i>")
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not _authorized(update, settings):
        await _deny(update, settings)
        return

    remote = _remote_manager(context)
    if remote is not None:
        records = remote.records_for(update.effective_user.id)
        count = 0
        for record in records:
            if await asyncio.to_thread(remote.cancel_job, record.job.id):
                count += 1
                await _mark_remote_cancelling(context.bot, record)
        if count:
            await update.effective_message.reply_text(f"Cancelling {count} job(s)…")
        else:
            await update.effective_message.reply_text("You have no active jobs.")
        return

    manager: JobManager = context.application.bot_data["manager"]
    count = manager.cancel_user(update.effective_user.id)
    if count:
        await update.effective_message.reply_text(f"Cancelling {count} job(s)…")
    else:
        await update.effective_message.reply_text("You have no active jobs.")


async def _mark_remote_cancelling(bot, record: RemoteRecord) -> None:
    if record.inline_message_id:
        await _edit_inline_card(
            bot,
            record.inline_message_id,
            render_job(record.job),
            keyboard=InlineKeyboardMarkup([]),
        )
        return
    if record.message_id <= 0:
        return
    await _edit_remote_card(
        bot,
        record.job.chat_id,
        record.message_id,
        render_job(record.job),
        keyboard=InlineKeyboardMarkup([]),
    )


async def cmd_handoff(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Stop the PC runner so the on-demand Actions fallback can take over."""
    settings: Settings = context.application.bot_data["settings"]
    if not _authorized(update, settings):
        await _deny(update, settings)
        return

    user = update.effective_user
    if update.effective_chat and update.effective_chat.type != ChatType.PRIVATE:
        await update.effective_message.reply_text(
            "Use /handoff in a private chat with the bot."
        )
        return
    if settings.allowed_user_ids and (
        user is None or user.id not in settings.allowed_user_ids
    ):
        await update.effective_message.reply_text(
            "Only the bot owner can hand over to GitHub Actions."
        )
        return
    if not settings.local_mode:
        await update.effective_message.reply_text(
            "Handoff is only available when the bot is running on the PC."
        )
        return

    manager: JobManager = context.application.bot_data["manager"]
    if manager.active_jobs():
        await update.effective_message.reply_text(
            "Jobs are still running. Wait for them to finish or cancel them first."
        )
        return

    stop_event = context.application.bot_data.get("stop_event")
    if stop_event is None:
        await update.effective_message.reply_text(
            "The local runner is not ready for handoff yet."
        )
        return

    await update.effective_message.reply_text(
        "Stopping the PC bot. GitHub Actions will take over in about a minute."
    )
    stop_event.set()


# --------------------------------------------------------------------- messages
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    message = update.effective_message
    if message is None or not message.text:
        return
    # In groups, only react to commands to avoid replying to every link.
    if update.effective_chat and update.effective_chat.type != ChatType.PRIVATE:
        return
    source = extract_url(message.text)
    if not source:
        if _authorized(update, settings):
            await message.reply_text(
                "Send me a link or a magnet link. To mirror a file or GIF, "
                "reply to it with /gf or /px."
            )
        return
    if not _authorized(update, settings):
        await _deny(update, settings)
        return
    await _submit_job(update, context, source=source)


async def _mirror_attachment(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    attachment_message: Message,
    attachment,
    *,
    host: str = "gofile",
    dump_partitions: Sequence[str] = (),
    compress: bool = False,
) -> None:
    """Submit a Telegram attachment, locally or to a GitHub Actions worker."""
    settings: Settings = context.application.bot_data["settings"]
    remote = _remote_manager(context)
    message = update.effective_message
    if message is None:
        return

    document = attachment_message.document
    if document:
        name = document.file_name or ""
        is_torrent = name.lower().endswith(".torrent") or document.mime_type in {
            "application/x-bittorrent",
            "application/x-bittorrent; charset=utf-8",
        }
        if is_torrent:
            if document.file_size and document.file_size > TELEGRAM_FILE_LIMIT:
                await message.reply_text(
                    "Telegram only lets bots download files up to 20 MB. "
                    "Send a direct link to a larger .torrent file instead."
                )
                return
            filename = sanitize_filename(name or "file.torrent", "file.torrent")
            file_id = getattr(document, "file_id", None)
            if remote is not None:
                if not file_id:
                    await message.reply_text(
                        "Telegram did not provide a file id for that .torrent file."
                    )
                    return
                await _submit_job(
                    update,
                    context,
                    source=filename,
                    source_kind="torrent_file",
                    telegram_file_id=file_id,
                    file_name=filename,
                    file_size=document.file_size,
                    host=host,
                    dump_partitions=dump_partitions,
                    compress=compress,
                )
                return
            status = await message.reply_text(
                f"Downloading <code>{html.escape(filename)}</code> from Telegram…",
                parse_mode=ParseMode.HTML,
            )
            path = await _save_telegram_file(
                message, document, filename, status, settings
            )
            if path is None:
                return
            await _submit_job(
                update,
                context,
                source=filename,
                source_kind="torrent_file",
                local_path=path,
                status_message=status,
                host=host,
                dump_partitions=dump_partitions,
                compress=compress,
            )
            return

    size = getattr(attachment, "file_size", None)
    if size and size > TELEGRAM_FILE_LIMIT:
        await message.reply_text(
            "Telegram only lets bots download files up to 20 MB. "
            "Send a direct link to a larger file instead."
        )
        return
    if size and settings.max_file_size > 0 and size > settings.max_file_size:
        await message.reply_text(
            f"That file is {human_size(size)}, above the "
            f"{human_size(settings.max_file_size)} limit."
        )
        return

    filename = _attachment_filename(attachment_message, attachment)
    if remote is not None:
        file_id = getattr(attachment, "file_id", None)
        if not file_id:
            await message.reply_text(
                "Telegram did not provide a file id for that attachment."
            )
            return
        await _submit_job(
            update,
            context,
            source=filename,
            source_kind="telegram_file",
            telegram_file_id=file_id,
            file_name=filename,
            file_size=size,
            host=host,
            dump_partitions=dump_partitions,
            compress=compress,
        )
        return

    status = await message.reply_text(
        f"Downloading <code>{html.escape(filename)}</code> from Telegram…",
        parse_mode=ParseMode.HTML,
    )
    path = await _save_telegram_file(message, attachment, filename, status, settings)
    if path is None:
        return
    await _submit_job(
        update,
        context,
        source=filename,
        source_kind="telegram_file",
        local_path=path,
        status_message=status,
        host=host,
        dump_partitions=dump_partitions,
        compress=compress,
    )


# ---------------------------------------------------------------------- buttons
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    query = update.callback_query
    if query is None or query.data is None:
        return
    if not _authorized(update, settings):
        await _deny(update, settings)
        return

    parts = query.data.split(":")
    if parts[0] == "inline" and len(parts) >= 2 and parts[1] == "start":
        await _handle_inline_start(
            update, context, parts[2] if len(parts) > 2 else ""
        )
        return
    if parts[0] == "inline" and len(parts) == 2 and parts[1] == "pending":
        await query.answer(
            "This card is outdated. Run the inline query again.", show_alert=True
        )
        inline_message_id = getattr(query, "inline_message_id", None)
        if inline_message_id:
            await _edit_inline_card(
                context.bot,
                inline_message_id,
                _inline_expired_text(),
                keyboard=InlineKeyboardMarkup([]),
            )
        return
    if parts[0] == "pick":
        await query.answer(
            "That menu is no longer used. Send the link again.", show_alert=True
        )
        return

    if parts[0] == "cancel" and len(parts) == 2:
        remote = _remote_manager(context)
        if remote is not None:
            record = remote.record(parts[1])
            if record is None or record.job.finished:
                await query.answer(
                    "Job not found or already finished.", show_alert=True
                )
                return
            job = record.job
            user_id = update.effective_user.id
            if not _can_cancel(job, user_id, settings):
                await query.answer(
                    "Only the job's launcher or an admin can cancel it.",
                    show_alert=True,
                )
                return
            await query.answer("Cancelling…")
            if await asyncio.to_thread(remote.cancel_job, job.id, user_id=user_id):
                await _mark_remote_cancelling(context.bot, record)
            elif record.inline_message_id:
                await _edit_inline_card(
                    context.bot,
                    record.inline_message_id,
                    "Could not cancel the GitHub Actions run. Try /cancel again.",
                    keyboard=InlineKeyboardMarkup([]),
                )
            else:
                await _edit_remote_card(
                    context.bot,
                    job.chat_id,
                    record.message_id,
                    "Could not cancel the GitHub Actions run. Try /cancel again.",
                    keyboard=InlineKeyboardMarkup([]),
                )
            return

        manager: JobManager = context.application.bot_data["manager"]
        job = manager.get(parts[1])
        if not job or job.finished:
            await query.answer("Job not found or already finished.", show_alert=True)
            return
        user_id = update.effective_user.id
        if not _can_cancel(job, user_id, settings):
            await query.answer(
                "Only the job's launcher or an admin can cancel it.",
                show_alert=True,
            )
            return
        job.cancel.cancel()
        await query.answer("Cancelling…")
        return

    await query.answer()


async def _handle_inline_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    token: str,
) -> None:
    """Start an inline card from its fallback button.

    Telegram only sends ``chosen_inline_result`` when inline feedback is enabled
    in BotFather. The Start button makes inline mode work without that setting.
    """
    settings: Settings = context.application.bot_data["settings"]
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None:
        return

    inline_message_id = getattr(query, "inline_message_id", None)
    if not inline_message_id:
        await query.answer(
            "This card cannot be started. Run the inline query again.",
            show_alert=True,
        )
        return
    if not token:
        await query.answer("This button is invalid.", show_alert=True)
        return

    entry: Optional[dict] = None
    mismatch = False
    async with _inline_lock(context):
        pending = _read_inline_pending(settings)
        candidate = pending.get(token)
        if candidate is not None and candidate.get("user_id") != user.id:
            mismatch = True
        elif candidate is not None:
            entry = candidate
            pending.pop(token, None)
            _write_inline_pending(settings, pending)

    if mismatch:
        await query.answer(
            "Only the person who created this mirror can start it.",
            show_alert=True,
        )
        return

    existing = _job_for_inline_message(context, inline_message_id)
    if existing is not None:
        await query.answer("This mirror is already running.")
        await _edit_inline_card(
            context.bot,
            inline_message_id,
            render_job(existing),
            keyboard=_cancel_keyboard(existing.id),
        )
        return

    if entry is None:
        await query.answer(
            "This mirror request expired. Run the inline query again.",
            show_alert=True,
        )
        await _edit_inline_card(
            context.bot,
            inline_message_id,
            _inline_expired_text(),
            keyboard=InlineKeyboardMarkup([]),
        )
        return

    source = str(entry.get("source") or "")
    if not source:
        await query.answer("This button is invalid.", show_alert=True)
        return
    host = "pixeldrain" if entry.get("host") == "pixeldrain" else "gofile"
    dump_partitions = tuple(
        str(part) for part in entry.get("dump_partitions") or () if part
    )
    compress = bool(entry.get("compress"))

    await query.answer("Starting…")
    job = new_job(
        user_id=user.id,
        chat_id=0,
        source=source,
        host=host,
        user_name=(
            getattr(user, "full_name", "")
            or getattr(user, "first_name", "")
            or f"User {user.id}"
        ),
        inline_message_id=inline_message_id,
        dump_partitions=dump_partitions,
        compress=compress,
    )
    await _start_job(context, job, None)


REMOTE_REFRESH_SECONDS = 15


async def _monitor_remote(app: Application) -> None:
    """Clean up tracked Actions runs and finish cancellation cards."""
    remote: Optional[RemoteJobManager] = app.bot_data.get("remote")
    if remote is None:
        return

    while True:
        await asyncio.sleep(REMOTE_REFRESH_SECONDS)
        try:
            events = await asyncio.to_thread(remote.refresh)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Could not refresh remote jobs")
            continue

        for event in events:
            conclusion = event.conclusion.lower()
            record = event.record
            if conclusion == "cancelled" or (
                record.state == "cancelling" and conclusion != "success"
            ):
                logger.info("Remote job %s was cancelled.", record.job.id)
                await _deliver_remote_cancelled(app.bot, record)
            elif conclusion in REMOTE_FAILURE_CONCLUSIONS:
                logger.warning(
                    "Remote job %s failed with conclusion %s.",
                    record.job.id,
                    conclusion or "unknown",
                )
                await _deliver_remote_failure(app.bot, record)


# ------------------------------------------------------------------- lifecycle
async def track_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    lock: asyncio.Lock = context.application.bot_data["state_lock"]
    async with lock:
        last = int(context.application.bot_data.get("last_update_id", 0))
        if update.update_id <= last:
            raise ApplicationHandlerStop
        context.application.bot_data["last_update_id"] = update.update_id
        save_state(settings.state_file, {"last_update_id": update.update_id})


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled update error", exc_info=context.error)


async def post_init(app: Application) -> None:
    settings: Settings = app.bot_data["settings"]
    commands = [
        BotCommand("start", "Welcome and usage"),
        BotCommand("gf", "Upload to Gofile: /gf <link>"),
        BotCommand("mirror", "Upload to Gofile: /mirror <link>"),
        BotCommand("px", "Upload to Pixeldrain: /px <link>"),
        BotCommand("status", "Show active jobs"),
        BotCommand("cancel", "Cancel your jobs"),
        BotCommand("id", "Show your Telegram ID"),
    ]
    if settings.local_mode:
        commands.append(
            BotCommand("handoff", "Stop the PC bot and start GitHub Actions")
        )
    try:
        await app.bot.set_my_commands(commands)
    except TelegramError:
        logger.warning("Could not set bot commands", exc_info=True)


def _register(app: Application, settings: Settings, manager: JobManager) -> None:
    app.bot_data["settings"] = settings
    app.bot_data["manager"] = manager
    app.bot_data["reporters"] = set()
    app.bot_data["state_lock"] = asyncio.Lock()
    app.bot_data["accepting_jobs"] = True

    app.add_handler(TypeHandler(Update, gate_group_access), group=-2)
    app.add_handler(TypeHandler(Update, track_update), group=-1)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("gf", cmd_gf))
    app.add_handler(CommandHandler("mirror", cmd_mirror))
    app.add_handler(CommandHandler("px", cmd_px))
    app.add_handler(CommandHandler("compress", cmd_compress))
    app.add_handler(CommandHandler("compress_gf", cmd_compress))
    app.add_handler(CommandHandler("compress_px", cmd_compress))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("handoff", cmd_handoff))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(InlineQueryHandler(on_inline_query))
    app.add_handler(ChosenInlineResultHandler(on_chosen_inline_result))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_error_handler(on_error)


async def run_bot(
    settings: Settings,
    status: Optional[dict[str, bool]] = None,
    *,
    actions_repo: Optional[str] = None,
    actions_ref: Optional[str] = None,
) -> None:
    remote: Optional[RemoteJobManager] = None
    if settings.remote_jobs:
        if not actions_repo or not actions_ref:
            raise RuntimeError(
                "REMOTE_JOBS requires ACTIONS_REPO and ACTIONS_REF (or the gh CLI "
                "and a checked-out git branch)."
            )
        remote = RemoteJobManager(
            settings,
            repo=actions_repo,
            ref=actions_ref,
            state_file=settings.remote_state_file,
        )
        manager: JobManager | RemoteJobManager = remote
    else:
        manager = JobManager(settings)

    app = (
        Application.builder()
        .token(settings.telegram_token)
        .post_init(post_init)
        .build()
    )
    stop_event = asyncio.Event()
    app.bot_data["stop_event"] = stop_event
    _register(app, settings, manager)
    app.bot_data["remote"] = remote
    state = load_state(settings.state_file)
    app.bot_data["last_update_id"] = int(state.get("last_update_id", 0) or 0)

    if not settings.allowed_user_ids:
        logger.warning("ALLOWED_USER_IDS is empty: anyone who finds the bot can use it.")

    # Remove files left behind by a previous interrupted run.
    uploads = settings.download_dir / "uploads"
    if uploads.exists():
        cutoff = time.time() - 24 * 60 * 60
        for old_path in uploads.iterdir():
            try:
                if old_path.stat().st_mtime >= cutoff:
                    continue
                if old_path.is_dir():
                    shutil.rmtree(old_path, ignore_errors=True)
                else:
                    old_path.unlink(missing_ok=True)
            except OSError:
                pass

    await app.initialize()
    await app.start()
    await app.updater.start_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )
    if status is not None:
        status["polling"] = True
    logger.info(
        "Bot polling started (runner=%s, run_seconds=%s, max_file_size=%s, "
        "uploaders=Gofile%s)",
        "GitHub Actions" if remote is not None else "PC",
        settings.run_seconds or "unlimited",
        human_size(settings.max_file_size) if settings.max_file_size > 0 else "unlimited",
        ",Pixeldrain" if settings.pixeldrain_api_key else "",
    )

    monitor_task = None
    if remote is not None:
        monitor_task = asyncio.create_task(
            _monitor_remote(app), name="remote-job-monitor"
        )

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            pass

    try:
        if settings.run_seconds > 0:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=settings.run_seconds)
            except asyncio.TimeoutError:
                pass
        else:
            await stop_event.wait()
    finally:
        logger.info("Stopping bot…")
        app.bot_data["accepting_jobs"] = False

        if monitor_task is not None:
            monitor_task.cancel()
            try:
                await monitor_task
            except asyncio.CancelledError:
                pass

        if remote is not None:
            # The workers keep running on GitHub after the controller exits.
            if manager.active_jobs():
                logger.info(
                    "Leaving %d job(s) running on GitHub Actions.",
                    len(manager.active_jobs()),
                )
        else:
            # On GitHub Actions, finish jobs that were already running before
            # the process exits. The workflow has enough timeout budget for
            # this grace period. Local runs stop immediately on Ctrl+C.
            if settings.run_seconds > 0:
                drain_deadline = time.monotonic() + 20 * 60
                while manager.active_jobs() and time.monotonic() < drain_deadline:
                    await asyncio.sleep(5)
                if manager.active_jobs():
                    logger.warning(
                        "Grace period ended with active jobs; cancelling them."
                    )

            for job in manager.active_jobs():
                job.cancel.cancel()

        try:
            await app.updater.stop()
        except TelegramError:
            pass
        try:
            await app.stop()
        except TelegramError:
            pass
        await app.shutdown()


def main() -> None:
    settings = load_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram.ext.Application").setLevel(logging.INFO)

    if settings.remote_jobs:
        if not settings.remote_job_key:
            logger.error(
                "REMOTE_JOBS is enabled but REMOTE_JOB_KEY is not set. "
                "Generate one and set the same value as a GitHub Actions secret."
            )
            raise SystemExit(1)
        logger.info("Remote mode: mirror jobs run on GitHub Actions.")

    repo: Optional[str] = None
    ref: Optional[str] = None
    if settings.local_mode or settings.remote_jobs:
        if "replace-me" in settings.telegram_token:
            logger.error(
                "TELEGRAM_BOT_TOKEN is still the placeholder from .env.example. "
                "Edit .env before starting the PC runner."
            )
            raise SystemExit(1)
        try:
            repo = resolve_repo(settings.actions_repo)
            ref = settings.actions_ref or current_branch()
            if settings.local_mode:
                logger.info("Local mode: checking GitHub Actions (%s, %s).", repo, ref)
                take_over(repo, branch=ref)
                sync_offset_from_origin(settings.state_file, ref)
        except HandoffError as exc:
            logger.error("Cannot start the local bot: %s", exc)
            raise SystemExit(1) from exc

    status = {"polling": False}
    try:
        asyncio.run(
            run_bot(
                settings,
                status,
                actions_repo=repo,
                actions_ref=ref,
            )
        )
    except KeyboardInterrupt:
        pass
    finally:
        if settings.local_mode and settings.auto_handoff and status["polling"]:
            if repo is None or ref is None:
                logger.error("Cannot start GitHub Actions: repository is unknown.")
            else:
                try:
                    dispatch_handoff(repo, ref, read_offset(settings.state_file))
                except HandoffError as exc:
                    logger.error("Could not start the GitHub Actions fallback: %s", exc)
                except Exception:
                    logger.exception(
                        "Unexpected error while starting the GitHub Actions fallback"
                    )


if __name__ == "__main__":
    main()
