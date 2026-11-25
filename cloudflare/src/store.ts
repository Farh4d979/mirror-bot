import type {
  DurableObjectNamespaceLike,
  DurableObjectStateLike,
  GroupCache,
  JobRecord,
  NewJob,
} from "./types";

interface StoreState {
  jobs: JobRecord[];
  seen: number[];
  groups: Record<string, GroupCache>;
}

const EMPTY_STATE: StoreState = { jobs: [], seen: [], groups: {} };
const MAX_SEEN = 1000;

function nowSeconds(): number {
  return Math.floor(Date.now() / 1000);
}

function snapshot(state: StoreState): StoreState {
  return {
    jobs: Array.isArray(state.jobs) ? state.jobs : [],
    seen: Array.isArray(state.seen) ? state.seen : [],
    groups: state.groups && typeof state.groups === "object" ? state.groups : {},
  };
}

/** Serializable operations used by both the Worker and the Durable Object. */
export interface StoreLike {
  seen(updateId: number): Promise<boolean>;
  markSeen(updateId: number): Promise<void>;
  reserve(job: NewJob): Promise<{ job: JobRecord; created: boolean }>;
  setMessage(id: string, messageId: number): Promise<JobRecord | null>;
  setInlineMessage(id: string, inlineMessageId: string): Promise<JobRecord | null>;
  attachRun(id: string, runId: number, runUrl: string): Promise<JobRecord | null>;
  update(id: string, patch: Partial<JobRecord>): Promise<JobRecord | null>;
  remove(id: string): Promise<void>;
  get(id: string): Promise<JobRecord | null>;
  list(): Promise<JobRecord[]>;
  listForUser(userId: number): Promise<JobRecord[]>;
  getGroup(chatId: number): Promise<GroupCache | null>;
  setGroup(chatId: number, allowed: boolean, checkedAt: number): Promise<void>;
}

export class JobStore {
  private chain: Promise<unknown> = Promise.resolve();

  constructor(private readonly ctx: DurableObjectStateLike) {}

  private serialize<T>(fn: () => Promise<T>): Promise<T> {
    const result = this.chain.then(fn, fn);
    this.chain = result.then(
      () => undefined,
      () => undefined,
    );
    return result;
  }

  private async load(): Promise<StoreState> {
    const stored = await this.ctx.storage.get<StoreState>("state");
    return stored ? snapshot(stored) : { jobs: [], seen: [], groups: {} };
  }

  private async save(state: StoreState): Promise<void> {
    await this.ctx.storage.put("state", state);
  }

  async fetch(request: Request): Promise<Response> {
    if (request.method !== "POST") {
      return Response.json({ ok: false, error: "POST required" }, { status: 405 });
    }

    let body: Record<string, unknown>;
    try {
      body = (await request.json()) as Record<string, unknown>;
    } catch {
      return Response.json({ ok: false, error: "Invalid JSON" }, { status: 400 });
    }

    try {
      const result = await this.serialize(() => this.handle(body));
      return Response.json({ ok: true, ...result });
    } catch (error) {
      return Response.json(
        { ok: false, error: error instanceof Error ? error.message : String(error) },
        { status: 400 },
      );
    }
  }

