import type { JobRecord } from "./types";
import { htmlEscape, humanEta, humanSize } from "./utils";

export interface InlineKeyboard {
  inline_keyboard: Array<Array<{ text: string; callback_data: string }>>;
}

export function cancelKeyboard(jobId: string): InlineKeyboard {
  return {
    inline_keyboard: [[{ text: "Cancel job", callback_data: `cancel:${jobId}` }]],
  };
}

/**
 * Telegram only returns inline_message_id for chosen inline results whose
 * message has an inline keyboard. Attach this Start button so the card is
 * editable even when inline feedback is disabled; once the job exists it is
 * replaced with the real Cancel button.
 */
export function pendingKeyboard(token: string): InlineKeyboard {
  return {
    inline_keyboard: [
      [{ text: "▶️ Start mirroring", callback_data: `inline:start:${token}` }],
    ],
  };
}

export function emptyKeyboard(): InlineKeyboard {
  return { inline_keyboard: [] };
}

function launcherHtml(job: JobRecord): string {
  const name = job.userName || `User ${job.userId}`;
  return (
    `👤 Launched by <a href="tg://user?id=${job.userId}">` +
    `${htmlEscape(name)}</a>`
  );
}

function durationText(job: JobRecord, now: number): string {
  if (!job.startedAt) return "";
  const finished = job.finishedAt || now;
  return humanEta(Math.max(0, finished - job.startedAt));
}

function stateDetail(job: JobRecord): string {
  if (job.detail) return job.detail;
  if (job.state === "dispatching") return "Starting a GitHub Actions runner…";
  if (job.state === "queued") return "Waiting for a GitHub Actions runner…";
  if (job.state === "cancelling") return "Cancelling on GitHub Actions…";
  return "GitHub Actions";
}

export function renderJob(job: JobRecord, now = Math.floor(Date.now() / 1000)): string {
  const lines = [`☁️ <b>GitHub Actions</b> · <code>${job.id}</code>`];
  lines.push(`<i>${htmlEscape(stateDetail(job))}</i>`);
  lines.push(launcherHtml(job));

  const footer = [`📦 ${job.host === "pixeldrain" ? "Pixeldrain" : "Gofile"}`];
  if (job.fileSize) footer.push(`💾 ${humanSize(job.fileSize)}`);
  if (job.startedAt && now > job.startedAt) {
    footer.push(`⏱ ${humanEta(now - job.startedAt)}`);
  }
  lines.push(footer.join(" · "));
  return lines.join("\n");
}

export function renderCancelled(
  job: JobRecord,
  now = Math.floor(Date.now() / 1000),
): string {
  const lines = [`⏹️ <b>Job cancelled</b> · <code>${job.id}</code>`];
  const duration = durationText(job, now);
  if (duration) lines.push(`⏱ Stopped after ${duration}`);
  lines.push(launcherHtml(job));
  return lines.join("\n");
}

export function renderFailed(
  job: JobRecord,
  runUrl = "",
  now = Math.floor(Date.now() / 1000),
): string {
  const reason = job.error || "Unknown error.";
  const lines = [
    `❌ <b>Job failed</b> · <code>${job.id}</code>`,
    `<b>Reason:</b> ${htmlEscape(reason)}`,
  ];
  const duration = durationText(job, now);
  if (duration) lines.push(`⏱ Failed after ${duration}`);
  lines.push(launcherHtml(job));
  if (runUrl) {
    const url = htmlEscape(runUrl);
    lines.push("", `🔗 <a href="${url}">Actions run</a>`);
  }
  return lines.join("\n");
}

export function renderStatus(
  jobs: JobRecord[],
  now = Math.floor(Date.now() / 1000),
): string {
  if (jobs.length === 0) return "No active jobs.";
  const lines = ["<b>Active jobs</b>"];
  for (const job of jobs.slice(0, 10)) {
    lines.push(`☁️ <code>${job.id}</code> · <b>GitHub Actions</b>`);
    const detail = stateDetail(job);
    if (detail) lines.push(`<i>${htmlEscape(detail)}</i>`);
  }
  return lines.join("\n");
}

