import { validateFernetKey } from "./fernet";
import type { Env } from "./types";
import { parseSize } from "./utils";

export interface Config {
  telegramToken: string;
  webhookSecret: string;
  githubToken: string;
  remoteJobKey: string;
  allowedUserIds: Set<number>;
  repo: string;
  ref: string;
  workflow: string;
  maxFileSize: number;
  jobTimeout: number;
  multiFileMode: "largest" | "zip";
  ytdlpFormat: string;
  gofileToken: string;
  pixeldrainApiKey: string;
  botUsername: string;
}

export class ConfigError extends Error {}

function required(env: Env, name: keyof Env): string {
  const value = String(env[name] || "").trim();
  if (!value) throw new ConfigError(`${String(name)} is not configured.`);
  return value;
}

function parseIds(value: string | undefined): Set<number> {
  const ids = new Set<number>();
  for (const part of String(value || "")
    .replace(/;/g, ",")
    .split(",")) {
    const text = part.trim();
    if (/^-?\d+$/.test(text)) ids.add(Number(text));
  }
  return ids;
}

export function loadConfig(env: Env): Config {
  const telegramToken = required(env, "TELEGRAM_BOT_TOKEN");
  const webhookSecret = required(env, "TELEGRAM_WEBHOOK_SECRET");
  const githubToken = required(env, "GITHUB_TOKEN");
  const remoteJobKey = required(env, "REMOTE_JOB_KEY");
  if (!validateFernetKey(remoteJobKey)) {
    throw new ConfigError("REMOTE_JOB_KEY is not a valid Fernet key.");
  }

  const repo = required(env, "ACTIONS_REPO");
  if (!repo.includes("/")) {
    throw new ConfigError("ACTIONS_REPO must be owner/name.");
  }

  const rawTimeout = Number.parseInt(String(env.JOB_TIMEOUT || "7200"), 10);
  const jobTimeout = Number.isFinite(rawTimeout) ? Math.max(60, rawTimeout) : 7200;
  const multiFileMode = String(env.MULTI_FILE_MODE || "largest").toLowerCase() === "zip"
    ? "zip"
    : "largest";

  return {
    telegramToken,
    webhookSecret,
    githubToken,
    remoteJobKey,
    allowedUserIds: parseIds(env.ALLOWED_USER_IDS),
    repo,
    ref: String(env.ACTIONS_REF || "main").trim() || "main",
    workflow: String(env.REMOTE_WORKFLOW || "job.yml").trim() || "job.yml",
    maxFileSize: parseSize(env.MAX_FILE_SIZE, 0),
    jobTimeout,
    multiFileMode,
    ytdlpFormat: String(env.YTDLP_FORMAT || "").trim() || "bv*+ba/b",
    gofileToken: String(env.GOFILE_TOKEN || "").trim(),
    pixeldrainApiKey: String(env.PIXELDRAIN_API_KEY || "").trim(),
    botUsername: String(env.BOT_USERNAME || "").replace(/^@/, "").trim(),
  };
}

/** Report configured secret names without exposing their values. */
export function configStatus(env: Env): {
  ready: boolean;
  missing: string[];
  invalid: string[];
} {
  const missing: string[] = [];
  const invalid: string[] = [];
  for (const name of [
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_WEBHOOK_SECRET",
    "GITHUB_TOKEN",
    "REMOTE_JOB_KEY",
    "ACTIONS_REPO",
  ] as const) {
    if (!String(env[name] || "").trim()) missing.push(name);
  }
  if (String(env.REMOTE_JOB_KEY || "").trim() && !validateFernetKey(env.REMOTE_JOB_KEY)) {
    invalid.push("REMOTE_JOB_KEY");
  }
  return { ready: missing.length === 0 && invalid.length === 0, missing, invalid };
}