  private async handle(body: Record<string, unknown>): Promise<Record<string, unknown>> {
    const state = await this.load();
    const op = String(body.op || "");
    const find = (id: unknown): JobRecord | undefined =>
      state.jobs.find((job) => job.id === String(id || ""));

    switch (op) {
      case "list":
        return { jobs: state.jobs };

      case "get":
        return { job: find(body.id) || null };

      case "listForUser":
        return {
          jobs: state.jobs.filter((job) => job.userId === Number(body.userId)),
        };

      case "reserve": {
        const incoming = body.job as NewJob | undefined;
        if (!incoming || !incoming.id || !incoming.dispatchKey) {
          throw new Error("Invalid job draft.");
        }
        const existing = state.jobs.find(
          (job) => job.dispatchKey === Number(incoming.dispatchKey),
        );
        if (existing) return { job: existing, created: false };
        if (find(incoming.id)) throw new Error("Duplicate job id.");

        const createdAt = nowSeconds();
        const job: JobRecord = {
          ...incoming,
          runId: 0,
          runUrl: "",
          createdAt,
          startedAt: createdAt,
          state: "dispatching",
          detail: "Starting a GitHub Actions runner…",
        };
        state.jobs.push(job);
        await this.save(state);
        return { job, created: true };
      }

      case "setMessage": {
        const job = find(body.id);
        if (!job) return { job: null };
        job.messageId = Number(body.messageId || 0);
        await this.save(state);
        return { job };
      }

      case "setInlineMessage": {
        const job = find(body.id);
        if (!job) return { job: null };
        job.inlineMessageId = String(body.inlineMessageId || "");
        await this.save(state);
        return { job };
      }

      case "attachRun": {
        const job = find(body.id);
        if (!job) return { job: null };
        job.runId = Number(body.runId || 0);
        job.runUrl = String(body.runUrl || "");
        job.state = "queued";
        job.detail = "Waiting for a GitHub Actions runner…";
        await this.save(state);
        return { job };
      }

      case "update": {
        const job = find(body.id);
        if (!job) return { job: null };
        const patch = (body.patch || {}) as Partial<JobRecord>;
        const allowed: (keyof JobRecord)[] = [
          "state",
          "detail",
          "runId",
          "runUrl",
          "error",
          "finishedAt",
          "messageId",
          "inlineMessageId",
        ];
        for (const key of allowed) {
          if (key in patch) {
            (job as unknown as Record<string, unknown>)[key] = patch[key];
          }
        }
        await this.save(state);
        return { job };
      }

      case "remove": {
        const before = state.jobs.length;
        state.jobs = state.jobs.filter((job) => job.id !== String(body.id || ""));
        if (state.jobs.length !== before) await this.save(state);
        return {};
      }

      case "seen":
        return { seen: state.seen.includes(Number(body.updateId)) };

      case "markSeen": {
        const updateId = Number(body.updateId);
        if (Number.isFinite(updateId) && !state.seen.includes(updateId)) {
          state.seen.push(updateId);
          if (state.seen.length > MAX_SEEN) {
            state.seen = state.seen.slice(state.seen.length - MAX_SEEN);
          }
          await this.save(state);
        }
        return {};
      }

      case "getGroup":
        return { group: state.groups[String(body.chatId)] || null };

      case "setGroup": {
        const checkedAt = Number(body.checkedAt || nowSeconds());
        state.groups[String(body.chatId)] = {
          allowed: Boolean(body.allowed),
          checkedAt,
        };
        const cutoff = checkedAt - 24 * 60 * 60;
        for (const [key, value] of Object.entries(state.groups)) {
          if (value.checkedAt < cutoff) delete state.groups[key];
        }
        await this.save(state);
        return {};
      }

      default:
        throw new Error(`Unknown store operation: ${op}`);
    }
  }
}

export class JobStoreClient implements StoreLike {
  private readonly stub;

  constructor(namespace: DurableObjectNamespaceLike) {
    this.stub = namespace.get(namespace.idFromName("coordinator"));
  }

  private async op<T>(op: string, args: Record<string, unknown> = {}): Promise<T> {
    const response = await this.stub.fetch("https://store/op", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ op, ...args }),
    });
    const data = (await response.json().catch(() => null)) as
      | ({ ok: boolean; error?: string } & Record<string, unknown>)
      | null;
    if (!response.ok || !data?.ok) {
      throw new Error(data?.error || `Durable Object ${op} failed.`);
    }
    return data as T;
  }

  async seen(updateId: number): Promise<boolean> {
    return (await this.op<{ seen: boolean }>("seen", { updateId })).seen;
  }

  async markSeen(updateId: number): Promise<void> {
    await this.op("markSeen", { updateId });
  }

  async reserve(job: NewJob): Promise<{ job: JobRecord; created: boolean }> {
    return this.op<{ job: JobRecord; created: boolean }>("reserve", { job });
  }

  async setMessage(id: string, messageId: number): Promise<JobRecord | null> {
    return (await this.op<{ job: JobRecord | null }>("setMessage", { id, messageId })).job;
  }

  async setInlineMessage(
    id: string,
    inlineMessageId: string,
  ): Promise<JobRecord | null> {
    return (
      await this.op<{ job: JobRecord | null }>("setInlineMessage", {
        id,
        inlineMessageId,
      })
    ).job;
  }

  async attachRun(
    id: string,
    runId: number,
    runUrl: string,
  ): Promise<JobRecord | null> {
    return (await this.op<{ job: JobRecord | null }>("attachRun", {
      id,
      runId,
      runUrl,
    })).job;
  }

  async update(id: string, patch: Partial<JobRecord>): Promise<JobRecord | null> {
    return (await this.op<{ job: JobRecord | null }>("update", { id, patch })).job;
  }

  async remove(id: string): Promise<void> {
    await this.op("remove", { id });
  }

  async get(id: string): Promise<JobRecord | null> {
    return (await this.op<{ job: JobRecord | null }>("get", { id })).job;
  }

  async list(): Promise<JobRecord[]> {
    return (await this.op<{ jobs: JobRecord[] }>("list")).jobs;
  }

  async listForUser(userId: number): Promise<JobRecord[]> {
    return (await this.op<{ jobs: JobRecord[] }>("listForUser", { userId })).jobs;
  }

  async getGroup(chatId: number): Promise<GroupCache | null> {
    return (await this.op<{ group: GroupCache | null }>("getGroup", { chatId })).group;
  }

  async setGroup(chatId: number, allowed: boolean, checkedAt: number): Promise<void> {
    await this.op("setGroup", { chatId, allowed, checkedAt });
  }
}
