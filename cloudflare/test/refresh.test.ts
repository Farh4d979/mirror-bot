import { describe, expect, it } from "vitest";
import type { HandlerDeps, TelegramLike } from "../src/handlers";
import { refreshJobs } from "../src/refresh";
import type { StoreLike } from "../src/store";
import type { GroupCache, JobRecord, NewJob } from "../src/types";

function job(overrides: Partial<JobRecord> = {}): JobRecord {
  return {
    id: "abc12345",
    dispatchKey: 1,
    userId: 42,
    userName: "Test",
    chatId: 1,
    messageId: 7,
    source: "https://example.com/file.bin",
    sourceKind: "url",
    host: "gofile",
    dumpPartitions: [],
    compress: false,
    runId: 123,
    runUrl: "https://github.com/owner/repo/actions/runs/123",
    createdAt: 1000,
    startedAt: 1000,
    state: "queued",
    detail: "Waiting for a GitHub Actions runner…",
    ...overrides,
  };
}

class FakeStore implements StoreLike {
  constructor(public jobs: JobRecord[]) {}
  readonly removed: string[] = [];
  readonly updates: Array<{ id: string; patch: Partial<JobRecord> }> = [];

  async seen(): Promise<boolean> {
    return false;
  }
  async markSeen(): Promise<void> {}
  async reserve(): Promise<{ job: JobRecord; created: boolean }> {
    throw new Error("not used");
  }
  async setMessage(): Promise<JobRecord | null> {
    return null;
  }
  async setInlineMessage(): Promise<JobRecord | null> {
    return null;
  }
  async attachRun(): Promise<JobRecord | null> {
    return null;
  }
  async update(id: string, patch: Partial<JobRecord>): Promise<JobRecord | null> {
    this.updates.push({ id, patch });
    const found = this.jobs.find((item) => item.id === id);
    if (!found) return null;
    Object.assign(found, patch);
    return found;
  }
  async remove(id: string): Promise<void> {
    this.removed.push(id);
    this.jobs = this.jobs.filter((item) => item.id !== id);
  }
  async get(id: string): Promise<JobRecord | null> {
    return this.jobs.find((item) => item.id === id) || null;
  }
  async list(): Promise<JobRecord[]> {
    return this.jobs;
  }
  async listForUser(userId: number): Promise<JobRecord[]> {
    return this.jobs.filter((item) => item.userId === userId);
  }
  async getGroup(): Promise<GroupCache | null> {
    return null;
  }
  async setGroup(): Promise<void> {}
  async putPending(): Promise<void> {}
  async takePending(): Promise<{ pending: null; matched: boolean }> {
    return { pending: null, matched: false };
  }
}

class FakeTelegram implements TelegramLike {
  readonly sent: string[] = [];
  readonly edits: string[] = [];
  readonly inlineEdits: string[] = [];
  readonly inlineAnswers: Array<Array<Record<string, unknown>>> = [];
  deleteResult: "deleted" | "not_found" | "error" = "deleted";

  async sendMessage(_chatId: number, text: string): Promise<{ message_id: number }> {
    this.sent.push(text);
    return { message_id: 99 };
  }
  async editMessageText(_chatId: number, _messageId: number, text: string): Promise<void> {
    this.edits.push(text);
  }
  async editInlineMessageText(
    _inlineMessageId: string,
    text: string,
  ): Promise<void> {
    this.inlineEdits.push(text);
  }
  async deleteMessage(): Promise<"deleted" | "not_found" | "error"> {
    return this.deleteResult;
  }
  async answerCallbackQuery(): Promise<void> {}
  async answerInlineQuery(
    _id: string,
    results: Array<Record<string, unknown>>,
  ): Promise<void> {
    this.inlineAnswers.push(results);
  }
  async getChatMember(): Promise<{ status: string }> {
    return { status: "member" };
  }
  async getMe(): Promise<{ id: number; username?: string }> {
    return { id: 1, username: "testbot" };
  }
}

interface RunState {
  status: string;
  conclusion: string | null;
  htmlUrl: string;
}

class FakeGitHub {
  constructor(public runs: Record<number, RunState | Error> = {}) {}
  async dispatch(): Promise<{ runId: number; runUrl: string }> {
    throw new Error("not used");
  }
  async cancel(): Promise<boolean> {
    return true;
  }
  async view(runId: number): Promise<RunState> {
    const run = this.runs[runId];
    if (run instanceof Error) throw run;
    if (!run) throw new Error(`missing run ${runId}`);
    return run;
  }
}

function deps(
  store: FakeStore,
  tg: FakeTelegram,
  gh: FakeGitHub,
  now = 2000,
): HandlerDeps {
  return {
    config: {
      telegramToken: "123:test",
      webhookSecret: "secret",
      githubToken: "token",
      remoteJobKey: "9STVjW3ysshly5_FpGoEwxYEnvtYUNMIYEFe8jwg_jM=",
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
    },
    store,
    tg,
    gh,
    now: () => now,
  };
}

