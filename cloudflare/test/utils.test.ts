import { describe, expect, it } from "vitest";
import type { TgMessage } from "../src/types";
import {
  DumpError,
  attachmentFilename,
  extractUrl,
  messageAttachment,
  normalizePartitions,
  parseDumpArgs,
  sanitizeFilename,
} from "../src/utils";

const chat = { id: 1, type: "private" as const };

function message(overrides: Partial<TgMessage>): TgMessage {
  return {
    message_id: 1,
    date: 0,
    chat,
    from: { id: 42, first_name: "Test" },
    ...overrides,
  };
}

describe("URL extraction", () => {
  it("finds http, https and magnet links", () => {
    expect(extractUrl("mirror https://example.com/a.bin now")).toBe(
      "https://example.com/a.bin",
    );
    expect(extractUrl("magnet:?xt=urn:btih:abc")).toBe("magnet:?xt=urn:btih:abc");
  });

  it("strips trailing punctuation", () => {
    expect(extractUrl("(https://example.com/a.bin).")).toBe(
      "https://example.com/a.bin",
    );
  });

  it("returns null when there is no link", () => {
    expect(extractUrl("hello")).toBeNull();
    expect(extractUrl("")).toBeNull();
  });
});

describe("filename handling", () => {
  it("sanitizes Windows-illegal characters and reserved names", () => {
    expect(sanitizeFilename("CON")).toBe("_CON");
    expect(sanitizeFilename("a<b>c?.zip")).toBe("a_b_c_.zip");
    expect(sanitizeFilename("../../etc/passwd")).toBe("_._etc_passwd");
  });

  it("uses a document name when present", () => {
    const attachment = {
      file_id: "f1",
      file_unique_id: "u1",
      file_name: "My Report.pdf",
    };
    expect(attachmentFilename(message({}), attachment)).toBe("My Report.pdf");
  });

  it("does not turn an unnamed animation document into a torrent", () => {
    const animation = {
      file_id: "f2",
      file_unique_id: "u2",
      mime_type: "video/mp4",
    };
    const msg = message({ document: animation, animation });
    expect(messageAttachment(msg)).toBe(animation);
    expect(attachmentFilename(msg, animation)).toBe("animation_u2.mp4");
  });

  it("names voice notes and stickers by their real format", () => {
    const voice = { file_id: "v", file_unique_id: "voice1", mime_type: "audio/ogg" };
    expect(attachmentFilename(message({ voice }), voice)).toBe("voice_voice1.ogg");

    const sticker = {
      file_id: "s",
      file_unique_id: "sticker1",
      is_animated: true,
    };
    expect(attachmentFilename(message({ sticker }), sticker)).toBe(
      "sticker_sticker1.tgs",
    );
  });

  it("picks the largest photo", () => {
    const small = { file_id: "a", file_unique_id: "a", width: 100, height: 100 };
    const large = { file_id: "b", file_unique_id: "b", width: 800, height: 800 };
    expect(messageAttachment(message({ photo: [small, large] }))).toBe(large);
  });
});

describe("dump arguments", () => {
  it("parses comma and space separated partitions", () => {
    const parsed = parseDumpArgs(["-dump", "boot,vendor", "system"]);
    expect(parsed.requested).toBe(true);
    expect(parsed.values).toEqual(["boot,vendor", "system"]);
    expect(normalizePartitions(parsed.values)).toEqual(["boot", "vendor", "system"]);
  });

  it("parses --dump=value and strips .img", () => {
    const parsed = parseDumpArgs(["--dump=boot.img,vendor.img"]);
    expect(parsed.values).toEqual(["boot.img,vendor.img"]);
    expect(normalizePartitions(parsed.values)).toEqual(["boot", "vendor"]);
  });

  it("stops collecting at another flag", () => {
    const parsed = parseDumpArgs(["-dump", "boot", "-x", "vendor"]);
    expect(parsed.values).toEqual(["boot"]);
    expect(parsed.remaining).toEqual(["-x", "vendor"]);
  });

  it("rejects unsafe partition names", () => {
    expect(() => normalizePartitions(["../boot"])).toThrow(DumpError);
    expect(() => normalizePartitions(["boot/../../x"])).toThrow(DumpError);
  });
});
