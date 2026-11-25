import type { Config } from "./config";
import { encryptPayload } from "./fernet";
import type { RunInfo } from "./github";
import { buildPayload } from "./payload";
import type { InlineKeyboard } from "./render";
import {
  cancelKeyboard,
  emptyKeyboard,
  renderCancelled,
  renderDumpUsage,
  renderFailed,
  renderInlinePending,
  renderInlineUsage,
  renderJob,
  renderStart,
  renderStatus,
  renderUsage,
  renderDenied,
} from "./render";
import type { StoreLike } from "./store";
import type { DeleteResult } from "./telegram";
import type {
  JobRecord,
  NewJob,
  TgCallbackQuery,
  TgChosenInlineResult,
  TgFile,
  TgInlineQuery,
  TgMessage,
  TgUpdate,
  TgUser,
} from "./types";
import {
  DumpError,
  attachmentFilename,
  extractUrl,
  fullName,
  htmlEscape,
  humanSize,
  messageAttachment,
  normalizePartitions,
  parseDumpArgs,
  sanitizeFilename,
} from "./utils";

export const TELEGRAM_FILE_LIMIT = 20 * 1024 * 1024;
const GROUP_ACCESS_TTL = 5 * 60;
const botUsernameCache = new Map<string, string>();

export interface TelegramLike {
  sendMessage(
    chatId: number,
    text: string,
    replyMarkup?: InlineKeyboard,
    replyToMessageId?: number,
  ): Promise<{ message_id: number }>;
  editMessageText(
    chatId: number,
    messageId: number,
    text: string,
    replyMarkup?: InlineKeyboard,
  ): Promise<void>;
  editInlineMessageText(
    inlineMessageId: string,
    text: string,
    replyMarkup?: InlineKeyboard,
  ): Promise<void>;
  deleteMessage(chatId: number, messageId: number): Promise<DeleteResult>;
  answerCallbackQuery(
    callbackQueryId: string,
    text?: string,
    showAlert?: boolean,
  ): Promise<void>;
  answerInlineQuery(
    inlineQueryId: string,
    results: Array<Record<string, unknown>>,
    options?: { cacheTime?: number; isPersonal?: boolean },
  ): Promise<void>;
  getChatMember(chatId: number, userId: number): Promise<{ status: string }>;
  getMe(): Promise<{ id: number; username?: string }>;
}

export interface GitHubLike {
  dispatch(payload: string, jobId: string): Promise<{ runId: number; runUrl: string }>;
  cancel(runId: number): Promise<boolean>;
  view(runId: number): Promise<RunInfo>;
}

export interface HandlerDeps {
  config: Config;
  store: StoreLike;
  tg: TelegramLike;
  gh: GitHubLike;
  now?: () => number;
  newId?: () => string;
}

function nowSeconds(deps: HandlerDeps): number {
  return deps.now ? deps.now() : Math.floor(Date.now() / 1000);
}

function randomJobId(): string {
  return crypto.randomUUID().replace(/-/g, "").slice(0, 8);
}

function isGroup(chatType: string): boolean {
  return chatType === "group" || chatType === "supergroup";
}

async function groupAllowed(
  deps: HandlerDeps,
  chatId: number,
): Promise<boolean> {
  if (deps.config.allowedUserIds.size === 0) return true;

  const now = nowSeconds(deps);
  const cached = await deps.store.getGroup(chatId);
  if (cached && now - cached.checkedAt < GROUP_ACCESS_TTL) return cached.allowed;

  let allowed = false;
  for (const ownerId of deps.config.allowedUserIds) {
    try {
      const member = await deps.tg.getChatMember(chatId, ownerId);
      if (member.status !== "left" && member.status !== "kicked") {
        allowed = true;
        break;
      }
    } catch {
      // Try the next configured owner.
    }
  }
  await deps.store.setGroup(chatId, allowed, now);
  return allowed;
}

function privateAuthorized(deps: HandlerDeps, userId: number): boolean {
  return deps.config.allowedUserIds.size === 0 || deps.config.allowedUserIds.has(userId);
}

