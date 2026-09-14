import { env, exports } from "cloudflare:workers";
import { createExecutionContext } from "cloudflare:test";
import { beforeEach, describe, expect, it, vi } from "vitest";
import worker from "../../src/index";
import type { MailWorkerEnv } from "../../src/env";
import type { InboxRecord } from "../../src/inbox-do";
import { ingest, makeDue, ops, queue, runPass, resetQueue } from "./helpers";

interface ListBody {
	ok: boolean;
	items: InboxRecord[];
}

beforeEach(resetQueue);

describe("ops API", () => {
	it("GET /health is public and says nothing else", async () => {
		const response = await ops("/health", {}, null);
		expect(response.status).toBe(200);
		expect(await response.json()).toEqual({ ok: true });
	});

	it("requires the bearer token for everything else", async () => {
		const anonymous = await ops("/inbox", {}, null);
		expect(anonymous.status).toBe(401);
		expect(anonymous.headers.get("WWW-Authenticate")).toBe("Bearer");
		expect((await ops("/inbox", {}, "wrong-token")).status).toBe(401);
		expect((await ops("/health", { method: "POST" }, null)).status).toBe(401);
	});

	it("does not exist at all when OPS_TOKEN is unset", async () => {
		const handler = worker.fetch;
		if (handler === undefined) {
			throw new Error("the worker exports no fetch handler");
		}
		// A blank value is "unset" (that is all a dashboard form can express).
		const override = { ...env, OPS_TOKEN: "" } as MailWorkerEnv;
		type IncomingRequest = Request<unknown, IncomingRequestCfProperties>;
		const request = new Request("https://mail.test/inbox", {
			headers: { Authorization: "Bearer " },
		}) as IncomingRequest;
		const response = await handler(request, override, createExecutionContext());
		expect(response.status).toBe(404);
		const health = await handler(
			new Request("https://mail.test/health") as IncomingRequest,
			override,
			createExecutionContext(),
		);
		expect(health.status).toBe(200);
	});

	it("lists rows newest first, by status, within the limit", async () => {
		const stub = queue();
		const first = await ingest("first@erp.example.com");
		const second = await ingest("reject422@erp.example.com");
		await makeDue(stub, second.id);
		await runPass(stub);

		const all = (await (await ops("/inbox")).json()) as ListBody;
		expect(all.ok).toBe(true);
		expect(all.items.map((r) => r.id)).toEqual([second.id, first.id]);

		const rejected = (await (await ops("/inbox?status=rejected")).json()) as ListBody;
		expect(rejected.items.map((r) => r.id)).toEqual([second.id]);

		const limited = (await (await ops("/inbox?limit=1")).json()) as ListBody;
		expect(limited.items).toHaveLength(1);

		expect((await ops("/inbox?status=bogus")).status).toBe(400);
	});

	it("serves one row and its raw message", async () => {
		const row = await ingest("raw@erp.example.com");
		const item = await ops(`/inbox/${row.id}`);
		expect(item.status).toBe(200);
		expect(((await item.json()) as { item: InboxRecord }).item).toEqual(row);

		const raw = await ops(`/inbox/${row.id}/raw`);
		expect(raw.status).toBe(200);
		expect(raw.headers.get("Content-Type")).toBe("message/rfc822");
		expect(raw.headers.get("Content-Disposition")).toBe(`attachment; filename="${row.id}.eml"`);
		const text = await raw.text();
		expect(text.startsWith("Delivered-To: raw@erp.example.com\r\n")).toBe(true);
		expect(text.length).toBe(row.size);

		await env.INBOX.delete(row.r2Key);
		expect((await ops(`/inbox/${row.id}/raw`)).status).toBe(404);
	});

	it("retries one row or every parked row", async () => {
		const stub = queue();
		const row = await ingest("reject422@erp.example.com");
		// Still pending: nothing to retry.
		expect((await ops(`/inbox/${row.id}/retry`, { method: "POST" })).status).toBe(409);
		await makeDue(stub, row.id);
		await runPass(stub);
		expect((await ops(`/inbox/${row.id}/retry`, { method: "POST" })).status).toBe(202);
		// The runtime re-attempts at once and Odoo parks it again; wait for that before the bulk call.
		await vi.waitFor(
			async () => {
				expect((await stub.get(row.id))?.attempts).toBe(1);
			},
			{ timeout: 5000, interval: 50 },
		);
		const bulk = await ops("/inbox/retry?status=rejected", { method: "POST" });
		expect(bulk.status).toBe(202);
		expect(await bulk.json()).toEqual({ ok: true, status: "rejected", requeued: 1 });
		expect((await ops("/inbox/retry?status=pending", { method: "POST" })).status).toBe(400);
		expect((await ops("/inbox/retry", { method: "POST" })).status).toBe(400);
	});

	it("deletes a row together with its message", async () => {
		const row = await ingest("delete@erp.example.com");
		expect((await ops(`/inbox/${row.id}`, { method: "DELETE" })).status).toBe(200);
		expect((await ops(`/inbox/${row.id}`)).status).toBe(404);
		expect(await env.INBOX.get(row.r2Key)).toBeNull();
		expect((await ops(`/inbox/${row.id}`, { method: "DELETE" })).status).toBe(404);
	});

	it("answers 404 for unknown routes and malformed ids", async () => {
		expect((await ops("/nope")).status).toBe(404);
		expect((await ops("/inbox/not-a-ulid")).status).toBe(404);
		expect((await ops("/inbox/01ARZ3NDEKTSV4RRFFQ69G5FAV")).status).toBe(404);
		expect((await ops("/inbox/01ARZ3NDEKTSV4RRFFQ69G5FAV/raw")).status).toBe(404);
		expect((await ops("/inbox/01ARZ3NDEKTSV4RRFFQ69G5FAV/retry", { method: "POST" })).status).toBe(
			404,
		);
		expect((await ops("/inbox/01ARZ3NDEKTSV4RRFFQ69G5FAV/bogus")).status).toBe(404);
		expect((await ops("/inbox", { method: "DELETE" })).status).toBe(404);
		// The service-binding default export is the same handler.
		expect((await exports.default.fetch("https://mail.test/health")).status).toBe(200);
	});
});
