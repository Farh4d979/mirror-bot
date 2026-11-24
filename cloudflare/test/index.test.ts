import { afterEach, describe, expect, it, vi } from "vitest";
import worker from "../src/index";
import { JobStore } from "../src/store";
import type { DurableObjectStorageLike, Env } from "../src/types";

class MemoryStorage implements DurableObjectStorageLike {
  private readonly values = new Map<string, unknown>();
  async get<T>(key: string): Promise<T | undefined> {
    return this.values.get(key) as T | undefined;
  }
  async put<T>(key: string, value: T): Promise<void> {
    this.values.set(key, value);
  }
}

function namespace() {
  const object = new JobStore({ storage: new MemoryStorage() });
  return {
    idFromName: (name: string) => name,
    get: () => ({
      fetch: (input: string, init?: RequestInit) =>
        object.fetch(new Request(input, init)),
    }),
  };
}

const KEY = "9STVjW3ysshly5_FpGoEwxYEnvtYUNMIYEFe8jwg_jM=";

function env(overrides: Partial<Env> = {}): Env {
  return {
    TELEGRAM_BOT_TOKEN: "123:test",
    TELEGRAM_WEBHOOK_SECRET: "webhook-secret",
    GITHUB_TOKEN: "github-token",
    REMOTE_JOB_KEY: KEY,
    ALLOWED_USER_IDS: "42",
    ACTIONS_REPO: "Farh4d979/mirror-bot",
    ACTIONS_REF: "main",
    REMOTE_WORKFLOW: "job.yml",
    JOBS: namespace(),
    ...overrides,
  } as Env;
}

const ctx = { waitUntil: () => {} };

function webhookRequest(update: unknown, secret = "webhook-secret"): Request {
  return new Request("https://bot.example.com/telegram", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-telegram-bot-api-secret-token": secret,
    },
    body: JSON.stringify(update),
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("Worker routes", () => {
  it("serves a plain-text root", async () => {
    const response = await worker.fetch(
      new Request("https://bot.example.com/"),
      env(),
      ctx,
    );
    expect(response.status).toBe(200);
    expect(await response.text()).toContain("mirror-bot Cloudflare controller");
  });

  it("reports missing configuration on /health without leaking values", async () => {
    const response = await worker.fetch(
      new Request("https://bot.example.com/health"),
      env({ TELEGRAM_BOT_TOKEN: "", GITHUB_TOKEN: "", REMOTE_JOB_KEY: "" }),
      ctx,
    );
    const body = (await response.json()) as {
      ok: boolean;
      missing: string[];
      active_jobs: number | null;
    };
    expect(response.status).toBe(200);
    expect(body.ok).toBe(false);
    expect(body.missing).toEqual([
      "TELEGRAM_BOT_TOKEN",
      "GITHUB_TOKEN",
      "REMOTE_JOB_KEY",
    ]);
    expect(body.active_jobs).toBeNull();
    expect(JSON.stringify(body)).not.toContain("github-token");
  });

  it("reports a ready worker and the active job count", async () => {
    const response = await worker.fetch(
      new Request("https://bot.example.com/health"),
      env(),
      ctx,
    );
    const body = (await response.json()) as {
      ok: boolean;
      ready: boolean;
      active_jobs: number;
    };
    expect(body.ok).toBe(true);
    expect(body.ready).toBe(true);
    expect(body.active_jobs).toBe(0);
  });

  it("returns 404 for unknown paths and 405 for non-POST webhooks", async () => {
    expect(
      (await worker.fetch(new Request("https://bot.example.com/nope"), env(), ctx))
        .status,
    ).toBe(404);
    expect(
      (await worker.fetch(new Request("https://bot.example.com/telegram"), env(), ctx))
        .status,
    ).toBe(405);
  });

  it("rejects a webhook without the secret header", async () => {
    const response = await worker.fetch(
      webhookRequest({ update_id: 1 }, "wrong-secret"),
      env(),
      ctx,
    );
    expect(response.status).toBe(403);
  });

  it("rejects malformed updates", async () => {
    const response = await worker.fetch(
      new Request("https://bot.example.com/telegram", {
        method: "POST",
        headers: {
          "x-telegram-bot-api-secret-token": "webhook-secret",
        },
        body: "not json",
      }),
      env(),
      ctx,
    );
    expect(response.status).toBe(400);
  });

  it("handles a valid update and deduplicates retries", async () => {
    const calls: Array<{ url: string; body: Record<string, unknown> }> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        calls.push({
          url: String(input),
          body: JSON.parse(String(init?.body || "{}")) as Record<string, unknown>,
        });
        return Response.json({ ok: true, result: { message_id: 1 } });
      }),
    );

    const update = {
      update_id: 501,
      message: {
        message_id: 1,
        date: 0,
        chat: { id: 42, type: "private" },
        from: { id: 42, first_name: "Owner" },
        text: "/id",
      },
    };

    const sharedEnv = env();
    const first = await worker.fetch(webhookRequest(update), sharedEnv, ctx);
    const second = await worker.fetch(webhookRequest(update), sharedEnv, ctx);

    expect(first.status).toBe(200);
    expect(second.status).toBe(200);
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toContain("sendMessage");
    expect(String(calls[0].body.text)).toContain("<code>42</code>");
  });

  it("returns 500 when the webhook is called without required secrets", async () => {
    const response = await worker.fetch(
      webhookRequest({ update_id: 2 }),
      env({ TELEGRAM_BOT_TOKEN: "" }),
      ctx,
    );
    expect(response.status).toBe(500);
  });
});