function canCancel(deps: HandlerDeps, job: JobRecord, userId: number): boolean {
  return job.userId === userId || deps.config.allowedUserIds.has(userId);
}

async function botUsername(deps: HandlerDeps): Promise<string> {
  if (deps.config.botUsername) return deps.config.botUsername;
  const cached = botUsernameCache.get(deps.config.telegramToken);
  if (cached !== undefined) return cached;
  try {
    const me = await deps.tg.getMe();
    const username = String(me.username || "").replace(/^@/, "");
    if (username) botUsernameCache.set(deps.config.telegramToken, username);
    return username;
  } catch {
    return "";
  }
}

async function commandTargetsBot(
  deps: HandlerDeps,
  mention: string | undefined,
): Promise<boolean> {
  if (!mention) return true;
  const username = await botUsername(deps);
  if (!username) return true;
  return mention.toLowerCase() === username.toLowerCase();
}

async function reply(
  deps: HandlerDeps,
  message: TgMessage,
  text: string,
  keyboard?: InlineKeyboard,
): Promise<void> {
  try {
    await deps.tg.sendMessage(message.chat.id, text, keyboard, message.message_id);
  } catch (error) {
    console.log(
      JSON.stringify({
        level: "warn",
        msg: "Could not send Telegram reply",
        chat_id: message.chat.id,
        error: error instanceof Error ? error.message : String(error),
      }),
    );
    // A reply failure should not retry the whole Telegram update forever.
  }
}

async function deny(deps: HandlerDeps, message: TgMessage, userId: number): Promise<void> {
  console.log(
    JSON.stringify({
      level: "warn",
      msg: "Denied user",
      user_id: userId,
      chat_id: message.chat.id,
    }),
  );
  await reply(deps, message, renderDenied(userId));
}

/** Edit a job card whether it lives in a chat or an inline message. */
async function editJobCard(
  deps: HandlerDeps,
  job: JobRecord,
  text: string,
  keyboard?: InlineKeyboard,
): Promise<void> {
  if (job.inlineMessageId) {
    await deps.tg.editInlineMessageText(job.inlineMessageId, text, keyboard);
    return;
  }
  await deps.tg.editMessageText(job.chatId, job.messageId, text, keyboard);
}

export async function handleUpdate(deps: HandlerDeps, update: TgUpdate): Promise<void> {
  if (update.callback_query) {
    await handleCallback(deps, update.callback_query);
    return;
  }
  if (update.message) {
    await handleMessage(deps, update.message, update.update_id);
    return;
  }
  if (update.inline_query) {
    await handleInlineQuery(deps, update.inline_query);
    return;
  }
  if (update.chosen_inline_result) {
    await handleChosenInlineResult(
      deps,
      update.chosen_inline_result,
      update.update_id,
    );
  }
}

