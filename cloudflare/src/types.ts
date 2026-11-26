/** Shared types for the Cloudflare controller. */

export type SourceKind = "url" | "torrent_file" | "telegram_file";
export type Host = "gofile" | "pixeldrain";
export type JobState = "dispatching" | "queued" | "running" | "cancelling";

export interface JobRecord {
  id: string;
  dispatchKey: number;
  userId: number;
  userName: string;
  chatId: number;
  messageId: number;
  /** Set when the job card lives in an inline message instead of a chat. */
  inlineMessageId?: string | null;
  source: string;
  sourceKind: SourceKind;
  host: Host;
  dumpPartitions: string[];
  /** Pack the result into a maximum-compression 7z archive. */
  compress: boolean;
  fileId?: string | null;
  fileName?: string | null;
  fileSize?: number | null;
  runId: number;
  runUrl: string;
  createdAt: number;
  startedAt: number;
  state: JobState;
  detail: string;
  error?: string;
  finishedAt?: number;
}

export type NewJob = Omit<
  JobRecord,
  | "runId"
  | "runUrl"
  | "createdAt"
  | "startedAt"
  | "state"
  | "detail"
  | "error"
  | "finishedAt"
>;

export interface GroupCache {
  allowed: boolean;
  checkedAt: number;
}

/** An inline query waiting for its Start button to be pressed. */
export interface PendingInline {
  userId: number;
  source: string;
  host: Host;
  dumpPartitions: string[];
  compress?: boolean;
  createdAt: number;
}

/** The subset of the Durable Object storage API used by this project. */
export interface DurableObjectStorageLike {
  get<T>(key: string): Promise<T | undefined>;
  put<T>(key: string, value: T): Promise<void>;
}

export interface DurableObjectStateLike {
  storage: DurableObjectStorageLike;
}

export interface DurableObjectStubLike {
  fetch(input: string, init?: RequestInit): Promise<Response>;
}

export interface DurableObjectNamespaceLike {
  idFromName(name: string): unknown;
  get(id: unknown): DurableObjectStubLike;
}

export interface ExecutionContextLike {
  waitUntil(promise: Promise<unknown>): void;
}

export interface Env {
  TELEGRAM_BOT_TOKEN: string;
  TELEGRAM_WEBHOOK_SECRET: string;
  GITHUB_TOKEN: string;
  REMOTE_JOB_KEY: string;
  ALLOWED_USER_IDS?: string;
  ACTIONS_REPO?: string;
  ACTIONS_REF?: string;
  REMOTE_WORKFLOW?: string;
  GOFILE_TOKEN?: string;
  PIXELDRAIN_API_KEY?: string;
  MAX_FILE_SIZE?: string;
  JOB_TIMEOUT?: string;
  MULTI_FILE_MODE?: string;
  YTDLP_FORMAT?: string;
  BOT_USERNAME?: string;
  JOBS: DurableObjectNamespaceLike;
}

// ---------------------------------------------------------------- Telegram API
export interface TgUser {
  id: number;
  first_name: string;
  last_name?: string;
  username?: string;
  is_bot?: boolean;
}

export interface TgChat {
  id: number;
  type: "private" | "group" | "supergroup" | "channel";
  title?: string;
  username?: string;
}

export interface TgFile {
  file_id: string;
  file_unique_id: string;
  file_name?: string;
  mime_type?: string;
  file_size?: number;
  width?: number;
  height?: number;
  is_animated?: boolean;
  is_video?: boolean;
}

export interface TgMessage {
  message_id: number;
  date: number;
  chat: TgChat;
  from?: TgUser;
  text?: string;
  caption?: string;
  photo?: TgFile[];
  document?: TgFile;
  video?: TgFile;
  audio?: TgFile;
  voice?: TgFile;
  video_note?: TgFile;
  animation?: TgFile;
  sticker?: TgFile;
  reply_to_message?: TgMessage;
}

export interface TgCallbackQuery {
  id: string;
  from: TgUser;
  message?: TgMessage;
  inline_message_id?: string;
  data?: string;
}

export interface TgInlineQuery {
  id: string;
  from: TgUser;
  query: string;
  offset: string;
  chat_type?: string;
}

export interface TgChosenInlineResult {
  result_id: string;
  from: TgUser;
  query: string;
  inline_message_id?: string;
}

export interface TgUpdate {
  update_id: number;
  message?: TgMessage;
  callback_query?: TgCallbackQuery;
  inline_query?: TgInlineQuery;
  chosen_inline_result?: TgChosenInlineResult;
}