const RUN_URL = "https://github.com/owner/repo/actions/runs/123";

describe("cron monitor", () => {
  it("finalizes a cancelled run with a cancelled card", async () => {
    const store = new FakeStore([job({ state: "cancelling" })]);
    const tg = new FakeTelegram();
    const gh = new FakeGitHub({
      123: { status: "completed", conclusion: "cancelled", htmlUrl: RUN_URL },
    });

    await refreshJobs(deps(store, tg, gh));

    expect(store.removed).toEqual(["abc12345"]);
    expect(tg.sent.at(-1)).toContain("Job cancelled");
    expect(tg.sent.at(-1)).not.toContain("Actions run");
  });

  it("still posts a cancelled card when the progress card is already gone", async () => {
    const store = new FakeStore([job({ state: "cancelling" })]);
    const tg = new FakeTelegram();
    tg.deleteResult = "not_found";
    const gh = new FakeGitHub({
      123: { status: "completed", conclusion: "cancelled", htmlUrl: RUN_URL },
    });

    await refreshJobs(deps(store, tg, gh));

    expect(store.removed).toEqual(["abc12345"]);
    expect(tg.sent.at(-1)).toContain("Job cancelled");
  });

  it("posts a failure card when a run fails before the worker reports", async () => {
    const store = new FakeStore([job({ state: "running", startedAt: 1900 })]);
    const tg = new FakeTelegram();
    const gh = new FakeGitHub({
      123: { status: "completed", conclusion: "failure", htmlUrl: RUN_URL },
    });

    await refreshJobs(deps(store, tg, gh));

    expect(store.removed).toEqual(["abc12345"]);
    expect(tg.sent.at(-1)).toContain("Job failed");
    expect(tg.sent.at(-1)).toContain(RUN_URL);
  });

  it("removes a successful run without posting a second card", async () => {
    const store = new FakeStore([job({ state: "running" })]);
    const tg = new FakeTelegram();
    const gh = new FakeGitHub({
      123: { status: "completed", conclusion: "success", htmlUrl: RUN_URL },
    });

    await refreshJobs(deps(store, tg, gh));

    expect(store.removed).toEqual(["abc12345"]);
    expect(tg.sent).toHaveLength(0);
    expect(tg.edits).toHaveLength(0);
  });

  it("marks an in-progress run as running", async () => {
    const store = new FakeStore([job()]);
    const tg = new FakeTelegram();
    const gh = new FakeGitHub({
      123: { status: "in_progress", conclusion: null, htmlUrl: RUN_URL },
    });

    await refreshJobs(deps(store, tg, gh));

    expect(store.updates).toHaveLength(1);
    expect(store.updates[0].patch.state).toBe("running");
    expect(store.removed).toHaveLength(0);
    expect(tg.edits).toHaveLength(1);
    expect(tg.edits[0]).toContain("GitHub Actions");
    expect(tg.edits[0]).toContain("abc12345");
  });

  it("refreshes the queued card so the elapsed time stays live", async () => {
    const store = new FakeStore([job({ state: "queued" })]);
    const tg = new FakeTelegram();
    const gh = new FakeGitHub({
      123: { status: "queued", conclusion: null, htmlUrl: RUN_URL },
    });

    await refreshJobs(deps(store, tg, gh));

    expect(store.updates).toHaveLength(1);
    expect(store.updates[0].patch.state).toBe("queued");
    expect(tg.edits).toHaveLength(1);
    expect(tg.edits[0]).toContain("Waiting for a GitHub Actions runner");
  });

  it("fails a dispatch that never produced a run", async () => {
    const store = new FakeStore([
      job({ runId: 0, runUrl: "", createdAt: 1000, state: "dispatching" }),
    ]);
    const tg = new FakeTelegram();
    const gh = new FakeGitHub();

    await refreshJobs(deps(store, tg, gh, 1000 + 11 * 60));

    expect(store.removed).toEqual(["abc12345"]);
    expect(tg.sent.at(-1)).toContain("GitHub Actions never started this job");
  });

  it("fails a job whose run cannot be read for 12 hours", async () => {
    const store = new FakeStore([job({ createdAt: 1000, state: "running" })]);
    const tg = new FakeTelegram();
    const gh = new FakeGitHub({ 123: new Error("HTTP 403") });

    await refreshJobs(deps(store, tg, gh, 1000 + 12 * 60 * 60 + 1));

    expect(store.removed).toEqual(["abc12345"]);
    expect(tg.sent.at(-1)).toContain("Could not read the GitHub Actions run");
  });

  it("keeps a fresh dispatch that is still starting", async () => {
    const store = new FakeStore([
      job({ runId: 0, runUrl: "", createdAt: 1000, state: "dispatching" }),
    ]);
    const tg = new FakeTelegram();
    const gh = new FakeGitHub();

    await refreshJobs(deps(store, tg, gh, 1000 + 60));

    expect(store.removed).toHaveLength(0);
    expect(tg.sent).toHaveLength(0);
  });
});