async function handleMessage(
  deps: HandlerDeps,
  message: TgMessage,
  updateId: number,
): Promise<void> {
  const from = message.from;
  if (!from || from.is_bot) return;
  const chat = message.chat;
  if (chat.type === "channel") return;

  if (isGroup(chat.type)) {
    if (!(await groupAllowed(deps, chat.id))) return;
  } else if (chat.type === "private") {
    if (!privateAuthorized(deps, from.id)) {
      await deny(deps, message, from.id);
      return;
    }
  } else {
    return;
  }

  const text = String(message.text || "");
  if (text.startsWith("/")) {
    const match = /^\/([A-Za-z0-9_]+)(?:@([A-Za-z0-9_]+))?(?:\s+([\s\S]*))?$/.exec(
      text,
    );
    if (!match) return;
    const command = match[1].toLowerCase();
    const mention = match[2];
    const rest = match[3] || "";
    if (!(await commandTargetsBot(deps, mention))) return;
    const args = rest.trim() ? rest.trim().split(/\s+/).filter(Boolean) : [];

    if (command === "start" || command === "help") {
      await reply(deps, message, renderStart(await botUsername(deps)));
      return;
    }
    if (command === "id") {
      await reply(
        deps,
        message,
        `User ID: <code>${from.id}</code>\nChat ID: <code>${chat.id}</code>`,
      );
      return;
    }
    if (command === "status") {
      const jobs = await deps.store.listForUser(from.id);
      await reply(deps, message, renderStatus(jobs, nowSeconds(deps)));
      return;
    }
    if (command === "cancel") {
      await cancelAllForUser(deps, message, from.id);
      return;
    }
    if (command === "gf" || command === "mirror" || command === "px") {
      await handleLinkCommand(
        deps,
        message,
        command,
        command === "px" ? "pixeldrain" : "gofile",
        args,
        updateId,
      );
      return;
    }
    return;
  }

  // Attachments and other non-text messages are ignored unless a /gf or /px
  // command replies to them.
  if (!text) return;
  // Private chats may send a bare link. Groups only react to commands.
  if (chat.type !== "private") return;
  const source = extractUrl(message.text);
  if (source) {
    await submitJob(deps, {
      updateId,
      message,
      from,
      source,
      sourceKind: "url",
      host: "gofile",
      dumpPartitions: [],
    });
    return;
  }
  await reply(
    deps,
    message,
    "Send me a link or a magnet link. To mirror a file or GIF, reply to it with /gf or /px.",
  );
}

type InlineHost = "gofile" | "pixeldrain";

type InlineParse =
  | {
      ok: true;
      command: string;
      host: InlineHost;
      source: string;
      dumpPartitions: string[];
    }
  | {
      ok: false;
      reason: "usage" | "dump_usage" | "invalid";
      host: InlineHost;
      message?: string;
    };

/** Parse the text after "@bot" in an inline query. */
function parseInlineQuery(text: string): InlineParse {
  const tokens = String(text || "").trim().split(/\s+/).filter(Boolean);
  let command = "gf";
  let host: InlineHost = "gofile";
  let args = tokens;

  if (tokens.length > 0 && tokens[0].startsWith("/")) {
    const match = /^\/([A-Za-z0-9_]+)(?:@[A-Za-z0-9_]+)?$/.exec(tokens[0]);
    if (!match) return { ok: false, reason: "usage", host };
    command = match[1].toLowerCase();
    if (command !== "gf" && command !== "mirror" && command !== "px") {
      return { ok: false, reason: "usage", host };
    }
    host = command === "px" ? "pixeldrain" : "gofile";
    args = tokens.slice(1);
  }

  let source: string | null = null;
  let sourceIndex = -1;
  for (let index = 0; index < args.length; index += 1) {
    const found = extractUrl(args[index]);
    if (found) {
      source = found;
      sourceIndex = index;
      break;
    }
  }
  if (!source) return { ok: false, reason: "usage", host };

  const remaining = args.filter((_, index) => index !== sourceIndex);
  const { values, requested } = parseDumpArgs(remaining);
  let dumpPartitions: string[];
  try {
    dumpPartitions = normalizePartitions(values);
  } catch (error) {
    return {
      ok: false,
      reason: "invalid",
      host,
      message: error instanceof DumpError ? error.message : String(error),
    };
  }
  if (requested && dumpPartitions.length === 0) {
    return { ok: false, reason: "dump_usage", host };
  }
  return { ok: true, command, host, source, dumpPartitions };
}

function inlineResult(
  id: string,
  title: string,
  description: string,
  text: string,
): Record<string, unknown> {
  return {
    type: "article",
    id,
    title,
    description: description.slice(0, 200),
    input_message_content: {
      message_text: text,
      parse_mode: "HTML",
      disable_web_page_preview: true,
    },
  };
}

function inlineErrorText(parsed: Extract<InlineParse, { ok: false }>): string {
  if (parsed.reason === "invalid") {
    return htmlEscape(parsed.message || "Invalid dump partition.");
  }
  if (parsed.reason === "dump_usage") return renderDumpUsage("gf");
  return renderInlineUsage();
}

