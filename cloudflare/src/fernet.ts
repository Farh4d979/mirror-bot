/**
 * Fernet-compatible payload encryption.
 *
 * The GitHub Actions worker decrypts these tokens with Python's
 * `cryptography.fernet.Fernet`, so this file must stay byte-compatible:
 * version 0x80, 8-byte timestamp, 16-byte IV, AES-128-CBC ciphertext,
 * HMAC-SHA256 over everything before the MAC.
 */

const VERSION = 0x80;
const KEY_BYTES = 32;
const IV_BYTES = 16;
const MAC_BYTES = 32;
const TIMESTAMP_BYTES = 8;
const encoder = new TextEncoder();
const decoder = new TextDecoder();

function base64UrlDecode(value: string): Uint8Array {
  const text = value.replace(/-/g, "+").replace(/_/g, "/");
  const padded = text + "=".repeat((4 - (text.length % 4)) % 4);
  const binary = atob(padded);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return bytes;
}

/**
 * Fernet expects the standard padded URL-safe base64 alphabet. Unlike JWT,
 * the trailing `=` characters must stay in the token.
 */
function base64UrlEncode(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_");
}

function bufferSource(bytes: Uint8Array): BufferSource {
  return bytes as unknown as BufferSource;
}

function concat(...parts: Uint8Array[]): Uint8Array {
  const length = parts.reduce((total, part) => total + part.length, 0);
  const output = new Uint8Array(length);
  let offset = 0;
  for (const part of parts) {
    output.set(part, offset);
    offset += part.length;
  }
  return output;
}

function decodeKey(key: string): Uint8Array {
  const text = key.trim();
  // Python's `cryptography.fernet.Fernet` expects the padded 44-character form.
  if (!/^[A-Za-z0-9_-]{43}=$/.test(text)) {
    throw new Error("REMOTE_JOB_KEY is not a valid Fernet key.");
  }
  let raw: Uint8Array;
  try {
    raw = base64UrlDecode(text);
  } catch {
    throw new Error("REMOTE_JOB_KEY is not a valid Fernet key.");
  }
  if (raw.length !== KEY_BYTES) {
    throw new Error("REMOTE_JOB_KEY is not a valid Fernet key.");
  }
  return raw;
}

export function validateFernetKey(key: string | undefined | null): boolean {
  try {
    decodeKey(String(key || ""));
    return true;
  } catch {
    return false;
  }
}

export async function encryptPayload(
  key: string,
  payload: Record<string, unknown>,
): Promise<string> {
  const rawKey = decodeKey(key);
  const signingKey = rawKey.slice(0, 16);
  const encryptionKey = rawKey.slice(16, 32);

  const timestamp = new Uint8Array(TIMESTAMP_BYTES);
  new DataView(timestamp.buffer).setBigUint64(
    0,
    BigInt(Math.floor(Date.now() / 1000)),
    false,
  );
  const iv = crypto.getRandomValues(new Uint8Array(IV_BYTES));
  const plaintext = encoder.encode(JSON.stringify(payload));

  const aesKey = await crypto.subtle.importKey(
    "raw",
    encryptionKey,
    { name: "AES-CBC" },
    false,
    ["encrypt"],
  );
  const ciphertext = new Uint8Array(
    await crypto.subtle.encrypt({ name: "AES-CBC", iv }, aesKey, plaintext),
  );
  const body = concat(new Uint8Array([VERSION]), timestamp, iv, ciphertext);

  const hmacKey = await crypto.subtle.importKey(
    "raw",
    signingKey,
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const mac = new Uint8Array(
    await crypto.subtle.sign("HMAC", hmacKey, bufferSource(body)),
  );
  return base64UrlEncode(concat(body, mac));
}

export async function decryptPayload(
  key: string,
  token: string,
): Promise<Record<string, unknown>> {
  const rawKey = decodeKey(key);
  const signingKey = rawKey.slice(0, 16);
  const encryptionKey = rawKey.slice(16, 32);

  let raw: Uint8Array;
  try {
    raw = base64UrlDecode(token.trim());
  } catch {
    throw new Error("Could not decrypt the job payload.");
  }
  if (
    raw.length < 1 + TIMESTAMP_BYTES + IV_BYTES + 16 + MAC_BYTES ||
    raw[0] !== VERSION
  ) {
    throw new Error("Could not decrypt the job payload.");
  }

  const body = raw.slice(0, raw.length - MAC_BYTES);
  const mac = raw.slice(raw.length - MAC_BYTES);
  const hmacKey = await crypto.subtle.importKey(
    "raw",
    signingKey,
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["verify"],
  );
  const valid = await crypto.subtle.verify(
    "HMAC",
    hmacKey,
    bufferSource(mac),
    bufferSource(body),
  );
  if (!valid) throw new Error("Could not decrypt the job payload.");

  const iv = raw.slice(1 + TIMESTAMP_BYTES, 1 + TIMESTAMP_BYTES + IV_BYTES);
  const ciphertext = raw.slice(1 + TIMESTAMP_BYTES + IV_BYTES, raw.length - MAC_BYTES);
  const aesKey = await crypto.subtle.importKey(
    "raw",
    encryptionKey,
    { name: "AES-CBC" },
    false,
    ["decrypt"],
  );

  let plaintext: ArrayBuffer;
  try {
    plaintext = await crypto.subtle.decrypt({ name: "AES-CBC", iv }, aesKey, ciphertext);
  } catch {
    throw new Error("Could not decrypt the job payload.");
  }

  let value: unknown;
  try {
    value = JSON.parse(decoder.decode(plaintext));
  } catch {
    throw new Error("Could not decrypt the job payload.");
  }
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("The job payload is not an object.");
  }
  return value as Record<string, unknown>;
}
