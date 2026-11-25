import { describe, expect, it } from "vitest";
import type { Config } from "../src/config";
import { decryptPayload } from "../src/fernet";
import type { GitHubLike } from "../src/handlers";
import {
  handleUpdate,
  deliverCancelled,
  deliverFailed,
  type HandlerDeps,
  type TelegramLike,
} from "../src/handlers";
import type { StoreLike } from "../src/store";
import type {
  GroupCache,
  JobRecord,
  NewJob,
  TgCallbackQuery,
  TgChosenInlineResult,
  TgFile,
  TgInlineQuery,
  TgMessage,
  TgUpdate,
} from "../src/types";

const KEY = "9STVjW3ysshly5_FpGoEwxYEnvtYUNMIYEFe8jwg_jM=";

function config(overrides: Partial<Config> = {}): Config {
  return {
    telegramToken: "123:test",
    webhookSecret: "secret",
    githubToken: "token",
    remoteJobKey: KEY,
    allowedUserIds: new Set([42]),
    repo: "owner/repo",
    ref: "main",
    workflow: "job.yml",
    maxFileSize: 0,
    jobTimeout: 7200,
    multiFileMode: "largest",
    ytdlpFormat: "bv*+ba/b",
    gofileToken: "",
    pixeldrainApiKey: "",
    botUsername: "testbot",
    ...overrides,
  };
}

class FakeStore implements StoreLike {
  readonly jobs = new Map<string, JobRecord>();
  readonly seenIds = new Set<number>();
  readonly groups = new Map<number, GroupCache>();

  async seen(updateId: number): Promise<boolean> {
    return this.seenIds.has(updateId);
  }

  async markSeen(updateId: number): Promise<void> {
    this.seenIds.add(updateId);
  }

  async reserve(job: NewJob): Promise<{ job: JobRecord; created: boolean }> {
    for (const existing of this.jobs.values()) {
      if (existing.dispatchKey === job.dispatchKey) {
        return { job: existing, created: false };
      }
    }
    const record: JobRecord = {
      ...job,
      runId: 0,
      runUrl: "",
      createdAt: 1000,
      startedAt: 1000,
      state: "dispatching",
      detail: "Starting a GitHub Actions runner…",
    };
    this.jobs.set(record.id, record);
    return { job: record, created: true };
  }

  async setMessage(id: string, messageId: number): Promise<JobRecord | null> {
    const job = this.jobs.get(id);
    if (!job) return null;
    job.messageId = messageId;
    return job;
  }

  async setInlineMessage(
    id: string,
    inlineMessageId: string,
  ): Promise<JobRecord | null> {
    const job = this.jobs.get(id);
    if (!job) return null;
    job.inlineMessageId = inlineMessageId;
    return job;
  }

  async attachRun(id: string, runId: number, runUrl: string): Promise<JobRecord | null> {
    const job = this.jobs.get(id);
    if (!job) return null;
    job.runId = runId;
    job.runUrl = runUrl;
    job.state = "queued";
    job.detail = "Waiting for a GitHub Actions runner…";
    return job;
  }

  async update(id: string, patch: Partial<JobRecord>): Promise<JobRecord | null> {
    const job = this.jobs.get(id);
    if (!job) return null;
    Object.assign(job, patch);
    return job;
  }

  async remove(id: string): Promise<void> {
    this.jobs.delete(id);
  }

  async get(id: string): Promise<JobRecord | null> {
    return this.jobs.get(id) || null;
  }

  async list(): Promise<JobRecord[]> {
    return [...this.jobs.values()];
  }

  async listForUser(userId: number): Promise<JobRecord[]> {
    return [...this.jobs.values()].filter((job) => job.userId === userId);
  }

  async getGroup(chatId: number): Promise<GroupCache | null> {
    return this.groups.get(chatId) || null;
  }

  async setGroup(chatId: number, allowed: boolean, checkedAt: number): Promise<void> {
    this.groups.set(chatId, { allowed, checkedAt });
  }
}

class FakeTelegram implements TelegramLike {
  readonly sent: Array<{
    chatId: number;
    text: string;
    keyboard?: unknown;
    replyTo?: number;
    messageId: number;
  }> = [];
  readonly edits: Array<{ chatId: number; messageId: number; text: string }> = [];
  readonly inlineEdits: Array<{
    inlineMessageId: string;
    text: string;
    keyboard?: unknown;
  }> = [];
  readonly inlineAnswers: Array<{
    id: string;
    results: Array<Record<string, unknown>>;
    options?: { cacheTime?: number; isPersonal?: boolean };
  }> = [];
  readonly answers: Array<{ id: string; text?: string; alert?: boolean }> = [];
  memberStatus = "member";
  nextMessageId = 100;

