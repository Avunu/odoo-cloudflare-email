import { describe, it, expect } from "vitest";
import {
	EnvelopeError,
	assertHeaderSafe,
	envelopeHeaders,
	messageIdOf,
	prependHeaders,
} from "../../src/lib/rfc822";

const enc = new TextEncoder();
const dec = new TextDecoder();

// Built at runtime rather than written as escapes: the formatter rewrites `\u0000` and friends
// into literal bytes, and a NUL in a source file is exactly what these tests exist to reject.
const NUL = String.fromCodePoint(0);
const UNIT_SEP = String.fromCodePoint(0x1f);
const DEL = String.fromCodePoint(0x7f);

const RAW = enc.encode(
	"From: Alice <alice@example.com>\r\nSubject: hi\r\nMessage-ID: <a@example.com>\r\n\r\nbody\r\n",
);
const ENVELOPE_BLOCK =
	"Delivered-To: support@erp.example.com\r\nReturn-Path: <alice@example.com>\r\n";

/** Every byte value once, so a corrupting decode/encode round trip cannot hide. */
function allBytes(): Uint8Array<ArrayBuffer> {
	return new Uint8Array(Array.from({ length: 256 }, (_, i) => i));
}

describe("envelopeHeaders", () => {
	it("yields Delivered-To then Return-Path in angle brackets", () => {
		expect(envelopeHeaders("alice@example.com", "support@erp.example.com")).toEqual([
			["Delivered-To", "support@erp.example.com"],
			["Return-Path", "<alice@example.com>"],
		]);
	});

	it("writes the null reverse-path for an empty sender (a bounce)", () => {
		expect(envelopeHeaders("", "support@erp.example.com")).toEqual([
			["Delivered-To", "support@erp.example.com"],
			["Return-Path", "<>"],
		]);
	});
});

describe("prependHeaders", () => {
	it("puts the lines first, in order, CRLF-terminated, then the untouched message", () => {
		const out = prependHeaders(
			RAW,
			envelopeHeaders("alice@example.com", "support@erp.example.com"),
		);
		const text = dec.decode(out);
		expect(text.startsWith(`${ENVELOPE_BLOCK}From: `)).toBe(true);
		expect(out.subarray(-RAW.length)).toEqual(RAW);
		expect(out.length).toBe(RAW.length + enc.encode(ENVELOPE_BLOCK).length);
		// No bare LF anywhere in the block we produced.
		expect(text.slice(0, text.length - RAW.length).replaceAll("\r\n", "")).not.toContain("\n");
	});

	it("keeps binary content byte-for-byte and accepts an ArrayBuffer", () => {
		const raw = allBytes();
		const fromView = prependHeaders(raw, [["Delivered-To", "x@example.com"]]);
		const fromBuffer = prependHeaders(raw.buffer, [["Delivered-To", "x@example.com"]]);
		expect(fromView).toEqual(fromBuffer);
		expect([...fromView.subarray(-256)]).toEqual([...raw]);
		// The input is not mutated and the output is a fresh buffer.
		expect([...raw]).toEqual([...allBytes()]);
		expect(fromView.buffer).not.toBe(raw.buffer);
	});

	it("returns a copy of the message when there is nothing to prepend", () => {
		const out = prependHeaders(RAW, []);
		expect(out).toEqual(RAW);
		expect(out.buffer).not.toBe(RAW.buffer);
	});

	it("rejects header injection before writing anything", () => {
		const attempts = [
			"victim@example.com\r\nBcc: attacker@evil.test",
			"victim@example.com\nX-Injected: 1",
			"victim@example.com\r",
			`victim@example.com${NUL}`,
			`victim@example.com${UNIT_SEP}`,
			`victim@example.com${DEL}`,
		];
		for (const to of attempts) {
			expect(() => prependHeaders(RAW, envelopeHeaders("a@example.com", to))).toThrow(
				EnvelopeError,
			);
		}
		expect(() =>
			prependHeaders(RAW, envelopeHeaders("a@example.com\r\nX: y", "b@example.com")),
		).toThrow(EnvelopeError);
	});
});

describe("assertHeaderSafe", () => {
	it("accepts ordinary and internationalised addresses", () => {
		expect(() => assertHeaderSafe("Delivered-To", "support@erp.example.com")).not.toThrow();
		expect(() => assertHeaderSafe("Delivered-To", "josé@exämple.com")).not.toThrow();
		expect(() => assertHeaderSafe("Return-Path", "<>")).not.toThrow();
	});

	it("names the offending header in the error", () => {
		expect(() => assertHeaderSafe("Return-Path", "<a@b>\n")).toThrow(/Return-Path/);
	});

	it("limits the whole line to 998 octets, counted as UTF-8", () => {
		// "Delivered-To: " is 14 octets, so 984 more fill the line exactly.
		expect(() => assertHeaderSafe("Delivered-To", "a".repeat(984))).not.toThrow();
		expect(() => assertHeaderSafe("Delivered-To", "a".repeat(985))).toThrow(EnvelopeError);
		// 493 two-octet characters are 986 octets: over the limit although only 493 characters long.
		expect(() => assertHeaderSafe("Delivered-To", "é".repeat(493))).toThrow(EnvelopeError);
	});
});

describe("messageIdOf", () => {
	it("returns the trimmed header, angle brackets included", () => {
		const headers = new Headers({ "message-id": "  <abc.123@example.com>  " });
		expect(messageIdOf(headers)).toBe("<abc.123@example.com>");
	});

	it("is null when the header is missing or empty", () => {
		expect(messageIdOf(new Headers())).toBeNull();
		expect(messageIdOf(new Headers({ "Message-ID": "   " }))).toBeNull();
	});

	it("strips non-ASCII and control characters and caps the length at 256", () => {
		expect(messageIdOf(new Headers({ "Message-ID": `<abéc${DEL}@x>` }))).toBe("<abc@x>");
		const long = `<${"a".repeat(300)}@example.com>`;
		expect(messageIdOf(new Headers({ "Message-ID": long }))).toBe(long.slice(0, 256));
		expect(messageIdOf(new Headers({ "Message-ID": "éè" }))).toBeNull();
	});
});