export function renderStart(botUsername = ""): string {
  const inlineHandle = botUsername ? `@${botUsername}` : "@YourBot";
  return (
    "<b>Mirror bot</b>\n\n"
    + "Send me a link or a magnet link. This Cloudflare Worker only handles "
    + "Telegram commands. A GitHub Actions worker downloads it and uploads it "
    + "to Gofile or Pixeldrain, then replies with the public link.\n\n"
    + "<b>Inline mode</b>\n"
    + `Type <code>${htmlEscape(inlineHandle)} /gf &lt;link&gt;</code> in any chat. `
    + "The bot posts an inline card there and updates it with progress and the "
    + "final link. Inline mode only accepts direct links, not replied files.\n\n"
    + "To mirror a file or GIF that is already in the chat, reply to it with "
    + "<code>/gf</code> or <code>/px</code>. Other attachments are ignored. "
    + "Telegram bots can download replied attachments up to 20 MB; send a link "
    + "for anything larger.\n\n"
    + "Any file type, Gofile share links, pages with a download button, video "
    + "sites, image galleries, and public MEGA links are supported.\n\n"
    + "<b>Commands</b>\n"
    + "/gf &lt;link&gt; — upload to Gofile, or reply to a file with /gf\n"
    + "/mirror &lt;link&gt; — same as /gf\n"
    + "/px &lt;link&gt; — upload to Pixeldrain, or reply to a file with /px "
    + "(requires a free Pixeldrain API key)\n"
    + "/status — active jobs\n"
    + "/cancel — cancel your jobs\n"
    + "/id — show your Telegram ID\n\n"
    + "<b>Android partition dump</b>\n"
    + "/gf &lt;link&gt; -dump boot,vendor — download an OTA zip, fastboot ROM, "
    + "or 7z/RAR archive, extract only those partition images from payload.bin "
    + "or super.img, and upload them. Use <code>-dump</code> with spaces too: "
    + "<code>-dump boot vendor</code>."
  );
}

export function renderUsage(command: string): string {
  return (
    `Usage: <code>/${command} &lt;link&gt; [-dump boot,vendor]</code>\n` +
    `Or reply to a file or link with <code>/${command} [-dump boot,vendor]</code>.`
  );
}

export function renderDumpUsage(command: string): string {
  return (
    "Add partition names after <code>-dump</code>, e.g. " +
    `<code>/${command} &lt;link&gt; -dump boot,vendor</code>.`
  );
}

export function renderInlinePending(
  host: "gofile" | "pixeldrain",
  dumpPartitions: string[] = [],
): string {
  const lines = [
    "☁️ <b>GitHub Actions</b>",
    "<i>Preparing the mirror job…</i>",
    `📦 ${host === "pixeldrain" ? "Pixeldrain" : "Gofile"}`,
  ];
  if (dumpPartitions.length > 0) {
    lines.push(`🧩 ${htmlEscape(dumpPartitions.join(", "))}`);
  }
  lines.push("", "Tap <b>▶️ Start mirroring</b> if it does not start automatically.");
  return lines.join("\n");
}

export function renderInlineExpired(): string {
  return (
    "⌛ <b>This mirror request expired.</b>\n"
    + "Run the inline query again to start a new job."
  );
}

export function renderInlineUsage(): string {
  return (
    "Usage: <code>/gf &lt;link&gt; [-dump boot,vendor]</code>\n"
    + "Inline mode can only mirror a direct link. Reply-to-file is not "
    + "available in inline mode."
  );
}

export function renderDenied(userId: number): string {
  return (
    "This bot is private.\n"
    + `Your Telegram ID is <code>${userId}</code> — add it to ALLOWED_USER_IDS to get access.`
  );
}