  async sendMessage(
    chatId: number,
    text: string,
    keyboard?: unknown,
    replyTo?: number,
  ): Promise<{ message_id: number }> {
    const messageId = this.nextMessageId++;
    this.sent.push({ chatId, text, keyboard, replyTo, messageId });
    return { message_id: messageId };
  }

  async editMessageText(
    chatId: number,
    messageId: number,
    text: string,
  ): Promise<void> {
    this.edits.push({ chatId, messageId, text });
  }

  async editInlineMessageText(
    inlineMessageId: string,
    text: string,
    keyboard?: unknown,
  ): Promise<void> {
    this.inlineEdits.push({ inlineMessageId, text, keyboard });
  }

  async answerInlineQuery(
    id: string,
    results: Array<Record<string, unknown>>,
    options?: { cacheTime?: number; isPersonal?: boolean },
  ): Promise<void> {
    this.inlineAnswers.push({ id, results, options });
  }

  async deleteMessage(): Promise<"deleted"> {
    return "deleted";
  }

  async answerCallbackQuery(
    id: string,
    text?: string,
    alert?: boolean,
  ): Promise<void> {
    this.answers.push({ id, text, alert });
  }

  async getChatMember(): Promise<{ status: string }> {
    return { status: this.memberStatus };
  }

  async getMe(): Promise<{ id: number; username?: string }> {
    return { id: 1, username: "testbot" };
  }
}

class FakeGitHub implements GitHubLike {
  readonly dispatched: Array<{ payload: string; jobId: string }> = [];
  readonly cancelled: number[] = [];
  nextRunId = 100;

  async dispatch(
    payload: string,
    jobId: string,
  ): Promise<{ runId: number; runUrl: string }> {
    this.dispatched.push({ payload, jobId });
    const runId = this.nextRunId++;
    return {
      runId,
      runUrl: `https://github.com/owner/repo/actions/runs/${runId}`,
    };
  }

  async cancel(runId: number): Promise<boolean> {
    this.cancelled.push(runId);
    return true;
  }

  async view(): Promise<{
    status: string;
    conclusion: string | null;
    htmlUrl: string;
  }> {
    return {
      status: "in_progress",
      conclusion: null,
      htmlUrl: "https://github.com/owner/repo/actions/runs/1",
    };
  }
}

function makeDeps(overrides: Partial<Config> = {}): {
  deps: HandlerDeps;
  store: FakeStore;
  tg: FakeTelegram;
  gh: FakeGitHub;
} {
  const store = new FakeStore();
  const tg = new FakeTelegram();
  const gh = new FakeGitHub();
  let counter = 0;
  const deps: HandlerDeps = {
    config: config(overrides),
    store,
    tg,
    gh,
    now: () => 1000,
    newId: () => `job${String((counter += 1)).padStart(5, "0")}`,
  };
  return { deps, store, tg, gh };
}

const privateChat = { id: 1, type: "private" as const };
const groupChat = { id: -100, type: "group" as const };
const user = { id: 42, first_name: "Fərhad" };

function message(overrides: Partial<TgMessage> = {}): TgMessage {
  return {
    message_id: 1,
    date: 0,
    chat: privateChat,
    from: user,
    text: "/gf https://example.com/file.bin",
    ...overrides,
  };
}

function update(messageValue: TgMessage, updateId = 1): TgUpdate {
  return { update_id: updateId, message: messageValue };
}

async function payloadOf(gh: FakeGitHub, index = 0): Promise<Record<string, unknown>> {
  return decryptPayload(KEY, gh.dispatched[index].payload);
}

