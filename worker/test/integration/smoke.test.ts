import { env, exports } from "cloudflare:workers";
import { describe, it, expect } from "vitest";

// Placeholder proving the workerd harness is wired: the wrangler config resolves, the bindings the
// real suites lean on exist, and the entry module is what `exports` sees. The email path is not
// driven here — email.test.ts, inbox-do.test.ts and ops.test.ts cover the real behaviour.
describe("harness", () => {
	it("answers unknown paths with 404 once the ops token is presented", async () => {
		// OPS_TOKEN is set in vitest.integration.config.ts, so an anonymous request is a 401 before
		// any routing happens; the 404 for an unknown route is only reachable with the bearer.
		const anonymous = await exports.default.fetch("https://mail.test/nope");
		expect(anonymous.status).toBe(401);
		const res = await exports.default.fetch("https://mail.test/nope", {
			headers: { Authorization: "Bearer integration-ops-token" },
		});
		expect(res.status).toBe(404);
	});

	it("exposes the R2 bucket and the InboxQueue namespace", () => {
		expect(typeof env.INBOX.put).toBe("function");
		const id = env.INBOX_QUEUE.idFromName("inbox");
		expect(env.INBOX_QUEUE.get(id)).toBeDefined();
	});
});
