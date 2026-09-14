import { describe, it, expect, vi, afterEach } from "vitest";
import { bearerToken, intParam, json, notFound, unauthorized } from "../../src/lib/http";
import {
	ULID_PATTERN,
	concatBytes,
	logEvent,
	timingSafeEqual,
	toHex,
	ulid,
} from "../../src/lib/util";
import { VERSION } from "../../src/version";

describe("toHex", () => {
	it("renders each byte as two lowercase hex digits", () => {
		expect(toHex(new Uint8Array([0, 15, 16, 255]))).toBe("000f10ff");
		expect(toHex(new Uint8Array())).toBe("");
	});
});

describe("timingSafeEqual", () => {
	it("compares equal strings equal", () => {
		expect(timingSafeEqual("secret-token", "secret-token")).toBe(true);
		expect(timingSafeEqual("", "")).toBe(true);
		expect(timingSafeEqual("josé", "josé")).toBe(true);
	});

	it("rejects differing content and differing lengths", () => {
		expect(timingSafeEqual("secret-token", "secret-tokeN")).toBe(false);
		expect(timingSafeEqual("Secret-token", "secret-token")).toBe(false);
		expect(timingSafeEqual("secret-token", "secret-token ")).toBe(false);
		expect(timingSafeEqual("secret-token", "")).toBe(false);
	});
});

describe("concatBytes", () => {
	it("joins views and buffers in order into a fresh buffer", () => {
		const a = new Uint8Array([1, 2]);
		const b = new Uint8Array([3]).buffer;
		const c = new Uint8Array([4, 5, 6]);
		const out = concatBytes(a, b, c);
		expect([...out]).toEqual([1, 2, 3, 4, 5, 6]);
		expect(out.buffer).not.toBe(a.buffer);
		expect(out.buffer).not.toBe(c.buffer);
	});

	it("returns an empty array for no parts", () => {
		expect(concatBytes().length).toBe(0);
		expect(concatBytes(new Uint8Array()).length).toBe(0);
	});
});

describe("ulid", () => {
	it("is 26 characters of Crockford base32 with a 0-7 lead", () => {
		for (let i = 0; i < 200; i++) {
			expect(ulid()).toMatch(ULID_PATTERN);
		}
	});

	it("encodes the timestamp in the first ten characters (spec vectors)", () => {
		// 01ARYZ6S41 is the ULID spec README's own example time.
		expect(ulid(1_469_918_176_385).slice(0, 10)).toBe("01ARYZ6S41");
		expect(ulid(1_700_000_000_000).slice(0, 10)).toBe("01HF7YAT00");
		expect(ulid(0).slice(0, 10)).toBe("0000000000");
		expect(ulid(1).slice(0, 10)).toBe("0000000001");
		expect(ulid(2 ** 48 - 1).slice(0, 10)).toBe("7ZZZZZZZZZ");
	});

	it("sorts lexicographically by time", () => {
		const times = [0, 1, 1000, 1_469_918_176_385, 1_700_000_000_000, 2 ** 48 - 1];
		const ids = times.map((t) => ulid(t));
		for (let i = 1; i < ids.length; i++) {
			expect((ids[i - 1] ?? "") < (ids[i] ?? ""), `${ids[i - 1]} < ${ids[i]}`).toBe(true);
		}
	});

	it("differs in the random part for the same millisecond", () => {
		const a = ulid(1_700_000_000_000);
		const b = ulid(1_700_000_000_000);
		expect(a.slice(0, 10)).toBe(b.slice(0, 10));
		expect(a.slice(10)).not.toBe(b.slice(10));
	});

	it("rejects timestamps outside 48 bits", () => {
		expect(() => ulid(-1)).toThrow(RangeError);
		expect(() => ulid(1.5)).toThrow(RangeError);
		expect(() => ulid(2 ** 48)).toThrow(RangeError);
		expect(() => ulid(Number.NaN)).toThrow(RangeError);
	});
});

describe("logEvent", () => {
	afterEach(() => {
		vi.restoreAllMocks();
	});

	it("writes one JSON line with the event first", () => {
		const info = vi.spyOn(console, "info").mockImplementation(() => {});
		logEvent("info", "email_stored", { id: "01ARYZ6S41", size: 12 });
		expect(info).toHaveBeenCalledTimes(1);
		expect(info.mock.calls[0]?.[0]).toBe('{"event":"email_stored","id":"01ARYZ6S41","size":12}');
	});

	it("routes each level to the matching console method", () => {
		const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
		const error = vi.spyOn(console, "error").mockImplementation(() => {});
		logEvent("warn", "delivery_result");
		logEvent("error", "email_store_failed");
		expect(warn).toHaveBeenCalledWith('{"event":"delivery_result"}');
		expect(error).toHaveBeenCalledWith('{"event":"email_store_failed"}');
	});
});

describe("http helpers", () => {
	it("json() serialises with the given status", async () => {
		const res = json({ ok: true }, 202);
		expect(res.status).toBe(202);
		expect(res.headers.get("Content-Type")).toMatch(/^application\/json/);
		expect(await res.json()).toEqual({ ok: true });
	});

	it("notFound() and unauthorized() share the error shape", async () => {
		const missing = notFound();
		expect(missing.status).toBe(404);
		expect(await missing.json()).toEqual({ ok: false, error: "not found" });

		const denied = unauthorized();
		expect(denied.status).toBe(401);
		expect(denied.headers.get("WWW-Authenticate")).toBe("Bearer");
		expect(await denied.json()).toEqual({ ok: false, error: "unauthorized" });
	});

	it("bearerToken() extracts the token regardless of scheme case", () => {
		const request = (auth?: string): Request =>
			new Request("https://mail.test/inbox", auth ? { headers: { Authorization: auth } } : {});
		expect(bearerToken(request("Bearer abc.DEF-123"))).toBe("abc.DEF-123");
		expect(bearerToken(request("bearer abc"))).toBe("abc");
		expect(bearerToken(request("Bearer   spaced  "))).toBe("spaced");
		expect(bearerToken(request())).toBeNull();
		expect(bearerToken(request("Basic abc"))).toBeNull();
		expect(bearerToken(request("Bearer"))).toBeNull();
		expect(bearerToken(request("Bearer a b"))).toBeNull();
	});

	it("intParam() falls back on junk and clamps to the maximum", () => {
		const params = (query: string): URLSearchParams => new URLSearchParams(query);
		expect(intParam(params(""), "limit", 50, 200)).toBe(50);
		expect(intParam(params("limit=10"), "limit", 50, 200)).toBe(10);
		expect(intParam(params("limit=999"), "limit", 50, 200)).toBe(200);
		expect(intParam(params("limit=0"), "limit", 50, 200)).toBe(50);
		expect(intParam(params("limit=-3"), "limit", 50, 200)).toBe(50);
		expect(intParam(params("limit=1.5"), "limit", 50, 200)).toBe(50);
		expect(intParam(params("limit=abc"), "limit", 50, 200)).toBe(50);
		expect(intParam(params("limit=1e2"), "limit", 50, 200)).toBe(50);
	});
});

describe("version", () => {
	it("is a plain semver, as Release Please stamps it", () => {
		// Release Please rewrites this constant; a bad stamp would otherwise only surface in Odoo's
		// logs as a garbled User-Agent.
		expect(VERSION).toMatch(/^\d+\.\d+\.\d+$/);
	});
});