async function handleInlineQuery(
  deps: HandlerDeps,
  query: TgInlineQuery,
): Promise<void> {
  const from = query.from;
  if (!from || from.is_bot) return;

  if (!privateAuthorized(deps, from.id)) {
    await deps.tg.answerInlineQuery(
      query.id,
      [
        inlineResult(
          "denied",
          "This bot is private",
          "Tap to see your Telegram ID",
          renderDenied(from.id),
        ),
      ],
      { cacheTime: 0, isPersonal: true },
    );
    return;
  }

  const parsed = parseInlineQuery(query.query);
  if (!parsed.ok) {
    await deps.tg.answerInlineQuery(
      query.id,
      [
        inlineResult(
          "usage",
          "Usage: /gf <link>",
          "Example: /gf https://example.com/file.zip",
          inlineErrorText(parsed),
        ),
      ],
      { cacheTime: 0, isPersonal: true },
    );
    return;
  }

  console.log(
    JSON.stringify({
      level: "info",
      msg: "Inline query",
      user_id: from.id,
      host: parsed.host,
      dump_partitions: parsed.dumpPartitions.length,
    }),
  );
  const title =
    parsed.host === "pixeldrain" ? "Mirror to Pixeldrain" : "Mirror to Gofile";
  const description = parsed.dumpPartitions.length
    ? `${parsed.source} · dump ${parsed.dumpPartitions.join(", ")}`
    : parsed.source;
  await deps.tg.answerInlineQuery(
    query.id,
    [
      inlineResult(
        parsed.host,
        title,
        description,
        renderInlinePending(parsed.host, parsed.dumpPartitions),
      ),
    ],
    { cacheTime: 0, isPersonal: true },
  );
}

async function handleChosenInlineResult(
  deps: HandlerDeps,
  chosen: TgChosenInlineResult,
  updateId: number,
): Promise<void> {
  const from = chosen.from;
  const inlineMessageId = String(chosen.inline_message_id || "");
  if (!from || from.is_bot) return;
  if (!inlineMessageId) {
    console.log(
      JSON.stringify({
        level: "warn",
        msg: "Chosen inline result has no inline_message_id; enable inline feedback in BotFather",
        user_id: from.id,
      }),
    );
    return;
  }

  if (!privateAuthorized(deps, from.id)) {
    try {
      await deps.tg.editInlineMessageText(
        inlineMessageId,
        renderDenied(from.id),
        emptyKeyboard(),
      );
    } catch {
      // Nothing else can be delivered without a chat id.
    }
    return;
  }

  const parsed = parseInlineQuery(chosen.query);
  if (!parsed.ok) {
    try {
      await deps.tg.editInlineMessageText(
        inlineMessageId,
        inlineErrorText(parsed),
        emptyKeyboard(),
      );
    } catch {
      // Nothing else can be delivered without a chat id.
    }
    return;
  }

  await submitInlineJob(deps, {
    updateId,
    from,
    source: parsed.source,
    host: parsed.host,
    dumpPartitions: parsed.dumpPartitions,
    inlineMessageId,
  });
}

async function submitInlineJob(
  deps: HandlerDeps,
  options: {
    updateId: number;
    from: TgUser;
    source: string;
    host: InlineHost;
    dumpPartitions: string[];
    inlineMessageId: string;
  },
): Promise<void> {
  const { from, updateId, inlineMessageId } = options;
  const now = nowSeconds(deps);
  const draft: NewJob = {
    id: deps.newId ? deps.newId() : randomJobId(),
    dispatchKey: updateId,
    userId: from.id,
    userName: fullName({ ...from, id: from.id }),
    chatId: 0,
    messageId: 0,
    inlineMessageId,
    source: options.source,
    sourceKind: "url",
    host: options.host,
    dumpPartitions: options.dumpPartitions,
    fileId: null,
    fileName: null,
    fileSize: null,
  };

  let reserved: { job: JobRecord; created: boolean };
  try {
    reserved = await deps.store.reserve(draft);
  } catch (error) {
    console.log(
      JSON.stringify({
        level: "error",
        msg: "Could not reserve inline job",
        user_id: from.id,
        error: error instanceof Error ? error.message : String(error),
      }),
    );
    return;
  }
  if (!reserved.created) {
    console.log(
      JSON.stringify({
        level: "info",
        msg: "Duplicate inline job dispatch ignored",
        job_id: reserved.job.id,
        dispatch_key: updateId,
      }),
    );
    return;
  }

  const job = reserved.job;
  console.log(
    JSON.stringify({
      level: "info",
      msg: "Submitting inline job",
      job_id: job.id,
      user_id: job.userId,
      host: job.host,
      dump_partitions: job.dumpPartitions.length,
    }),
  );

  try {
    await editJobCard(
      deps,
      job,
      renderJob(job, now),
      cancelKeyboard(job.id),
    );
  } catch (error) {
    console.log(
      JSON.stringify({
        level: "error",
        msg: "Could not edit the inline job card",
        job_id: job.id,
        error: error instanceof Error ? error.message : String(error),
      }),
    );
    await deps.store.remove(job.id);
    return;
  }

  await dispatchReservedJob(deps, job);
}

