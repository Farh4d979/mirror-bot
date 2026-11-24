"""Shared Telegram progress and result cards."""

from __future__ import annotations

import html
import time

from .jobs import Job
from .utils import human_eta, human_size, progress_bar

STAGE_STYLES = {
    "queued": ("🕒", "Queued"),
    "resolving": ("🔎", "Resolving link"),
    "downloading": ("⬇️", "Downloading"),
    "extracting": ("🧩", "Extracting partitions"),
    "uploading": ("⬆️", "Uploading"),
    "remote": ("☁️", "GitHub Actions"),
    "done": ("✅", "Done"),
    "failed": ("❌", "Failed"),
    "cancelled": ("⏹️", "Cancelled"),
}


def _launcher_html(snap: dict) -> str:
    """Tag the job's launcher with a Telegram mention."""
    name = str(snap.get("user_name") or f"User {snap['user_id']}")
    return (
        f'👤 Launched by <a href="tg://user?id={int(snap["user_id"])}">'
        f"{html.escape(name)}</a>"
    )


def _duration_text(snap: dict) -> str:
    started = snap.get("started_at")
    if not started:
        return ""
    finished = snap.get("finished_at") or time.time()
    return human_eta(max(0.0, finished - started))


def render_job(job: Job) -> str:
    snap = job.snapshot()
    icon, title = STAGE_STYLES.get(snap["stage"], ("⚙️", snap["stage"].title()))
    lines = [f"{icon} <b>{title}</b> · <code>{snap['id']}</code>"]

    if snap["status"] == "queued":
        lines.append("<i>Waiting for a free worker…</i>")

    percent = snap["percent"]
    if percent is not None:
        lines.append(
            f"<code>{progress_bar(percent, width=16)}</code> <b>{percent:.0f}%</b>"
        )

    if snap["detail"]:
        lines.append(f"<i>{html.escape(str(snap['detail']))}</i>")

    if snap["extras"]:
        lines.append(
            f"<i>➕ {snap['extras']} extra file(s) skipped — largest is uploaded.</i>"
        )

    lines.append(_launcher_html(snap))

    footer = [f"📦 {snap['host'].title()}"]
    if snap["file_size"]:
        footer.append(f"💾 {human_size(snap['file_size'])}")
    if snap["status"] == "running" and snap["started_at"]:
        footer.append(f"⏱ {human_eta(max(0.0, time.time() - snap['started_at']))}")
    lines.append(" · ".join(footer))
    return "\n".join(lines)


def render_final(job: Job) -> str:
    snap = job.snapshot()
    duration = _duration_text(snap)
    if snap["status"] == "done" and snap["result"]:
        results = list(snap["results"]) or [snap["result"]]
        if snap["dump_partitions"]:
            requested = ", ".join(snap["dump_partitions"])
            lines = [
                f"✅ <b>Partition dump complete</b> · <code>{snap['id']}</code>",
                "",
                f"🗂 Requested: <code>{html.escape(requested)}</code>",
                f"📦 {html.escape(results[0].host)} · 💾 {human_size(snap['file_size'])}",
            ]
            if duration:
                lines.append(f"⏱ Took {duration}")
            lines.append(_launcher_html(snap))
            if snap["missing_partitions"]:
                missing = ", ".join(snap["missing_partitions"])
                lines.append(f"⚠️ <b>Not found:</b> <code>{html.escape(missing)}</code>")
            for result in results:
                name = html.escape(result.file_name or "image")
                size = human_size(result.size) if result.size else "?"
                url = html.escape(result.url, quote=True)
                lines.extend(
                    [
                        "",
                        f"📄 <b>{name}</b> · 💾 {size}",
                        f'🔗 <a href="{url}">{url}</a>',
                    ]
                )
            return "\n".join(lines)

        result = snap["result"]
        lines = [
            f"✅ <b>Job complete</b> · <code>{snap['id']}</code>",
            "",
            f"📄 {html.escape(snap['file_name'] or 'file')}",
            f"💾 {human_size(snap['file_size'])} · 📦 {html.escape(result.host)}",
        ]
        if duration:
            lines.append(f"⏱ Took {duration}")
        lines.append(_launcher_html(snap))
        if snap["extras"]:
            lines.append(f"<i>➕ {snap['extras']} extra file(s) were skipped.</i>")
        url = html.escape(result.url, quote=True)
        lines.extend(["", f'🔗 <a href="{url}">{url}</a>'])
        return "\n".join(lines)

    if snap["status"] == "cancelled":
        lines = [f"⏹️ <b>Job cancelled</b> · <code>{snap['id']}</code>"]
        if duration:
            lines.append(f"⏱ Stopped after {duration}")
        lines.append(_launcher_html(snap))
        return "\n".join(lines)

    reason = snap["error"] or "Unknown error."
    lines = [
        f"❌ <b>Job failed</b> · <code>{snap['id']}</code>",
        f"<b>Reason:</b> {html.escape(str(reason))}",
    ]
    if duration:
        lines.append(f"⏱ Failed after {duration}")
    lines.append(_launcher_html(snap))
    return "\n".join(lines)
