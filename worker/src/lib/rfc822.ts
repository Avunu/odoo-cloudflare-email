import { concatBytes } from "./util";
import type { ByteSource } from "./util";

// ---------------------------------------------------------------------------
// The little RFC 5322 the worker needs: adding envelope headers to a message
// it otherwise treats as opaque bytes.
//
// Email Routing hands the worker the SMTP envelope (`from`, `to`) separately
// from the message; the message itself usually names neither — a list post or
// a Bcc carries the routed address nowhere in its headers. Odoo's
// message_parse builds the recipient list from Delivered-To and the bounce
// check from Return-Path, so the envelope is written into the stored message
// exactly the way a delivering MTA would, before anything is stored or sent.
// ---------------------------------------------------------------------------

/** `[name, value]`, rendered as `name: value\r\n`. */
export type HeaderPair = readonly [name: string, value: string];

/** RFC 5322 §2.1.1: a line, excluding its CRLF, is at most 998 octets. */
const MAX_LINE_OCTETS = 998;

const enc = new TextEncoder();

/** An envelope value that cannot be written into a header line. */
export class EnvelopeError extends Error {
	constructor(message: string) {
		super(message);
		this.name = "EnvelopeError";
	}
}

/**
 * Throw unless `name: value` is a single, well-formed header line. A CR or LF in an envelope
 * address would let the sender inject headers into the stored message (and the signature would then
 * vouch for them); NUL and the other control characters have no place in a header either. The check
 * is on the full line so the 998-octet limit is measured the way an MTA measures it.
 */
export function assertHeaderSafe(name: string, value: string): void {
	for (const ch of value) {
		const code = ch.codePointAt(0) ?? 0;
		if (code < 0x20 || code === 0x7f) {
			throw new EnvelopeError(`${name} contains a control character`);
		}
	}
	if (enc.encode(`${name}: ${value}`).length > MAX_LINE_OCTETS) {
		throw new EnvelopeError(`${name} exceeds ${MAX_LINE_OCTETS} octets`);
	}
}

/**
 * The two headers a delivering MTA adds. Return-Path takes the angle-bracket form RFC 5321 §4.4
 * prescribes, with `<>` for the null reverse-path a bounce arrives with — Odoo's bounce handling
 * keys on exactly that.
 */
export function envelopeHeaders(from: string, to: string): HeaderPair[] {
	return [
		["Delivered-To", to],
		["Return-Path", from === "" ? "<>" : `<${from}>`],
	];
}

/**
 * Prepend header lines to a raw message, in the order given. The message is never decoded — an
 * attachment's bytes must come out exactly as they went in — so the header block is encoded
 * separately and joined with a single copy. Every pair is validated first, so an unsafe value
 * leaves nothing half-built.
 */
export function prependHeaders(
	raw: ByteSource,
	headers: readonly HeaderPair[],
): Uint8Array<ArrayBuffer> {
	let block = "";
	for (const [name, value] of headers) {
		assertHeaderSafe(name, value);
		block += `${name}: ${value}\r\n`;
	}
	return concatBytes(enc.encode(block), raw);
}

/** Longest Message-ID kept as R2 metadata; anything longer is truncated, never dropped. */
const MAX_MESSAGE_ID = 256;

/**
 * The message's own Message-ID, made safe for R2 custom metadata (which must be printable ASCII):
 * trimmed, non-ASCII stripped, capped at 256 characters. Null when the header is absent or has
 * nothing usable left. Angle brackets are kept so the value matches what Odoo stores verbatim.
 */
export function messageIdOf(headers: Headers): string | null {
	const header = headers.get("Message-ID");
	if (header === null) {
		return null;
	}
	let out = "";
	for (const ch of header.trim()) {
		const code = ch.codePointAt(0) ?? 0;
		if (code >= 0x20 && code < 0x7f) {
			out += ch;
		}
	}
	const id = out.slice(0, MAX_MESSAGE_ID);
	return id === "" ? null : id;
}
