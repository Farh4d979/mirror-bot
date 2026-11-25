import type { InlineKeyboard } from "./render";

export class TelegramError extends Error {
  constructor(
    public readonly method: string,
    message: string,
    public readonly code?: number,
  ) {
    super(message);
    this.name = "TelegramError";
  }
}

export type DeleteResult = "deleted" | "not_found" | "error";

interface ApiResponse<T> {
  ok: boolean;
  result?: T;
  description?: string;
  error_code?: number;
}

export class TelegramAPI {
  private readonly token: string;
  private readonly fetchImpl: typeof fetch;

  constructor(token: string, fetchImpl: typeof fetch = fetch) {
    this.token = token;
    this.fetchImpl = ((...args: Parameters<typeof fetch>) =>
      fetchImpl.call(globalThis, ...args)) as typeof fetch;
  }

  async call<T>(method: string, body: Record<string, unknown> = {}): Promise<T> {
    let response: Response;
    try {
      response = await this.fetchImpl(
        `https://api.telegram.org/bot${this.token}/${method}`,
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(body),
        },
      );
    } catch (error) {
      throw new TelegramError(
        method,
        `Telegram request failed: ${error instanceof Error ? error.message : String(error)}`,
      );
    }

    let data: ApiResponse<T> | null = null;
    try {
      data = (await response.json()) as ApiResponse<T>;
    } catch {
      // Fall through to a generic error below.
    }
    if (!response.ok || !data?.ok) {
      throw new TelegramError(
        method,
        data?.description || `Telegram returned HTTP ${response.status}.`,
        data?.error_code,
      );
    }
    return data.result as T;
  }

  async sendMessage(
    chatId: number,
    text: string,
    replyMarkup?: InlineKeyboard,
    replyToMessageId?: number,
  ): Promise<{ message_id: number }> {
    const body: Record<string, unknown> = {
      chat_id: chatId,
      text,
      parse_mode: "HTML",
      disable_web_page_preview: true,
    };
    if (replyMarkup) body.reply_markup = replyMarkup;
    if (replyToMessageId) body.reply_parameters = { message_id: replyToMessageId };
    return this.call<{ message_id: number }>("sendMessage", body);
  }

  async editMessageText(
    chatId: number,
    messageId: number,
    text: string,
    replyMarkup?: InlineKeyboard,
  ): Promise<void> {
    const body: Record<string, unknown> = {
      chat_id: chatId,
      message_id: messageId,
      text,
      parse_mode: "HTML",
      disable_web_page_preview: true,
    };
    if (replyMarkup) body.reply_markup = replyMarkup;
    try {
      await this.call("editMessageText", body);
    } catch (error) {
      if (
        error instanceof TelegramError &&
        /message is not modified/i.test(error.message)
      ) {
        return;
      }
      throw error;
    }
  }

  async editInlineMessageText(
    inlineMessageId: string,
    text: string,
    replyMarkup?: InlineKeyboard,
  ): Promise<void> {
    const body: Record<string, unknown> = {
      inline_message_id: inlineMessageId,
      text,
      parse_mode: "HTML",
      disable_web_page_preview: true,
    };
    if (replyMarkup) body.reply_markup = replyMarkup;
    try {
      await this.call("editMessageText", body);
    } catch (error) {
      if (
        error instanceof TelegramError &&
        /message is not modified/i.test(error.message)
      ) {
        return;
      }
      throw error;
    }
  }

  async deleteMessage(chatId: number, messageId: number): Promise<DeleteResult> {
    try {
      await this.call("deleteMessage", { chat_id: chatId, message_id: messageId });
      return "deleted";
    } catch (error) {
      if (error instanceof TelegramError) {
        if (/message to delete not found|message not found/i.test(error.message)) {
          return "not_found";
        }
        if (/message can't be deleted/i.test(error.message)) {
          return "error";
        }
      }
      return "error";
    }
  }

  async answerCallbackQuery(
    callbackQueryId: string,
    text?: string,
    showAlert = false,
  ): Promise<void> {
    const body: Record<string, unknown> = { callback_query_id: callbackQueryId };
    if (text) body.text = text;
    if (showAlert) body.show_alert = true;
    try {
      await this.call("answerCallbackQuery", body);
    } catch {
      // The query may have expired; that is not worth failing the update.
    }
  }

  async answerInlineQuery(
    inlineQueryId: string,
    results: Array<Record<string, unknown>>,
    options: { cacheTime?: number; isPersonal?: boolean } = {},
  ): Promise<void> {
    await this.call("answerInlineQuery", {
      inline_query_id: inlineQueryId,
      results,
      cache_time: options.cacheTime ?? 0,
      is_personal: options.isPersonal ?? true,
    });
  }

  async getChatMember(
    chatId: number,
    userId: number,
  ): Promise<{ status: string }> {
    return this.call<{ status: string }>("getChatMember", {
      chat_id: chatId,
      user_id: userId,
    });
  }

  async getMe(): Promise<{ id: number; username?: string }> {
    return this.call<{ id: number; username?: string }>("getMe");
  }
}
