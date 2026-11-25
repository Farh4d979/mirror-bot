import type { Config } from "./config";
import type { JobRecord } from "./types";

/**
 * The GitHub Actions worker is Python and expects these exact keys. Keep this
 * in sync with `mirror.remote.build_payload` and `mirror.worker.job_from_payload`.
 */
export function buildPayload(
  job: JobRecord,
  config: Config,
): Record<string, unknown> {
  return {
    version: 1,
    job_id: job.id,
    user_id: job.userId,
    user_name: job.userName,
    chat_id: job.chatId,
    message_id: job.messageId,
    inline_message_id: job.inlineMessageId ?? null,
    source: job.source,
    source_kind: job.sourceKind,
    host: job.host,
    dump_partitions: job.dumpPartitions,
    file_id: job.fileId ?? null,
    file_name: job.fileName ?? null,
    file_size: job.fileSize ?? null,
    settings: {
      max_file_size: config.maxFileSize,
      job_timeout: config.jobTimeout,
      multi_file_mode: config.multiFileMode,
      ytdlp_format: config.ytdlpFormat,
      gofile_token: config.gofileToken,
      pixeldrain_api_key: config.pixeldrainApiKey,
    },
  };
}
