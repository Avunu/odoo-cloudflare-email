const enc = new TextEncoder();

/** Anything the byte-oriented helpers accept: a raw buffer or a view over one. */
export type ByteSource = ArrayBuffer | Uint8Array;

export function toHex(bytes: Uint8Array): string {
	let out = "";
	for (const b of bytes) {
		out += b.toString(16).padStart(2, "0");
	}
	return out;
}

/**
 * Constant-time equality for two strings. Used to compare secret-derived hashes/tokens. Length is
 * allowed to leak (the compared values are fixed-length hashes/handles), but content comparison
 * takes time independent of where the first differing byte is.
 */
export function timingSafeEqual(a: string, b: string): boolean {
	const ab = enc.encode(a);
	const bb = enc.encode(b);
	if (ab.length !== bb.length) {
		return false;
	}
	let diff = 0;
	for (const [i, x] of ab.entries()) {
		diff |= x ^ (bb[i] ?? 0);
	}
	return diff === 0;
}

/**
 * Join byte sequences into one freshly allocated buffer. A single allocation + `set` per part is
 * the cheapest way to build "header block ++ 25 MiB message" without ever decoding the message as
 * text, which would corrupt binary attachments.
 */
export function concatBytes(...parts: readonly ByteSource[]): Uint8Array<ArrayBuffer> {
	const views = parts.map((part) => (part instanceof Uint8Array ? part : new Uint8Array(part)));
	let total = 0;
	for (const view of views) {
		total += view.length;
	}
	const out = new Uint8Array(total);
	let offset = 0;
	for (const view of views) {
		out.set(view, offset);
		offset += view.length;
	}
	return out;
}

// ---------------------------------------------------------------------------
// ULID — https://github.com/ulid/spec
//
// 26 characters of Crockford base32: 10 for a 48-bit millisecond timestamp and
// 16 for 80 bits of randomness. Chosen over UUID for message ids because it
// sorts by arrival time (an R2 list and an ops listing come back in the order
// the mail was received) and is safe in URLs and object keys without escaping.
// Ids created within the same millisecond are unordered relative to each other —
// the spec's optional monotonic extension is deliberately not implemented, as
// nothing here depends on it and it would require cross-request state.
// ---------------------------------------------------------------------------

/** Crockford's alphabet: digits + uppercase letters minus I, L, O and U. */
const ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";
const ULID_TIME_CHARS = 10;
const ULID_RANDOM_BYTES = 10;
/** 2^48 - 1: the largest timestamp ten base32 characters (with a leading 0–7) can carry. */
const ULID_MAX_TIME = 281_474_976_710_655;

/**
 * Matches exactly one ULID. The first character is 0–7 because 2^48 - 1 encodes to `7ZZZZZZZZZ`.
 * Exported so the ops router can validate a path segment before it reaches SQL or R2.
 */
export const ULID_PATTERN = /^[0-7][0-9A-HJKMNP-TV-Z]{25}$/;

/**
 * The timestamp exceeds 32 bits, so it is split with arithmetic rather than shifts — JavaScript's
 * bitwise operators truncate to 32 bits and would silently corrupt the high-order characters.
 */
function encodeTime(ms: number): string {
	let remaining = ms;
	let out = "";
	for (let i = 0; i < ULID_TIME_CHARS; i++) {
		out = `${ULID_ALPHABET.charAt(remaining % 32)}${out}`;
		remaining = Math.floor(remaining / 32);
	}
	return out;
}

/** Pack 10 random bytes into 16 characters, five bits at a time (80 is a multiple of 5). */
function encodeRandom(bytes: Uint8Array): string {
	let out = "";
	let acc = 0;
	let bits = 0;
	for (const byte of bytes) {
		acc = (acc << 8) | byte;
		bits += 8;
		while (bits >= 5) {
			bits -= 5;
			out += ULID_ALPHABET.charAt((acc >>> bits) & 31);
		}
		// Drop the bits already emitted so the accumulator never grows past 32 bits.
		acc &= (1 << bits) - 1;
	}
	return out;
}

/** A new ULID for `now` (milliseconds since the Unix epoch; defaults to the current time). */
export function ulid(now: number = Date.now()): string {
	if (!Number.isInteger(now) || now < 0 || now > ULID_MAX_TIME) {
		throw new RangeError("ulid timestamp must be an integer between 0 and 2^48 - 1 ms");
	}
	const random = crypto.getRandomValues(new Uint8Array(ULID_RANDOM_BYTES));
	return `${encodeTime(now)}${encodeRandom(random)}`;
}

export type LogLevel = "debug" | "info" | "warn" | "error";

/**
 * One JSON line per event, so Workers Logs can filter on `event` and any field. Callers pass only
 * metadata — ids, addresses, statuses, counts. Never a message body, a secret, or the inbound URL
 * (it embeds the per-server key).
 */
export function logEvent(
	level: LogLevel,
	event: string,
	fields: Record<string, unknown> = {},
): void {
	console[level](JSON.stringify({ event, ...fields }));
}
