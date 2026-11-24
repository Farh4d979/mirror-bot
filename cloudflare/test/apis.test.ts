import { describe, expect, it } from "vitest";
import { GitHubAPI } from "../src/github";
import { TelegramAPI } from "../src/telegram";

/**
 * Cloudflare Workers reject a bare `fetch(...)` call when it is invoked with a
 * class instance as `this` ("Illegal invocation"). Keep the API clients honest.
 */
function strictFetch(body: unknown, status = 200) {
  let calls = 0;
  const impl = function (
    this: unknown,
    _input: RequestInfo | URL,
    _init?: RequestInit,
  ): Promise<Response> {
    calls += 1;
    expect(this).toBe(globalThis);
    return Promise.resolve(
      new Response(JSON.stringify(body), {
        status,
        headers: { "content-type": "application/json" },
      }),
    );
  } as typeof fetch;
  return { impl, calls: () => calls };
}

describe("API clients", () => {
  it("calls Telegram fetch with the global receiver", async () => {
    const { impl, calls } = strictFetch({
      ok: true,
      result: { message_id: 7 },
    });
    const api = new TelegramAPI("123:test", impl);

    await expect(api.sendMessage(42, "hello")).resolves.toEqual({ message_id: 7 });
    expect(calls()).toBe(1);
  });

  it("calls GitHub fetch with the global receiver", async () => {
    const { impl, calls } = strictFetch({
      status: "completed",
      conclusion: "success",
      html_url: "https://github.com/example/run",
    });
    const api = new GitHubAPI(
      { token: "test", repo: "owner/repo", ref: "main", workflow: "job.yml" },
      impl,
    );

    await expect(api.view(55)).resolves.toEqual({
      status: "completed",
      conclusion: "success",
      htmlUrl: "https://github.com/example/run",
    });
    expect(calls()).toBe(1);
  });
});
