import { describe, it, expect } from "vitest";
import { ConfigError, loadConfig, opsTokenOf } from "../../src/config";
import type { MailWorkerVars } from "../../src/env";
import { DEFAULT_BACKOFF_SECONDS } from "../../src/lib/backoff";

const INBOUND_URL = "https://odoo.example.com/mail_cloudflare/inbound/k3y-that-is-secret";
const SECRET = "a-webhook-secret-of-sufficient-length";

/** The two required vars, with anything else layered on top. */
function vars(overrides: Partial<MailWorkerVars> = {}): Partial<MailWorkerVars> {
	return { ODOO_INBOUND_URL: INBOUND_URL, ODOO_WEBHOOK_SECRET: SECRET, ...overrides };
}

/** The ConfigError message for a given env, for asserting which fields it names. */
function failure(env: Partial<MailWorkerVars>): string {
	try {
		loadConfig(env);
	} catch (error) {
		if (error instanceof ConfigError) {
			return error.message;
		}
		throw error;
	}
	throw new Error("expected loadConfig to throw");
}

describe("loadConfig", () => {
	it("applies the documented defaults when only the required vars are set", () => {
		expect(loadConfig(vars())).toEqual({
			inboundUrl: INBOUND_URL,
			webhookSecret: SECRET,
			access: null,
			opsToken: null,
			retentionMs: 30 * 86_400_000,
			maxAttempts: 32,
			backoffSeconds: DEFAULT_BACKOFF_SECONDS,
			deliveryTimeoutMs: 30_000,
			deliveryDelayMs: 0,
		});
	});

	it("parses every tunable", () => {
		const config = loadConfig(
			vars({
				OPS_TOKEN: "ops-token",
				CF_ACCESS_CLIENT_ID: "client-id",
				CF_ACCESS_CLIENT_SECRET: "client-secret",
				RETENTION_DAYS: "0",
				MAX_ATTEMPTS: "3",
				BACKOFF_SECONDS: "60, 300",
				DELIVERY_TIMEOUT_SECONDS: "60",
				DELIVERY_DELAY_SECONDS: "3600",
			}),
		);
		expect(config.opsToken).toBe("ops-token");
		expect(config.access).toEqual({ clientId: "client-id", clientSecret: "client-secret" });
		expect(config.retentionMs).toBe(0);
		expect(config.maxAttempts).toBe(3);
		expect(config.backoffSeconds).toEqual([60, 300]);
		expect(config.deliveryTimeoutMs).toBe(60_000);
		expect(config.deliveryDelayMs).toBe(3_600_000);
	});

	it("requires the inbound URL and the secret, naming both", () => {
		const message = failure({});
		expect(message).toContain("ODOO_INBOUND_URL");
		expect(message).toContain("ODOO_WEBHOOK_SECRET");
	});

	it("treats empty and whitespace-only strings as unset", () => {
		expect(failure(vars({ ODOO_INBOUND_URL: "" }))).toContain("ODOO_INBOUND_URL");
		expect(failure(vars({ ODOO_WEBHOOK_SECRET: "   " }))).toContain("ODOO_WEBHOOK_SECRET");

		const config = loadConfig(
			vars({
				OPS_TOKEN: "",
				CF_ACCESS_CLIENT_ID: "",
				CF_ACCESS_CLIENT_SECRET: " ",
				RETENTION_DAYS: "",
				MAX_ATTEMPTS: "",
				BACKOFF_SECONDS: "",
				DELIVERY_TIMEOUT_SECONDS: "",
				DELIVERY_DELAY_SECONDS: "",
			}),
		);
		expect(config.opsToken).toBeNull();
		expect(config.access).toBeNull();
		expect(config.retentionMs).toBe(30 * 86_400_000);
		expect(config.maxAttempts).toBe(32);
		expect(config.backoffSeconds).toEqual(DEFAULT_BACKOFF_SECONDS);
		expect(config.deliveryTimeoutMs).toBe(30_000);
		expect(config.deliveryDelayMs).toBe(0);
	});

	it("trims surrounding whitespace from values", () => {
		const config = loadConfig(
			vars({ ODOO_INBOUND_URL: ` ${INBOUND_URL}\n`, OPS_TOKEN: " ops-token " }),
		);
		expect(config.inboundUrl).toBe(INBOUND_URL);
		expect(config.opsToken).toBe("ops-token");
	});

	it("requires the Access client id and secret together", () => {
		expect(failure(vars({ CF_ACCESS_CLIENT_ID: "client-id" }))).toContain(
			"CF_ACCESS_CLIENT_SECRET",
		);
		expect(failure(vars({ CF_ACCESS_CLIENT_SECRET: "client-secret" }))).toContain(
			"CF_ACCESS_CLIENT_ID",
		);
		// A blank half is an unset half.
		expect(
			failure(vars({ CF_ACCESS_CLIENT_ID: "client-id", CF_ACCESS_CLIENT_SECRET: "" })),
		).toContain("CF_ACCESS_CLIENT_SECRET");
	});

	it("accepts only http(s) inbound URLs, including plain-http localhost for wrangler dev", () => {
		expect(loadConfig(vars({ ODOO_INBOUND_URL: "http://localhost:8169/x" })).inboundUrl).toBe(
			"http://localhost:8169/x",
		);
		for (const url of [
			"ftp://odoo.example.com/x",
			"mailto:odoo@example.com",
			"odoo.example.com",
			"https://",
		]) {
			expect(failure(vars({ ODOO_INBOUND_URL: url })), url).toContain("ODOO_INBOUND_URL");
		}
	});

	it("requires a secret of at least 16 characters", () => {
		expect(failure(vars({ ODOO_WEBHOOK_SECRET: "short-secret" }))).toContain("ODOO_WEBHOOK_SECRET");
		expect(loadConfig(vars({ ODOO_WEBHOOK_SECRET: "0123456789abcdef" })).webhookSecret).toBe(
			"0123456789abcdef",
		);
	});

	it("rejects numeric tunables that are not whole numbers in range", () => {
		const cases: [keyof MailWorkerVars, string][] = [
			["RETENTION_DAYS", "-1"],
			["RETENTION_DAYS", "1.5"],
			["RETENTION_DAYS", "1e3"],
			["RETENTION_DAYS", "thirty"],
			["MAX_ATTEMPTS", "0"],
			["DELIVERY_TIMEOUT_SECONDS", "0"],
			["DELIVERY_TIMEOUT_SECONDS", "61"],
			["DELIVERY_DELAY_SECONDS", "abc"],
		];
		for (const [name, value] of cases) {
			expect(failure(vars({ [name]: value })), `${name}=${value}`).toContain(name);
		}
	});

	it("reports a bad backoff schedule under BACKOFF_SECONDS", () => {
		expect(failure(vars({ BACKOFF_SECONDS: "60,3OO" }))).toContain("BACKOFF_SECONDS");
		expect(failure(vars({ BACKOFF_SECONDS: "60,0" }))).toContain("BACKOFF_SECONDS");
	});

	it("collects every problem into one ConfigError", () => {
		const message = failure({
			ODOO_INBOUND_URL: "not a url",
			ODOO_WEBHOOK_SECRET: "short",
			MAX_ATTEMPTS: "zero",
		});
		expect(message).toContain("ODOO_INBOUND_URL");
		expect(message).toContain("ODOO_WEBHOOK_SECRET");
		expect(message).toContain("MAX_ATTEMPTS");
	});

	it("names fields, never values", () => {
		const message = failure(
			vars({ ODOO_INBOUND_URL: "gopher://odoo.example.com/secret-key", MAX_ATTEMPTS: "lots" }),
		);
		expect(message).not.toContain("secret-key");
		expect(message).not.toContain("gopher");
		expect(message).not.toContain("lots");
		expect(message).not.toContain(SECRET);
	});

	it("ignores unknown vars and bindings", () => {
		const env = { ...vars(), INBOX: { put: () => null }, UNRELATED: "x" };
		expect(loadConfig(env).inboundUrl).toBe(INBOUND_URL);
	});
});

describe("opsTokenOf", () => {
	it("reads the token with the same blank-means-unset rule, without needing the rest", () => {
		expect(opsTokenOf({})).toBeNull();
		expect(opsTokenOf({ OPS_TOKEN: "" })).toBeNull();
		expect(opsTokenOf({ OPS_TOKEN: "  " })).toBeNull();
		expect(opsTokenOf({ OPS_TOKEN: " ops-token " })).toBe("ops-token");
	});
});
