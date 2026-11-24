/** Pure helpers ported from the Python bot. */

import type { TgFile, TgMessage } from "./types";

const URL_RE = /(?:https?:\/\/|magnet:\?)[^\s<>"']+/i;
const ILLEGAL_FILENAME = /[<>:"/\\|?*\x00-\x1f]/g;
const RESERVED_NAMES = new Set([
  "CON",
  "PRN",
  "AUX",
  "NUL",
  ...Array.from({ length: 9 }, (_, i) => `COM${i + 1}`),
  ...Array.from({ length: 9 }, (_, i) => `LPT${i + 1}`),
]);
const IMG_SUFFIX_RE = /\.img$/i;
const SPLIT_RE = /[\s,]+/;
const DUMP_ARG_RE = /^--?dump(?:=(.*))?$/i;
const PARTITION_NAME_RE = /^[a-z0-9][a-z0-9._-]{0,63}$/;

const MIME_EXTENSIONS: Record<string, string> = {
  "image/jpeg": ".jpg",
  "image/jpg": ".jpg",
  "image/png": ".png",
  "image/gif": ".gif",
  "image/webp": ".webp",
  "image/bmp": ".bmp",
  "image/svg+xml": ".svg",
  "video/mp4": ".mp4",
  "video/webm": ".webm",
  "video/quicktime": ".mov",
  "video/x-matroska": ".mkv",
  "audio/mpeg": ".mp3",
  "audio/mp4": ".m4a",
  "audio/x-m4a": ".m4a",
  "audio/ogg": ".ogg",
  "audio/opus": ".opus",
  "audio/wav": ".wav",
  "audio/x-wav": ".wav",
  "audio/flac": ".flac",
  "application/pdf": ".pdf",
  "application/zip": ".zip",
  "application/x-7z-compressed": ".7z",
  "application/x-rar-compressed": ".rar",
  "application/gzip": ".gz",
  "application/x-tar": ".tar",
  "application/x-bittorrent": ".torrent",
  "application/epub+zip": ".epub",
  "application/vnd.android.package-archive": ".apk",
  "application/json": ".json",
  "text/plain": ".txt",
  "text/html": ".html",
};

export class DumpError extends Error {}

export function extractUrl(text: string | undefined | null): string | null {
  if (!text) return null;
  const match = URL_RE.exec(text);
  if (!match) return null;
  return match[0].replace(/[).,;!?]+$/, "");
}

export function isMagnet(link: string): boolean {
  return /^magnet:\?[^\s]+$/i.test((link || "").trim());
}

export function humanSize(size: number | null | undefined): string {
  if (size === null || size === undefined || !Number.isFinite(size) || size < 0) {
    return "?";
  }
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = Number(size);
  for (let index = 0; index < units.length; index += 1) {
    const unit = units[index];
    if (value < 1024 || index === units.length - 1) {
      if (unit === "B") return `${Math.trunc(value)} B`;
      return `${value.toFixed(1)} ${unit}`;
    }
    value /= 1024;
  }
  return `${value.toFixed(1)} TB`;
}

export function humanEta(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || seconds < 0 || !Number.isFinite(seconds)) {
    return "";
  }
  const total = Math.trunc(seconds);
  if (total < 60) return `${total}s`;
  const minutes = Math.floor(total / 60);
  const rest = total % 60;
  if (minutes < 60) return `${minutes}m ${String(rest).padStart(2, "0")}s`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${String(minutes % 60).padStart(2, "0")}m`;
}

export function htmlEscape(value: unknown, quote = true): string {
  let text = String(value ?? "");
  text = text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  if (quote) text = text.replace(/"/g, "&quot;");
  return text;
}

export function sanitizeFilename(
  name: string,
  fallback = "download.bin",
  maxLength = 180,
): string {
  let value = String(name || "").trim();
  try {
    value = decodeURIComponent(value);
  } catch {
    // Keep the original text when it contains a malformed percent escape.
  }
  value = value.normalize("NFKC");
  value = value.replace(ILLEGAL_FILENAME, "_");
  value = value.replace(/\s+/g, " ").replace(/^[ .]+|[ .]+$/g, "");
  if (!value || value === "." || value === "..") value = fallback;

  const dot = value.lastIndexOf(".");
  let stem = dot > 0 ? value.slice(0, dot) : dot === 0 ? "" : value;
  let suffix = dot >= 0 ? value.slice(dot + 1) : "";
  if (dot < 0) {
    stem = value;
    suffix = "";
  }
  stem = stem.replace(/^[ .]+|[ .]+$/g, "") || "file";
  if (RESERVED_NAMES.has(stem.toUpperCase())) stem = `_${stem}`;
  if (suffix.length > 16) suffix = "";
  const keep = Math.max(1, maxLength - suffix.length - (suffix ? 1 : 0));
  stem = stem.slice(0, keep);
  const result = suffix ? `${stem}.${suffix}` : stem;
  return result || fallback;
}

export function guessExtension(mimeType: string | undefined | null): string | null {
  const mime = String(mimeType || "").split(";")[0].trim().toLowerCase();
  return MIME_EXTENSIONS[mime] || null;
}

function attachmentKind(message: TgMessage): string {
  if (message.photo) return "photo";
  if (message.video) return "video";
  if (message.audio) return "audio";
  if (message.voice) return "voice";
  if (message.video_note) return "video_note";
  if (message.animation) return "animation";
  if (message.sticker) return "sticker";
  return "file";
}

export function attachmentFilename(message: TgMessage, attachment: TgFile): string {
  if (attachment.file_name) return sanitizeFilename(attachment.file_name, "file.bin");

  let extension = guessExtension(attachment.mime_type);
  if (extension === ".jpe") extension = ".jpg";
  const unique = attachment.file_unique_id || Math.random().toString(16).slice(2, 10);
  let prefix = attachmentKind(message);

  if (prefix === "photo") {
    extension = extension || ".jpg";
  } else if (prefix === "voice") {
    extension = ".ogg";
  } else if (prefix === "sticker") {
    if (message.sticker?.is_animated) extension = ".tgs";
    else if (message.sticker?.is_video) extension = ".webm";
    else extension = extension || ".webp";
  } else {
    extension =
      extension ||
      (prefix === "video" || prefix === "video_note" || prefix === "animation"
        ? ".mp4"
        : prefix === "audio"
          ? ".mp3"
          : ".bin");
  }

  return sanitizeFilename(`${prefix}_${unique}${extension}`, `${prefix}${extension || ".bin"}`);
}

/** Return the file-like attachment of a Telegram message, if any. */
export function messageAttachment(message: TgMessage | undefined): TgFile | null {
  if (!message) return null;
  if (message.photo && message.photo.length > 0) {
    return message.photo.reduce((best, photo) => {
      const bestScore = best.file_size || (best.width || 0) * (best.height || 0);
      const score = photo.file_size || (photo.width || 0) * (photo.height || 0);
      return score > bestScore ? photo : best;
    });
  }
  for (const attribute of [
    "document",
    "video",
    "audio",
    "voice",
    "video_note",
    "animation",
    "sticker",
  ] as const) {
    const attachment = message[attribute];
    if (attachment) return attachment;
  }
  return null;
}

export function parseDumpArgs(args: string[]): {
  remaining: string[];
  values: string[];
  requested: boolean;
} {
  const remaining: string[] = [];
  const values: string[] = [];
  let requested = false;
  let collecting = false;

  for (const raw of args) {
    const token = String(raw || "").trim();
    if (!token) continue;
    const match = DUMP_ARG_RE.exec(token);
    if (match) {
      requested = true;
      collecting = true;
      if (match[1]) values.push(match[1]);
      continue;
    }
    if (collecting) {
      if (token.startsWith("-")) {
        collecting = false;
        remaining.push(token);
      } else {
        values.push(token);
      }
    } else {
      remaining.push(token);
    }
  }

  return { remaining, values, requested };
}

export function normalizePartitions(values: string[]): string[] {
  const names: string[] = [];
  for (const value of values) {
    for (const rawPiece of String(value || "").split(SPLIT_RE)) {
      const piece = rawPiece.trim().replace(IMG_SUFFIX_RE, "").toLowerCase();
      if (!piece) continue;
      if (!PARTITION_NAME_RE.test(piece)) {
        throw new DumpError(
          `\`${piece}\` is not a valid partition name. Example: ` +
            "`/gf <link> -dump boot,vendor`.",
        );
      }
      if (!names.includes(piece)) names.push(piece);
    }
  }
  return names;
}

export function parseSize(value: string | number | null | undefined, fallback: number): number {
  if (value === null || value === undefined || value === "") return fallback;
  if (typeof value === "number") return Number.isFinite(value) ? value : fallback;
  const text = String(value).trim().toUpperCase().replace(/IB/g, "B");
  const match = /^(\d+(?:\.\d+)?)\s*([KMGT]?B?)$/.exec(text);
  if (!match) return fallback;
  const number = Number(match[1]);
  const unit = match[2].replace(/B$/, "") || "B";
  const factor =
    ({ "": 1, K: 1024, M: 1024 ** 2, G: 1024 ** 3, T: 1024 ** 4 } as Record<string, number>)[
      unit
    ] || 1;
  return Math.trunc(number * factor);
}

export function fullName(user: {
  first_name?: string;
  last_name?: string;
  username?: string;
  id: number;
}): string {
  const name = [user.first_name, user.last_name].filter(Boolean).join(" ").trim();
  return name || user.username || `User ${user.id}`;
}

export function chatArgs(text: string, commandLength: number): string[] {
  return text
    .slice(commandLength)
    .trim()
    .split(/\s+/)
    .filter(Boolean);
}