async function handleLinkCommand(  deps: HandlerDeps,
  message: TgMessage,
  command: string,
  host: "gofile" | "pixeldrain",
  args: string[],
  updateId: number,
): Promise<void> {
  let source: string | null = null;
  let sourceIndex = -1;
  for (let index = 0; index < args.length; index += 1) {
    const found = extractUrl(args[index]);
    if (found) {
      source = found;
      sourceIndex = index;
      break;
    }
  }

  const remaining = args.filter((_, index) => index !== sourceIndex);
  const { values, requested } = parseDumpArgs(remaining);
  let dumpPartitions: string[] = [];
  try {
    dumpPartitions = normalizePartitions(values);
  } catch (error) {
    const text = error instanceof DumpError ? error.message : String(error);
    await reply(deps, message, htmlEscape(text));
    return;
  }

  if (requested && dumpPartitions.length === 0) {
    await reply(deps, message, renderDumpUsage(command));
    return;
  }

  const replied = message.reply_to_message;
  if (!source && replied) {
    const attachment = messageAttachment(replied);
    if (attachment) {
      await mirrorAttachment(deps, {
        updateId,
        message,
        replied,
        attachment,
        host,
        dumpPartitions,
      });
      return;
    }
    if (replied.text) source = extractUrl(replied.text);
  }

  if (!source) {
    await reply(deps, message, renderUsage(command));
    return;
  }

  await submitJob(deps, {
    updateId,
    message,
    from: message.from!,
    source,
    sourceKind: "url",
    host,
    dumpPartitions,
  });
}

async function mirrorAttachment(
  deps: HandlerDeps,
  options: {
    updateId: number;
    message: TgMessage;
    replied: TgMessage;
    attachment: TgFile;
    host: "gofile" | "pixeldrain";
    dumpPartitions: string[];
  },
): Promise<void> {
  const { message, replied, attachment, host, dumpPartitions, updateId } = options;
  const document = replied.document;

  if (document) {
    const name = document.file_name || "";
    const isTorrent =
      name.toLowerCase().endsWith(".torrent") ||
      document.mime_type === "application/x-bittorrent" ||
      document.mime_type === "application/x-bittorrent; charset=utf-8";
    if (isTorrent) {
      if (document.file_size && document.file_size > TELEGRAM_FILE_LIMIT) {
        await reply(
          deps,
          message,
          "Telegram only lets bots download files up to 20 MB. "
            + "Send a direct link to a larger .torrent file instead.",
        );
        return;
      }
      const filename = sanitizeFilename(name || "file.torrent", "file.torrent");
      if (!document.file_id) {
        await reply(deps, message, "Telegram did not provide a file id for that .torrent file.");
        return;
      }
      await submitJob(deps, {
        updateId,
        message,
        from: message.from!,
        source: filename,
        sourceKind: "torrent_file",
        fileId: document.file_id,
        fileName: filename,
        fileSize: document.file_size || null,
        host,
        dumpPartitions,
      });
      return;
    }
  }

  const size = attachment.file_size || null;
  if (size && size > TELEGRAM_FILE_LIMIT) {
    await reply(
      deps,
      message,
      "Telegram only lets bots download files up to 20 MB. "
        + "Send a direct link to a larger file instead.",
    );
    return;
  }
  if (size && deps.config.maxFileSize > 0 && size > deps.config.maxFileSize) {
    await reply(
      deps,
      message,
      `That file is ${humanSize(size)}, above the ${humanSize(deps.config.maxFileSize)} limit.`,
    );
    return;
  }

  const filename = attachmentFilename(replied, attachment);
  if (!attachment.file_id) {
    await reply(deps, message, "Telegram did not provide a file id for that attachment.");
    return;
  }
  await submitJob(deps, {
    updateId,
    message,
    from: message.from!,
    source: filename,
    sourceKind: "telegram_file",
    fileId: attachment.file_id,
    fileName: filename,
    fileSize: size,
    host,
    dumpPartitions,
  });
}

