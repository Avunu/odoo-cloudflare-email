// The fake Odoo the integration suites deliver to, plus the bindings they run with.
//
// This module is imported by vitest.integration.config.ts (Node, where miniflare's
// `outboundService` runs) AND by the tests (workerd, for the constants), so it must not touch
// `cloudflare:test`, the filesystem or anything else only one side has. The captured deliveries
// live in this Node-side Map and are read back from workerd through the `control.test` origin:
// the two sides share no memory, only the outbound fetch path, which is exactly what makes the
// capture faithful — it sees the bytes and headers the worker really sent.

export const ODOO_ORIGIN = "https://odoo.test";
export const ODOO_INBOUND_URL = `${ODOO_ORIGIN}/mail_cloudflare/inbound/test-server-key`;
export const ODOO_WEBHOOK_SECRET = "integration-test-secret-0123456789";
export const OPS_TOKEN = "integration-ops-token-0123456789";
export const CONTROL_ORIGIN = "https://control.test";

// oxfmt rewrites escape sequences inside string literals into the bytes they denote, so the line
// ending is spelled out by code point to stay visible (and unmangled) in the source.
const CRLF = String.fromCodePoint(13, 10);

/**
 * Test/fixtures/simple.eml, line by line. The same bytes the wrangler dev recipe POSTs; kept here
 * as well because workerd has no filesystem to read the file from and Node's is off-limits to the
 * config under the lint rules. CRLF-terminated lines, like the file.
 */
export const FIXTURE_EML = [
	"Message-ID: <simple-0001@example.com>",
	"Date: Mon, 14 Sep 2026 12:00:00 +0000",
	"From: Alice Example <alice@example.com>",
	"To: support@erp.example.com",
	"Subject: Hello from the fixture",
	"MIME-Version: 1.0",
	"Content-Type: text/plain; charset=utf-8",
	"Content-Transfer-Encoding: 7bit",
	"",
	"Hi there,",
	"",
	"This is the simple fixture message used by the tests and the wrangler dev recipe.",
	"",
	"-- ",
	"Alice",
]
	.map((line) => `${line}${CRLF}`)
	.join("");

/** The env the suites run with; DELIVERY_DELAY_SECONDS keeps alarms from firing on their own. */
export const BINDINGS = {
	ODOO_INBOUND_URL,
	ODOO_WEBHOOK_SECRET,
	OPS_TOKEN,
	RETENTION_DAYS: "30",
	MAX_ATTEMPTS: "3",
	BACKOFF_SECONDS: "60,300",
	// The `slow@` recipient sleeps longer than this so the timeout path is exercised.
	DELIVERY_TIMEOUT_SECONDS: "1",
	// Enqueued rows are due an hour out; tests rewind them and fire the alarm deliberately with
	// runDurableObjectAlarm(), so no delivery ever happens behind a test's back.
	DELIVERY_DELAY_SECONDS: "3600",
} as const;

/** One delivery as the fake Odoo received it. */
export interface CapturedDelivery {
	readonly method: string;
	readonly path: string;
	/** Header names lower-cased, as `Headers` iterates them. */
	readonly headers: Record<string, string>;
	/** The body bytes, as a plain array so they survive JSON. */
	readonly body: number[];
}

const captured = new Map<string, CapturedDelivery>();

/** How long `slow@` stalls — comfortably past DELIVERY_TIMEOUT_SECONDS. */
const SLOW_MS = 1500;

function sleep(ms: number): Promise<void> {
	return new Promise((resolve) => {
		setTimeout(resolve, ms);
	});
}

/**
 * Answer by the envelope recipient's local part, so a test picks Odoo's behaviour simply by
 * choosing an address:
 *
 * - `fail500@…` → 500, the transient-failure path (retry with backoff)
 * - `reject422@…` → 422 with Odoo's error shape (unroutable: parked as rejected)
 * - `redirect@…` → 302 (a misconfigured URL: never followed, parked as rejected)
 * - `slow@…` → stalls past the delivery timeout (network-level failure: retry)
 * - `nothread@…` → 200 with `thread_id: false` (a duplicate Odoo ignored: delivered)
 * - `plain200@…` → 200 with a non-JSON body (delivered, thread id unknown)
 * - Anything else → 200 `{ok:true, thread_id: 42}`
 *
 * Every other host is refused with a 502 so a test that starts depending on the network fails
 * loudly instead of quietly reaching out.
 */
export async function outboundService(request: Request): Promise<Response> {
	const url = new URL(request.url);

	if (url.origin === CONTROL_ORIGIN) {
		const id = url.pathname.split("/").pop() ?? "";
		const hit = captured.get(id);
		return hit === undefined
			? new Response("no such delivery", { status: 404 })
			: Response.json(hit);
	}

	if (url.origin !== ODOO_ORIGIN || request.method !== "POST") {
		return new Response(`unexpected outbound request to ${request.url}`, { status: 502 });
	}

	const headers: Record<string, string> = {};
	for (const [name, value] of request.headers) {
		headers[name] = value;
	}
	const body = [...new Uint8Array(await request.arrayBuffer())];
	const id = headers["x-mail-cloudflare-id"] ?? "";
	captured.set(id, { method: request.method, path: url.pathname, headers, body });

	const [local] = (headers["x-mail-cloudflare-envelope-to"] ?? "").split("@");
	switch (local) {
		case "fail500": {
			return Response.json({ ok: false, error: "internal error" }, { status: 500 });
		}
		case "reject422": {
			return Response.json({ ok: false, error: "No possible route found" }, { status: 422 });
		}
		case "redirect": {
			return new Response(null, { status: 302, headers: { Location: "https://elsewhere.test/" } });
		}
		case "slow": {
			await sleep(SLOW_MS);
			return Response.json({ ok: true, thread_id: 1, id });
		}
		case "nothread": {
			return Response.json({ ok: true, thread_id: false, id });
		}
		case "plain200": {
			return new Response("OK", { status: 200 });
		}
		default: {
			return Response.json({ ok: true, thread_id: 42, id });
		}
	}
}
