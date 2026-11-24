import { describe, expect, it } from "vitest";
import type { JobRecord } from "../src/types";
import {
  cancelKeyboard,
  emptyKeyboard,
  renderCancelled,
  renderDenied,
  renderFailed,
  renderJob,
  renderStart,
  renderStatus,
} from "../src/render";

function job(overrides: Partial<JobRecord> = {}): JobRecord {
  return {
    id: "abc12345",
    dispatchKey: 1,
    userId: 42,
    userName: "Fərhad",
    chatId: -100,
    messageId: 7,
    source: "https://example.com/file.bin",
    sourceKind: "url",
    host: "gofile",
    dumpPartitions: [],
    runId: 123,
    runUrl: "https://github.com/owner/repo/actions/runs/123",
    createdAt: 100,
    startedAt: 100,
    state: "queued",
    detail: "Waiting for a GitHub Actions runner…",
    ...overrides,
  };
}

describe("job cards", () => {
  it("renders the initial GitHub Actions card with a launcher tag", () => {
    const text = renderJob(job(), 130);
    expect(text).toContain("☁️ <b>GitHub Actions</b> · <code>abc12345</code>");
    expect(text).toContain("Waiting for a GitHub Actions runner…");
    expect(text).toContain('<a href="tg://user?id=42">Fərhad</a>');
    expect(text).toContain("📦 Gofile");
    expect(text).toContain("⏱ 30s");
  });

  it("escapes user names and failure reasons", () => {
    const failed = renderFailed(
      job({ userName: "A <B>", error: "bad <tag> & stuff", finishedAt: 160 }),
      "https://github.com/owner/repo/actions/runs/123?x=1&y=2",
      200,
    );
    expect(failed).toContain("A &lt;B&gt;");
    expect(failed).toContain("bad &lt;tag&gt; &amp; stuff");
    expect(failed).toContain("⏱ Failed after 1m 00s");
    expect(failed).toContain('href="https://github.com/owner/repo/actions/runs/123?x=1&amp;y=2"');
  });

  it("renders a cancelled card without a runtime link", () => {
    const text = renderCancelled(job({ finishedAt: 130 }), 200);
    expect(text).toContain("⏹️ <b>Job cancelled</b>");
    expect(text).toContain("⏱ Stopped after 30s");
    expect(text).not.toContain("Actions run");
  });

  it("renders status for active jobs", () => {
    const text = renderStatus([
      job({ id: "one", detail: "GitHub Actions · 2m 00s", state: "running" }),
      job({ id: "two", detail: "Queued." }),
    ]);
    expect(text).toContain("<b>Active jobs</b>");
    expect(text).toContain("<code>one</code>");
    expect(text).toContain("GitHub Actions · 2m 00s");
    expect(renderStatus([])).toBe("No active jobs.");
  });

  it("keeps the start text free of the PC-only handoff command", () => {
    const text = renderStart();
    expect(text).toContain("/gf");
    expect(text).toContain("/px");
    expect(text).toContain("-dump boot,vendor");
    expect(text).not.toContain("/handoff");
    expect(text).toContain("Cloudflare Worker");
  });

  it("renders the private-bot denial with the user id", () => {
    expect(renderDenied(42)).toContain("<code>42</code>");
  });

  it("builds cancel and empty keyboards", () => {
    expect(cancelKeyboard("abc").inline_keyboard[0][0].callback_data).toBe(
      "cancel:abc",
    );
    expect(emptyKeyboard()).toEqual({ inline_keyboard: [] });
  });
});