async function submitJob(
  deps: HandlerDeps,
  options: {
    updateId: number;
    message: TgMessage;
    from: { id: number; first_name?: string; last_name?: string; username?: string };
    source: string;
    sourceKind: "url" | "torrent_file" | "telegram_file";
    fileId?: string | null;
    fileName?: string | null;
    fileSize?: number | null;
    host: "gofile" | "pixeldrain";
    dumpPartitions: string[];
  },
): Promise<void> {
  const { message, from, host, dumpPartitions, updateId } = options;
  const now = nowSeconds(deps);
  const draft: NewJob = {
    id: deps.newId ? deps.newId() : randomJobId(),
    dispatchKey: updateId,
    userId: from.id,
    userName: fullName({ ...from, id: from.id }),
    chatId: message.chat.id,
    messageId: 0,
    source: options.source,
    sourceKind: options.sourceKind,
    host,
    dumpPartitions,
    fileId: options.fileId ?? null,
    fileName: options.fileName ?? null,
    fileSize: options.fileSize ?? null,
  };

  let reserved: { job: JobRecord; created: boolean };
  try {
    reserved = await deps.store.reserve(draft);
  } catch (error) {
    await reply(
      deps,
      message,
      `Could not create the job: ${htmlEscape(error instanceof Error ? error.message : String(error))}`,
    );
    return;
  }
  if (!reserved.created) {
    console.log(
      JSON.stringify({
        level: "info",
        msg: "Duplicate job dispatch ignored",
        job_id: reserved.job.id,
        dispatch_key: updateId,
      }),
    );
    return;
  }
  let job = reserved.job;
  console.log(
    JSON.stringify({
      level: "info",
      msg: "Submitting job",
      job_id: job.id,
      chat_id: job.chatId,
      user_id: job.userId,
      host: job.host,
      source_kind: job.sourceKind,
      dump_partitions: job.dumpPartitions.length,
    }),
  );

  let sent: { message_id: number };
  try {
    sent = await deps.tg.sendMessage(
      job.chatId,
      renderJob(job, now),
      cancelKeyboard(job.id),
    );
  } catch (error) {
    console.log(
      JSON.stringify({
        level: "error",
        msg: "Could not create the job message",
        job_id: job.id,
        chat_id: job.chatId,
        error: error instanceof Error ? error.message : String(error),
      }),
    );
    await deps.store.remove(job.id);
    await reply(
      deps,
      message,
      `Could not create the job message: ${htmlEscape(error instanceof Error ? error.message : String(error))}`,
    );
    return;
  }

  job = (await deps.store.setMessage(job.id, sent.message_id)) || {
    ...job,
    messageId: sent.message_id,
  };

  await dispatchReservedJob(deps, job);
}