describe("Telegram link jobs", () => {
  it("dispatches a /gf link with an encrypted worker payload", async () => {
    const { deps, store, tg, gh } = makeDeps();
    await handleUpdate(deps, update(message()));

    expect(gh.dispatched).toHaveLength(1);
    expect(tg.sent).toHaveLength(1);
    expect(tg.sent[0].text).toContain("GitHub Actions");
    expect(tg.sent[0].keyboard).toBeTruthy();

    const payload = await payloadOf(gh);
    expect(payload).toMatchObject({
      version: 1,
      job_id: gh.dispatched[0].jobId,
      user_id: 42,
      user_name: "Fərhad",
      chat_id: 1,
      message_id: tg.sent[0].messageId,
      source: "https://example.com/file.bin",
      source_kind: "url",
      host: "gofile",
      dump_partitions: [],
    });
    expect(store.jobs.get(gh.dispatched[0].jobId)?.state).toBe("queued");
  });

  it("routes /px to Pixeldrain and /mirror to Gofile", async () => {
    const { deps, gh } = makeDeps();
    await handleUpdate(deps, update(message({ text: "/px https://example.com/a.bin" })));
    await handleUpdate(
      deps,
      update(message({ message_id: 2, text: "/mirror https://example.com/b.bin" }), 2),
    );

    expect((await payloadOf(gh, 0)).host).toBe("pixeldrain");
    expect((await payloadOf(gh, 1)).host).toBe("gofile");
  });

  it("passes -dump partitions to the worker", async () => {
    const { deps, gh } = makeDeps();
    await handleUpdate(
      deps,
      update(message({ text: "/gf https://example.com/ota.zip -dump boot,vendor" })),
    );
    expect((await payloadOf(gh)).dump_partitions).toEqual(["boot", "vendor"]);
  });

  it("rejects unsafe partition names without dispatching", async () => {
    const { deps, tg, gh } = makeDeps();
    await handleUpdate(
      deps,
      update(message({ text: "/gf https://example.com/ota.zip -dump ../boot" })),
    );
    expect(gh.dispatched).toHaveLength(0);
    expect(tg.sent.at(-1)?.text).toContain("not a valid partition name");
  });

  it("uses a link from the replied message", async () => {
    const { deps, gh } = makeDeps();
    const replied = message({
      message_id: 2,
      text: "https://example.com/replied.bin",
      reply_to_message: undefined,
    });
    const command = message({ message_id: 3, text: "/gf", reply_to_message: replied });
    await handleUpdate(deps, update(command));

    expect((await payloadOf(gh)).source).toBe("https://example.com/replied.bin");
  });

  it("shows usage when /gf has no link and no reply", async () => {
    const { deps, tg, gh } = makeDeps();
    await handleUpdate(deps, update(message({ text: "/gf" })));
    expect(gh.dispatched).toHaveLength(0);
    expect(tg.sent.at(-1)?.text).toContain("Usage:");
  });
});

describe("Telegram attachment replies", () => {
  const animation: TgFile = {
    file_id: "file-animation",
    file_unique_id: "unique-animation",
    mime_type: "video/mp4",
  };

  it("mirrors an unnamed animation as a Telegram file, not a torrent", async () => {
    const { deps, gh } = makeDeps();
    const replied = message({
      message_id: 2,
      text: undefined,
      document: animation,
      animation,
    });
    const command = message({ message_id: 3, text: "/gf", reply_to_message: replied });

    await handleUpdate(deps, update(command));
    const payload = await payloadOf(gh);
    expect(payload.source_kind).toBe("telegram_file");
    expect(payload.file_id).toBe("file-animation");
    expect(payload.file_name).toBe("animation_unique-animation.mp4");
  });

  it("recognises a replied .torrent document", async () => {
    const { deps, gh } = makeDeps();
    const replied = message({
      message_id: 2,
      text: undefined,
      document: {
        file_id: "file-torrent",
        file_unique_id: "unique-torrent",
        file_name: "ubuntu.torrent",
        mime_type: "application/x-bittorrent",
      },
    });
    const command = message({ message_id: 3, text: "/gf", reply_to_message: replied });

    await handleUpdate(deps, update(command));
    const payload = await payloadOf(gh);
    expect(payload.source_kind).toBe("torrent_file");
    expect(payload.file_name).toBe("ubuntu.torrent");
  });

  it("ignores an attachment sent without /gf or /px", async () => {
    const { deps, tg, gh } = makeDeps();
    await handleUpdate(
      deps,
      update(message({ text: undefined, document: animation, animation })),
    );
    expect(gh.dispatched).toHaveLength(0);
    expect(tg.sent).toHaveLength(0);
  });

  it("rejects replied attachments above Telegram's 20 MB download limit", async () => {
    const { deps, tg, gh } = makeDeps();
    const replied = message({
      message_id: 2,
      text: undefined,
      document: {
        file_id: "big",
        file_unique_id: "big",
        file_size: 21 * 1024 * 1024,
      },
    });
    const command = message({ message_id: 3, text: "/px", reply_to_message: replied });
    await handleUpdate(deps, update(command));

    expect(gh.dispatched).toHaveLength(0);
    expect(tg.sent.at(-1)?.text).toContain("20 MB");
  });
});

