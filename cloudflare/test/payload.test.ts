import { describe, expect, it } from "vitest";
import type { Config } from "../src/config";
import { buildPayload } from "../src/payload";
import type { JobRecord } from "../src/types";

const config: Config = {
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
};

const job: JobRecord = {
  id: "abc12345",
  dispatchKey: 1,
  userId: 42,
  userName: "Test User",
  chatId: -100,
  messageId: 7,
  source: "https://example.com/file.bin",
  sourceKind: "url",
  host: "gofile",
  dumpPartitions: ["boot", "vendor"],
  runId: 123,
  runUrl: "https://github.com/owner/repo/actions/runs/123",
  createdAt: 100,
  startedAt: 100,
  state: "queued",
  detail: "Waiting…",
};

describe("GitHub Actions payload", () => {
  it("matches the Python worker's expected keys", () => {
    expect(buildPayload(job, config)).toEqual({
      version: 1,
      job_id: "abc12345",
      user_id: 42,
      user_name: "Test User",
      chat_id: -100,
      message_id: 7,
      inline_message_id: null,
      source: "https://example.com/file.bin",
      source_kind: "url",
      host: "gofile",
      dump_partitions: ["boot", "vendor"],
      file_id: null,
      file_name: null,
      file_size: null,
      settings: {
        max_file_size: 0,
        job_timeout: 7200,
        multi_file_mode: "largest",
        ytdlp_format: "bv*+ba/b",
        gofile_token: "",
        pixeldrain_api_key: "",
      },
    });
  });

  it("keeps Telegram attachment metadata", () => {
    const payload = buildPayload(
      {
        ...job,
        source: "animation_u1.mp4",
        sourceKind: "telegram_file",
        fileId: "file-1",
        fileName: "animation_u1.mp4",
        fileSize: 1234,
      },
      config,
    );
    expect(payload.source_kind).toBe("telegram_file");
    expect(payload.file_id).toBe("file-1");
    expect(payload.file_name).toBe("animation_u1.mp4");
    expect(payload.file_size).toBe(1234);
  });
});