async function dispatchReservedJob(
  deps: HandlerDeps,
  job: JobRecord,
): Promise<void> {
  const now = nowSeconds(deps);
  const payload = buildPayload(job, deps.config);

  let token: string;
  try {
    token = await encryptPayload(deps.config.remoteJobKey, payload);
  } catch (error) {
    await failDispatch(deps, job, error);
    return;
  }

  try {
    const { runId, runUrl } = await deps.gh.dispatch(token, job.id);
    console.log(
      JSON.stringify({
        level: "info",
        msg: "Dispatched job",
        job_id: job.id,
        run_id: runId,
      }),
    );
    job =
      (await deps.store.attachRun(job.id, runId, runUrl)) || {
        ...job,
        runId,
        runUrl,
        state: "queued",
        detail: "Waiting for a GitHub Actions runner…",
      };
    try {
      await editJobCard(
        deps,
        job,
        renderJob(job, now),
        cancelKeyboard(job.id),
      );
    } catch {
      // The worker may already be editing the card; keep the queued message.
    }
  } catch (error) {
    await failDispatch(deps, job, error);
  }
}

async function failDispatch(
  deps: HandlerDeps,
  job: JobRecord,
  error: unknown,
): Promise<void> {
  const reason = error instanceof Error ? error.message : String(error);
  console.log(
    JSON.stringify({
      level: "error",
      msg: "Dispatch failed",
      job_id: job.id,
      error: reason,
    }),
  );
  const now = nowSeconds(deps);
  const failed = {
    ...job,
    error: `Could not start GitHub Actions: ${reason}`,
    finishedAt: now,
  };
  try {
    await editJobCard(
      deps,
      job,
      renderFailed(failed, "", now),
      emptyKeyboard(),
    );
  } catch {
    // The card may have been removed already.
  }
  await deps.store.remove(job.id);
}

type CancelResult = {
  ok: boolean;
  pending?: boolean;
  job: JobRecord;
  error?: string;
};

async function cancelJob(deps: HandlerDeps, job: JobRecord): Promise<CancelResult> {
  if (job.state === "cancelling") return { ok: true, job };
  if (!job.runId) return { ok: false, pending: true, job };
  let cancelled = false;
  try {
    cancelled = await deps.gh.cancel(job.runId);
  } catch (error) {
    return {
      ok: false,
      job,
      error: error instanceof Error ? error.message : String(error),
    };
  }
  if (!cancelled) return { ok: false, job };
  const updated =
    (await deps.store.update(job.id, {
      state: "cancelling",
      detail: "Cancelling on GitHub Actions…",
    })) || { ...job, state: "cancelling", detail: "Cancelling on GitHub Actions…" };
  return { ok: true, job: updated };
}

async function markCancelling(deps: HandlerDeps, job: JobRecord): Promise<void> {
  try {
    await editJobCard(
      deps,
      job,
      renderJob(job, nowSeconds(deps)),
      emptyKeyboard(),
    );
  } catch {
    // The card may already be gone.
  }
}

async function cancelAllForUser(
  deps: HandlerDeps,
  message: TgMessage,
  userId: number,
): Promise<void> {
  const jobs = await deps.store.listForUser(userId);
  let count = 0;
  let pending = false;
  for (const job of jobs) {
    const result = await cancelJob(deps, job);
    if (result.ok) {
      count += 1;
      await markCancelling(deps, result.job);
    } else if (result.pending) {
      pending = true;
    }
  }
  if (count) {
    await reply(deps, message, `Cancelling ${count} job(s)…`);
  } else if (pending) {
    await reply(deps, message, "The job is still starting. Try /cancel again in a few seconds.");
  } else {
    await reply(deps, message, "You have no active jobs.");
  }
}

