import { env, exports } from "cloudflare:workers";
import { describe, it, expect } from "vitest";

// Placeholder proving the workerd harness is wired: the wrangler config resolves, the bindings the
// real suites lean on exist, and the entry module is what `exports` sees. The email path is not
// driven here — with only the setReject stub in place there is nothing to assert yet.
describe("harness", () => {
	it("answers unknown paths with 404", async () => {
		const res = await exports.default.fetch("https://mail.test/nope");
		expect(res.status).toBe(404);
	});

	it("exposes the R2 bucket and the InboxQueue namespace", () => {
		expect(typeof env.INBOX.put).toBe("function");
		const id = env.INBOX_QUEUE.idFromName("inbox");
		expect(env.INBOX_QUEUE.get(id)).toBeDefined();
	});
});
