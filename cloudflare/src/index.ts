import { ConfigError, configStatus, loadConfig } from "./config";
import { GitHubAPI } from "./github";
import { handleUpdate } from "./handlers";
import { refreshJobs } from "./refresh";
import { JobStoreClient } from "./store";
import { TelegramAPI } from "./telegram";
import type { Env, ExecutionContextLike, TgUpdate } from "./types";

export { JobStore } from "./store";

function jsonResponse(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

function safeEqual(left: string, right: string): boolean {
  if (left.length !== right.length) return false;
  let difference = 0;
  for (let index = 0; index < left.length; index += 1) {
    difference |= left.charCodeAt(index) ^ right.charCodeAt(index);
  }
  return difference === 0;
}

function dependencies(env: Env) {
  const config = loadConfig(env);
  return {
    config,
    store: new JobStoreClient(env.JOBS),
    tg: new TelegramAPI(config.telegramToken),
    gh: new GitHubAPI({
      token: config.githubToken,
      repo: config.repo,
      ref: config.ref,
      workflow: config.workflow,
    }),
  };
}

async function refresh(env: Env): Promise<void> {
  let deps;
  try {
    deps = dependencies(env);
  } catch (error) {
    console.log(
      JSON.stringify({
        level: "error",
        msg: "Cannot refresh jobs; configuration is incomplete",
        error: error instanceof Error ? error.message : String(error),
      }),
    );
    return;
  }
  try {
    await refreshJobs(deps);
  } catch (error) {
    console.log(
      JSON.stringify({
        level: "error",
        msg: "Remote job refresh crashed",
        error: error instanceof Error ? error.message : String(error),
      }),
    );
  }
}

export default {
  async fetch(
    request: Request,
    env: Env,
    _ctx: ExecutionContextLike,
  ): Promise<Response> {
    const url = new URL(request.url);

    if (url.pathname === "/" && request.method === "GET") {
      return new Response("mirror-bot Cloudflare controller\n", {
        headers: { "content-type": "text/plain; charset=utf-8" },
      });
    }

    if (url.pathname === "/health" && request.method === "GET") {
      const status = configStatus(env);
      let activeJobs: number | null = null;
      if (status.ready) {
        try {
          activeJobs = (await new JobStoreClient(env.JOBS).list()).length;
        } catch {
          activeJobs = null;
        }
      }
      return jsonResponse({
        ok: status.ready,
        worker: "mirror-bot",
        mode: "cloudflare",
        ...status,
        active_jobs: activeJobs,
      });
    }

    if (url.pathname !== "/telegram") {
      return new Response("not found\n", { status: 404 });
    }
    if (request.method !== "POST") {
      return new Response("method not allowed\n", { status: 405 });
    }

    const expected = String(env.TELEGRAM_WEBHOOK_SECRET || "");
    const supplied = request.headers.get("x-telegram-bot-api-secret-token") || "";
    if (!expected || !safeEqual(expected, supplied)) {
      return new Response("forbidden\n", { status: 403 });
    }

    let update: TgUpdate;
    try {
      update = (await request.json()) as TgUpdate;
    } catch {
      return new Response("invalid update\n", { status: 400 });
    }
    if (!update || typeof update.update_id !== "number") {
      return new Response("invalid update\n", { status: 400 });
    }

    let deps;
    try {
      deps = dependencies(env);
    } catch (error) {
      console.log(
        JSON.stringify({
          level: "error",
          msg: "Cannot handle update; configuration is incomplete",
          update_id: update.update_id,
          error: error instanceof ConfigError ? error.message : String(error),
        }),
      );
      return new Response("configuration error\n", { status: 500 });
    }

    try {
      if (await deps.store.seen(update.update_id)) {
        return new Response("ok\n");
      }
      await handleUpdate(deps, update);
      await deps.store.markSeen(update.update_id);
      return new Response("ok\n");
    } catch (error) {
      console.log(
        JSON.stringify({
          level: "error",
          msg: "Unhandled update error",
          update_id: update.update_id,
          error: error instanceof Error ? error.message : String(error),
        }),
      );
      return new Response("update failed\n", { status: 500 });
    }
  },

  async scheduled(
    _controller: unknown,
    env: Env,
    ctx: ExecutionContextLike,
  ): Promise<void> {
    ctx.waitUntil(refresh(env));
  },
};