async function handleCallback(
  deps: HandlerDeps,
  query: TgCallbackQuery,
): Promise<void> {
  const from = query.from;
  const data = String(query.data || "");
  const message = query.message;
  if (!from || !data) {
    await deps.tg.answerCallbackQuery(query.id);
    return;
  }

  if (message) {
    if (isGroup(message.chat.type)) {
      if (!(await groupAllowed(deps, message.chat.id))) {
        await deps.tg.answerCallbackQuery(query.id, "This group is not authorized.", true);
        return;
      }
    } else if (message.chat.type === "private" && !privateAuthorized(deps, from.id)) {
      await deps.tg.answerCallbackQuery(query.id, "Not authorized.", true);
      return;
    }
  } else if (query.inline_message_id && !privateAuthorized(deps, from.id)) {
    await deps.tg.answerCallbackQuery(query.id, "Not authorized.", true);
    return;
  }

  const parts = data.split(":");
  if (parts[0] === "pick") {
    await deps.tg.answerCallbackQuery(
      query.id,
      "That menu is no longer used. Send the link again.",
      true,
    );
    return;
  }

  if (parts[0] === "cancel" && parts.length === 2) {
    const job = await deps.store.get(parts[1]);
    if (!job) {
      await deps.tg.answerCallbackQuery(query.id, "Job not found or already finished.", true);
      return;
    }
    if (!canCancel(deps, job, from.id)) {
      await deps.tg.answerCallbackQuery(
        query.id,
        "Only the job's launcher or an admin can cancel it.",
        true,
      );
      return;
    }
    await deps.tg.answerCallbackQuery(query.id, "Cancelling…");
    const result = await cancelJob(deps, job);
    if (result.ok) {
      await markCancelling(deps, result.job);
    } else if (result.pending) {
      await deps.tg.answerCallbackQuery(
        query.id,
        "The job is still starting. Try again in a few seconds.",
        true,
      );
    } else {
      try {
        await editJobCard(
          deps,
          job,
          "Could not cancel the GitHub Actions run. Try /cancel again.",
          emptyKeyboard(),
        );
      } catch {
        // The card may already be gone.
      }
    }
    return;
  }

  await deps.tg.answerCallbackQuery(query.id);
}

/** Finalize a cancelled run after GitHub has stopped it. */
export async function deliverCancelled(
  deps: HandlerDeps,
  job: JobRecord,
): Promise<void> {
  const now = nowSeconds(deps);
  const finished = { ...job, finishedAt: now };
  if (job.inlineMessageId) {
    try {
      await editJobCard(
        deps,
        job,
        renderCancelled(finished, now),
        emptyKeyboard(),
      );
    } catch {
      // Inline messages cannot be deleted; editing is the only option.
    }
    await deps.store.remove(job.id);
    return;
  }
  const deleted = await deps.tg.deleteMessage(job.chatId, job.messageId);
  if (deleted === "deleted" || deleted === "not_found") {
    try {
      await deps.tg.sendMessage(job.chatId, renderCancelled(finished, now));
    } catch {
      // Nothing more we can do.
    }
  } else if (deleted === "error") {
    try {
      await deps.tg.editMessageText(
        job.chatId,
        job.messageId,
        renderCancelled(finished, now),
        emptyKeyboard(),
      );
    } catch {
      // Nothing more we can do.
    }
  }
  await deps.store.remove(job.id);
}

/** Finalize a run whose worker died before it could post a card. */
export async function deliverFailed(
  deps: HandlerDeps,
  job: JobRecord,
): Promise<void> {
  const now = nowSeconds(deps);
  const finished = {
    ...job,
    error: job.error || "GitHub Actions stopped before the worker could report details.",
    finishedAt: now,
  };
  if (job.inlineMessageId) {
    try {
      await editJobCard(
        deps,
        job,
        renderFailed(finished, job.runUrl, now),
        emptyKeyboard(),
      );
    } catch {
      // Inline messages cannot be deleted; editing is the only option.
    }
    await deps.store.remove(job.id);
    return;
  }
  const deleted = await deps.tg.deleteMessage(job.chatId, job.messageId);
  if (deleted === "deleted") {
    try {
      await deps.tg.sendMessage(
        job.chatId,
        renderFailed(finished, job.runUrl, now),
      );
    } catch {
      // Nothing more we can do.
    }
  } else if (deleted === "error") {
    try {
      await deps.tg.editMessageText(
        job.chatId,
        job.messageId,
        renderFailed(finished, job.runUrl, now),
        emptyKeyboard(),
      );
    } catch {
      // Nothing more we can do.
    }
  }
  await deps.store.remove(job.id);
}