describe("groups and permissions", () => {
  it("ignores a group where no allowed user is a member", async () => {
    const { deps, tg, gh } = makeDeps();
    tg.memberStatus = "left";
    await handleUpdate(
      deps,
      update(message({ chat: groupChat, text: "/gf@testbot https://example.com/a.bin" })),
    );
    expect(gh.dispatched).toHaveLength(0);
    expect(tg.sent).toHaveLength(0);
  });

  it("accepts a group command addressed to this bot", async () => {
    const { deps, gh } = makeDeps();
    await handleUpdate(
      deps,
      update(message({ chat: groupChat, text: "/gf@testbot https://example.com/a.bin" })),
    );
    expect(gh.dispatched).toHaveLength(1);
  });

  it("ignores a group command addressed to another bot", async () => {
    const { deps, gh } = makeDeps();
    await handleUpdate(
      deps,
      update(message({ chat: groupChat, text: "/gf@otherbot https://example.com/a.bin" })),
    );
    expect(gh.dispatched).toHaveLength(0);
  });

  it("denies a private user who is not in ALLOWED_USER_IDS", async () => {
    const { deps, tg, gh } = makeDeps();
    await handleUpdate(
      deps,
      update(message({ from: { id: 99, first_name: "Other" } })),
    );
    expect(gh.dispatched).toHaveLength(0);
    expect(tg.sent.at(-1)?.text).toContain("This bot is private");
  });

  it("lets the launcher cancel their job with the inline button", async () => {
    const { deps, store, tg, gh } = makeDeps();
    await handleUpdate(deps, update(message()));
    const jobId = gh.dispatched[0].jobId;
    const cardId = tg.sent[0].messageId;

    const callback: TgCallbackQuery = {
      id: "cb-1",
      from: user,
      message: {
        message_id: cardId,
        date: 0,
        chat: privateChat,
        from: { id: 1, first_name: "Bot", is_bot: true },
      },
      data: `cancel:${jobId}`,
    };
    await handleUpdate(deps, { update_id: 2, callback_query: callback });

    expect(gh.cancelled).toEqual([100]);
    expect(store.jobs.get(jobId)?.state).toBe("cancelling");
    expect(tg.answers.at(-1)?.text).toBe("Cancelling…");
  });

  it("does not let another user cancel the job with the button", async () => {
    const { deps, tg, gh } = makeDeps();
    await handleUpdate(deps, update(message()));
    const jobId = gh.dispatched[0].jobId;
    const cardId = tg.sent[0].messageId;

    const callback: TgCallbackQuery = {
      id: "cb-2",
      from: { id: 99, first_name: "Other" },
      message: {
        message_id: cardId,
        date: 0,
        chat: privateChat,
        from: { id: 1, first_name: "Bot", is_bot: true },
      },
      data: `cancel:${jobId}`,
    };
    await handleUpdate(deps, { update_id: 2, callback_query: callback });

    expect(gh.cancelled).toEqual([]);
    expect(storeJobState(deps, jobId)).toBe("queued");
  });
});

function storeJobState(deps: HandlerDeps, jobId: string): string | undefined {
  return (deps.store as FakeStore).jobs.get(jobId)?.state;
}

describe("status and cancel commands", () => {
  it("lists active jobs and cancels them with /cancel", async () => {
    const { deps, tg, gh } = makeDeps();
    await handleUpdate(deps, update(message()));
    await handleUpdate(deps, update(message({ message_id: 2, text: "/status" }), 2));
    expect(tg.sent.at(-1)?.text).toContain("Active jobs");

    await handleUpdate(deps, update(message({ message_id: 3, text: "/cancel" }), 3));
    expect(gh.cancelled).toEqual([100]);
    expect(tg.sent.at(-1)?.text).toContain("Cancelling 1 job(s)");
  });
});

function inlineQuery(overrides: Partial<TgInlineQuery> = {}): TgInlineQuery {
  return {
    id: "iq-1",
    from: user,
    query: "/gf https://example.com/file.bin",
    offset: "",
    ...overrides,
  };
}

function chosenInline(
  overrides: Partial<TgChosenInlineResult> = {},
): TgChosenInlineResult {
  return {
    result_id: "gofile",
    from: user,
    query: "/gf https://example.com/file.bin",
    inline_message_id: "inline-1",
    ...overrides,
  };
}

