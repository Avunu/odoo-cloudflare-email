import { concatBytes, toHex } from "./util";
import type { ByteSource } from "./util";

// ---------------------------------------------------------------------------
// Request signing for the push to Odoo.
//
// Every delivery attempt carries X-Mail-Cloudflare-Timestamp (Unix seconds)
// and X-Mail-Cloudflare-Signature ("v1=" + hex HMAC-SHA256 over
// "<timestamp>." followed by the raw body bytes). Odoo recomputes the same
// HMAC with hmac.new(secret, f"{ts}.".encode() + body, sha256) and rejects
// anything older than ±300 s, so a captured request cannot be replayed later
// and a tampered body cannot pass. The "v1=" prefix leaves room to rotate the
// scheme without a flag day: a future "v2=" can be accepted alongside.
//
// The payload is bytes, not a string: the body is an RFC 5322 message that may
// contain arbitrary 8-bit content, and both sides must hash exactly the octets
// on the wire.
// ---------------------------------------------------------------------------

const enc = new TextEncoder();

/** Seconds since the Unix epoch, the granularity Odoo's `int(timestamp)` check expects. */
export function unixSeconds(now: number = Date.now()): number {
	return Math.floor(now / 1000);
}

/** The exact octets that are signed: UTF-8 `"<timestamp>."` immediately followed by the body. */
export function signaturePayload(timestamp: number, body: ByteSource): Uint8Array<ArrayBuffer> {
	return concatBytes(enc.encode(`${timestamp}.`), body);
}

export async function hmacSha256Hex(secret: string, message: ByteSource): Promise<string> {
	const key = await crypto.subtle.importKey(
		"raw",
		enc.encode(secret),
		{ name: "HMAC", hash: "SHA-256" },
		false,
		["sign"],
	);
	const signature = await crypto.subtle.sign("HMAC", key, message);
	return toHex(new Uint8Array(signature));
}

/** The value of the X-Mail-Cloudflare-Signature header for one attempt. */
export async function signBody(
	secret: string,
	timestamp: number,
	body: ByteSource,
): Promise<string> {
	return `v1=${await hmacSha256Hex(secret, signaturePayload(timestamp, body))}`;
}
