import { describe, it, expect } from "vitest";
import { hmacSha256Hex, signBody, signaturePayload, unixSeconds } from "../../src/lib/sign";

const enc = new TextEncoder();

// Known-answer vectors produced by the exact Python expression Odoo's fetchmail.server uses:
//   hmac.new(b"key", b"1700000000." + body, hashlib.sha256).hexdigest()
// If either side drifts (a separator, an encoding, a trailing newline) this is where it shows.
const SECRET = "key";
const TIMESTAMP = 1_700_000_000;
const TEXT_DIGEST = "4d583a269f4f276a3fa80ff31b5a01879a848096983222a17893d198418939aa";
const BINARY_BODY = new Uint8Array([0x00, 0xff, 0x0d, 0x0a, 0x80, 0x7f]);
const BINARY_DIGEST = "ca765087906f3a09a97f4e052b4e82dab694cf3184c2c486427e4dfe9d7bfc4e";

describe("signaturePayload", () => {
	it("is the UTF-8 timestamp, a dot, then the body bytes verbatim", () => {
		const payload = signaturePayload(TIMESTAMP, enc.encode("hello"));
		expect(new TextDecoder().decode(payload)).toBe("1700000000.hello");
	});

	it("accepts an ArrayBuffer and never alters binary bytes", () => {
		const fromView = signaturePayload(TIMESTAMP, BINARY_BODY);
		const fromBuffer = signaturePayload(TIMESTAMP, BINARY_BODY.buffer);
		expect(fromView).toEqual(fromBuffer);
		expect([...fromView.subarray(-BINARY_BODY.length)]).toEqual([...BINARY_BODY]);
	});
});

describe("hmacSha256Hex", () => {
	it("matches Python's hmac module on the text vector", async () => {
		const payload = signaturePayload(TIMESTAMP, enc.encode("hello"));
		expect(await hmacSha256Hex(SECRET, payload)).toBe(TEXT_DIGEST);
	});

	it("matches Python's hmac module on a body with NUL, CRLF and high bytes", async () => {
		const payload = signaturePayload(TIMESTAMP, BINARY_BODY);
		expect(await hmacSha256Hex(SECRET, payload)).toBe(BINARY_DIGEST);
	});
});

describe("signBody", () => {
	it("prefixes the hex digest with the v1 scheme tag", async () => {
		expect(await signBody(SECRET, TIMESTAMP, enc.encode("hello"))).toBe(`v1=${TEXT_DIGEST}`);
		expect(await signBody(SECRET, TIMESTAMP, BINARY_BODY)).toBe(`v1=${BINARY_DIGEST}`);
	});

	it("changes with the timestamp, the body and the secret", async () => {
		const body = enc.encode("hello");
		const reference = await signBody(SECRET, TIMESTAMP, body);
		expect(await signBody(SECRET, TIMESTAMP + 1, body)).not.toBe(reference);
		expect(await signBody(SECRET, TIMESTAMP, enc.encode("hello!"))).not.toBe(reference);
		expect(await signBody("other", TIMESTAMP, body)).not.toBe(reference);
	});

	it("is deterministic for identical inputs", async () => {
		const body = enc.encode("hello");
		expect(await signBody(SECRET, TIMESTAMP, body)).toBe(await signBody(SECRET, TIMESTAMP, body));
	});
});

describe("unixSeconds", () => {
	it("truncates milliseconds to whole seconds", () => {
		expect(unixSeconds(1_700_000_000_999)).toBe(1_700_000_000);
		expect(unixSeconds(1_700_000_000_000)).toBe(1_700_000_000);
	});

	it("defaults to now", () => {
		const before = Math.floor(Date.now() / 1000);
		const value = unixSeconds();
		const after = Math.floor(Date.now() / 1000);
		expect(value).toBeGreaterThanOrEqual(before);
		expect(value).toBeLessThanOrEqual(after);
	});
});