describe("inline mode", () => {
  it("answers an inline query without creating a job", async () => {
    const { deps, tg, gh } = makeDeps();
    await handleUpdate(deps, {
      update_id: 5,
      inline_query: inlineQuery({
        query: "/gf https://example.com/file.bin -dump boot,vendor",
      }),
    });

    expect(gh.dispatched).toHaveLength(0);
    expect(tg.inlineAnswers).toHaveLength(1);
    expect(tg.inlineAnswers[0].options).toMatchObject({
      cacheTime: 0,
      isPersonal: true,
    });
    const result = tg.inlineAnswers[0].results[0] as {
      title: string;
      input_message_content: { message_text: string };
      reply_markup: {
        inline_keyboard: Array<Array<{ text: string; callback_data: string }>>;
      };
    };
    expect(result.title).toBe("Mirror to Gofile");
    expect(result.input_message_content.message_text).toContain("boot, vendor");
    expect(result.reply_markup.inline_keyboard[0][0]).toMatchObject({
      text: "⏳ Starting…",
      callback_data: "inline:pending",
    });
  });

  it("dispatches a chosen inline result with inline_message_id", async () => {
    const { deps, store, tg, gh } = makeDeps();
    await handleUpdate(deps, {
      update_id: 6,
      chosen_inline_result: chosenInline({
        query: "/px https://example.com/a.bin -dump boot",
      }),
    });

    expect(gh.dispatched).toHaveLength(1);
    const payload = await payloadOf(gh);
    expect(payload).toMatchObject({
      chat_id: 0,
      message_id: 0,
      inline_message_id: "inline-1",
      host: "pixeldrain",
      dump_partitions: ["boot"],
    });
    expect(tg.sent).toHaveLength(0);
    expect(tg.inlineEdits.at(-1)?.inlineMessageId).toBe("inline-1");
    expect(store.jobs.get(gh.dispatched[0].jobId)?.inlineMessageId).toBe(
      "inline-1",
    );
  });

  it("denies inline queries from other users", async () => {
    const { deps, tg, gh } = makeDeps();
    await handleUpdate(deps, {
      update_id: 7,
      inline_query: inlineQuery({ from: { id: 99, first_name: "Other" } }),
    });

    expect(gh.dispatched).toHaveLength(0);
    const result = tg.inlineAnswers[0].results[0] as {
      title: string;
      reply_markup?: unknown;
    };
    expect(result.title).toBe("This bot is private");
    expect(result.reply_markup).toBeUndefined();
  });

  it("answers the placeholder button while a job is starting", async () => {
    const { deps, tg } = makeDeps();
    await handleUpdate(deps, {
      update_id: 8,
      callback_query: {
        id: "cb-pending",
        from: user,
        inline_message_id: "inline-1",
        data: "inline:pending",
      },
    });

    expect(tg.answers.at(-1)?.text).toContain("still starting");
  });

  it("ignores a chosen result without inline_message_id", async () => {
    const { deps, gh } = makeDeps();
    await handleUpdate(deps, {
      update_id: 8,
      chosen_inline_result: chosenInline({ inline_message_id: undefined }),
    });

    expect(gh.dispatched).toHaveLength(0);
  });

  it("cancels an inline job from its callback button", async () => {
    const { deps, tg, gh } = makeDeps();
    await handleUpdate(deps, {
      update_id: 9,
      chosen_inline_result: chosenInline(),
    });
    const jobId = gh.dispatched[0].jobId;
    const callback: TgCallbackQuery = {
      id: "cb-inline",
      from: user,
      inline_message_id: "inline-1",
      data: `cancel:${jobId}`,
    };

    await handleUpdate(deps, { update_id: 10, callback_query: callback });

    expect(gh.cancelled).toEqual([100]);
    expect(tg.answers.at(-1)?.text).toBe("Cancelling…");
    expect(tg.inlineEdits.at(-1)?.text).toContain("Cancelling");
  });

  it("edits the inline card when a cancelled run finishes", async () => {
    const { deps, store, tg, gh } = makeDeps();
    await handleUpdate(deps, {
      update_id: 11,
      chosen_inline_result: chosenInline(),
    });
    const job = store.jobs.get(gh.dispatched[0].jobId);
    expect(job).toBeTruthy();

    await deliverCancelled(deps, job!);

    expect(tg.inlineEdits.at(-1)?.text).toContain("Job cancelled");
    expect(tg.sent).toHaveLength(0);
    expect(store.jobs.has(job!.id)).toBe(false);
  });

  it("edits the inline card when a run fails without a worker report", async () => {
    const { deps, store, tg, gh } = makeDeps();
    await handleUpdate(deps, {
      update_id: 12,
      chosen_inline_result: chosenInline(),
    });
    const job = store.jobs.get(gh.dispatched[0].jobId);
    expect(job).toBeTruthy();

    await deliverFailed(deps, job!);

    expect(tg.inlineEdits.at(-1)?.text).toContain("Job failed");
    expect(tg.sent).toHaveLength(0);
    expect(store.jobs.has(job!.id)).toBe(false);
  });
});
