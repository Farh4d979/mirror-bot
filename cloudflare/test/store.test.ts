import { describe, expect, it } from "vitest";
import { JobStore } from "../src/store";
import type { DurableObjectStorageLike, NewJob } from "../src/types";

class MemoryStorage implements DurableObjectStorageLike {
  private readonly values = new Map<string, unknown>();

  async get<T>(key: string): Promise<T | undefined> {
    return this.values.get(key) as T | undefined;
  }

  async put<T>(key: string, value: T): Promise<void> {
    this.values.set(key, value);
  }
}

async function op(
  store: JobStore,
  body: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  const response = await store.fetch(
    new Request("https://store/op", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }),
  );
  return (await response.json()) as Record<string, unknown>;
}

const draft: NewJob = {
  id: "abc12345",
  dispatchKey: 99,
  userId: 42,
  userName: "Test",
  chatId: -100,
  messageId: 0,
  source: "https://example.com/file.bin",
  sourceKind: "url",
  host: "gofile",
  dumpPartitions: [],
  fileId: null,
  fileName: null,
  fileSize: null,
};

describe("Durable Object job store", () => {
  it("reserves one job per Telegram update", async () => {
    const store = new JobStore({ storage: new MemoryStorage() });
    const first = await op(store, { op: "reserve", job: draft });
    const second = await op(store, {
      op: "reserve",
      job: { ...draft, id: "different" },
    });

    expect(first.created).toBe(true);
    expect(second.created).toBe(false);
    expect((second.job as { id: string }).id).toBe("abc12345");
  });

  it("deduplicates inline jobs by inline_message_id", async () => {
    const store = new JobStore({ storage: new MemoryStorage() });
    const first = await op(store, {
      op: "reserve",
      job: { ...draft, inlineMessageId: "inline-1" },
    });
    const second = await op(store, {
      op: "reserve",
      job: {
        ...draft,
        id: "different",
        dispatchKey: 100,
        inlineMessageId: "inline-1",
      },
    });

    expect(first.created).toBe(true);
    expect(second.created).toBe(false);
    expect((second.job as { id: string }).id).toBe("abc12345");
  });

  it("stores pending inline requests and only consumes them for the launcher", async () => {
    const store = new JobStore({ storage: new MemoryStorage() });
    const pending = {
      userId: 42,
      source: "https://example.com/file.bin",
      host: "gofile",
      dumpPartitions: [],
      createdAt: Math.floor(Date.now() / 1000),
    };
    await op(store, { op: "putPending", token: "tok", pending });

    const wrongUser = await op(store, {
      op: "takePending",
      token: "tok",
      userId: 99,
    });
    expect(wrongUser.matched).toBe(false);
    expect((wrongUser.pending as { userId: number }).userId).toBe(42);

    const launcher = await op(store, {
      op: "takePending",
      token: "tok",
      userId: 42,
    });
    expect(launcher.matched).toBe(true);
    expect((launcher.pending as { source: string }).source).toBe(
      "https://example.com/file.bin",
    );

    const again = await op(store, {
      op: "takePending",
      token: "tok",
      userId: 42,
    });
    expect(again.pending).toBeNull();
  });

  it("tracks message ids, runs, updates and removal", async () => {
    const storage = new MemoryStorage();
    const store = new JobStore({ storage });
    await op(store, { op: "reserve", job: draft });

    const withMessage = await op(store, {
      op: "setMessage",
      id: draft.id,
      messageId: 55,
    });
    expect((withMessage.job as { messageId: number }).messageId).toBe(55);

    const withRun = await op(store, {
      op: "attachRun",
      id: draft.id,
      runId: 123,
      runUrl: "https://github.com/owner/repo/actions/runs/123",
    });
    expect((withRun.job as { state: string }).state).toBe("queued");

    const updated = await op(store, {
      op: "update",
      id: draft.id,
      patch: { state: "cancelling", detail: "Cancelling…" },
    });
    expect((updated.job as { state: string }).state).toBe("cancelling");

    const listed = await op(store, { op: "list" });
    expect(listed.jobs).toHaveLength(1);

    await op(store, { op: "remove", id: draft.id });
    const after = await op(store, { op: "list" });
    expect(after.jobs).toEqual([]);
  });

  it("deduplicates Telegram update ids and caches group access", async () => {
    const store = new JobStore({ storage: new MemoryStorage() });
    expect((await op(store, { op: "seen", updateId: 7 })).seen).toBe(false);
    await op(store, { op: "markSeen", updateId: 7 });
    expect((await op(store, { op: "seen", updateId: 7 })).seen).toBe(true);

    await op(store, { op: "setGroup", chatId: -5, allowed: true, checkedAt: 1000 });
    const group = await op(store, { op: "getGroup", chatId: -5 });
    expect(group.group).toEqual({ allowed: true, checkedAt: 1000 });
  });

  it("rejects unknown operations", async () => {
    const store = new JobStore({ storage: new MemoryStorage() });
    const response = await store.fetch(
      new Request("https://store/op", {
        method: "POST",
        body: JSON.stringify({ op: "nope" }),
      }),
    );
    expect(response.status).toBe(400);
  });
});
