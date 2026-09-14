import { env, createExecutionContext, runDurableObjectAlarm, runInDurableObject } from "cloudflare:test";
import worker from "../../src/index";
import { describe, it, expect } from "vitest";
import { signBody } from "../../src/lib/sign";
import { inboxQueue } from "../../src/inbox-do";
import type { InboxQueue } from "../../src/inbox-do";
import type { InboxRecord } from "../../src/inbox-do";

const RAW = "Message-ID: <x@example.com>\r\nSubject: hi\r\n\r\nbody\r\n";

function mockMessage(from: string, to: string, raw = RAW) {
	const bytes = new TextEncoder().encode(raw);
	let rejected: string | null = null;
	const message = {
		from,
		to,
		headers: new Headers({ "Message-ID": "<x@example.com>" }),
		rawSize: bytes.length,
		raw: new Response(bytes).body!,
		setReject(reason: string) { rejected = reason; },
		forward: () => Promise.reject(new Error("nope")),
		reply: () => Promise.reject(new Error("nope")),
		rejectedReason: () => rejected,
	};
	return message;
}

async function captured(id: string) {
	const res = await fetch(`https://control.test/captured/${id}`);
	if (res.status !== 200) return null;
	return (await res.json()) as { headers: Record<string, string>; body: number[] };
}

async function pendingRow(to: string): Promise<InboxRecord> {
	const m = mockMessage("alice@example.com", to);
	await worker.email!(m as never, env, createExecutionContext());
	const rows = await inboxQueue(env).list("pending", 100);
	const row = rows.find((r) => r.to === to);
	if (!row) throw new Error("row missing");
	return row;
}

async function fireNow(stub: DurableObjectStub<InboxQueue>) {
	await runInDurableObject(stub, async (_instance, state) => {
		state.storage.sql.exec("UPDATE inbox SET next_attempt_at = 0 WHERE status = 'pending'");
		await state.storage.setAlarm(Date.now());
	});
	return runDurableObjectAlarm(stub);
}

describe("scratch W2", () => {
	it("stores, enqueues, delivers with a valid signature", async () => {
		const stub = inboxQueue(env);
		const row = await pendingRow("support@erp.example.com");
		expect(row.status).toBe("pending");
		expect(row.messageId).toBe("<x@example.com>");
		const alarm = await runInDurableObject(stub, (_i, state) => state.storage.getAlarm());
		expect(alarm).toBeGreaterThan(Date.now() + 3_500_000);

		const obj = await env.INBOX.get(row.r2Key);
		expect(obj).not.toBeNull();
		const text = await obj!.text();
		expect(text.startsWith("Delivered-To: support@erp.example.com\r\nReturn-Path: <alice@example.com>\r\nMessage-ID:")).toBe(true);
		expect(obj!.httpMetadata?.contentType).toBe("message/rfc822");
		expect(obj!.customMetadata?.from).toBe("alice@example.com");
		expect(obj!.customMetadata?.messageId).toBe("<x@example.com>");

		expect(await fireNow(stub)).toBe(true);
		const after = await stub.get(row.id);
		expect(after?.status).toBe("delivered");
		expect(after?.threadId).toBe(42);
		expect(after?.attempts).toBe(1);
		expect(after?.lastStatus).toBe(200);
		expect(after?.purgeAt).toBeGreaterThan(Date.now() + 29 * 86_400_000);

		const hit = await captured(row.id);
		expect(hit).not.toBeNull();
		const h = hit!.headers;
		expect(h["content-type"]).toBe("message/rfc822");
		expect(h["user-agent"]).toBe("mail-cloudflare-worker/1.0.0");
		expect(h["x-mail-cloudflare-attempt"]).toBe("1");
		expect(h["x-mail-cloudflare-envelope-from"]).toBe("alice@example.com");
		const body = new Uint8Array(hit!.body);
		const expected = await signBody("integration-test-secret", Number(h["x-mail-cloudflare-timestamp"]), body);
		expect(h["x-mail-cloudflare-signature"]).toBe(expected);
		expect(new TextDecoder().decode(body)).toBe(text);
		// Alarm cleared or set to the purge time
		const alarm2 = await runInDurableObject(stub, (_i, state) => state.storage.getAlarm());
		expect(alarm2).toBe(after?.purgeAt);
	});

	it("500 -> pending with backoff; dead after MAX_ATTEMPTS", async () => {
		const stub = inboxQueue(env);
		const row = await pendingRow("fail500@erp.example.com");
		await fireNow(stub);
		let after = await stub.get(row.id);
		expect(after?.status).toBe("pending");
		expect(after?.attempts).toBe(1);
		expect(after?.lastStatus).toBe(500);
		expect(after?.nextAttemptAt).toBeGreaterThan(Date.now() + 55_000);
		expect(after?.nextAttemptAt).toBeLessThan(Date.now() + 65_000);
		const alarm = await runInDurableObject(stub, (_i, state) => state.storage.getAlarm());
		expect(alarm).toBe(after?.nextAttemptAt);
		await fireNow(stub);
		after = await stub.get(row.id);
		expect(after?.attempts).toBe(2);
		expect(after?.nextAttemptAt).toBeGreaterThan(Date.now() + 290_000);
		await fireNow(stub);
		after = await stub.get(row.id);
		expect(after?.status).toBe("dead");
		expect(after?.attempts).toBe(3);
		expect(after?.nextAttemptAt).toBeNull();
		// retry resets budget
		expect(await stub.retry(row.id)).toBe("requeued");
		expect(await stub.retry(row.id)).toBe("already_pending");
		expect(await stub.retry("01ARZ3NDEKTSV4RRFFQ69G5FAV")).toBe("not_found");
		after = await stub.get(row.id);
		expect(after?.attempts).toBe(0);
		expect(await stub.remove(row.id)).toBe(true);
		expect(await stub.remove(row.id)).toBe(false);
		expect(await env.INBOX.get(row.r2Key)).toBeNull();
	});

	it("422 -> rejected, 302 -> rejected", async () => {
		const stub = inboxQueue(env);
		const a = await pendingRow("reject422@erp.example.com");
		const b = await pendingRow("redirect@erp.example.com");
		await fireNow(stub);
		const ra = await stub.get(a.id);
		expect(ra?.status).toBe("rejected");
		expect(ra?.lastError).toBe("HTTP 422: No possible route found");
		const rb = await stub.get(b.id);
		expect(rb?.status).toBe("rejected");
		expect(rb?.lastStatus).toBe(302);
		expect(await stub.retryAll("rejected")).toBe(2);
		expect((await stub.get(a.id))?.status).toBe("pending");
	});

	it("CR/LF envelope -> setReject, nothing stored", async () => {
		const before = (await env.INBOX.list()).objects.length;
		const m = mockMessage("alice@example.com", "x@erp.example.com\r\nBcc: evil@x");
		await worker.email!(m as never, env, createExecutionContext());
		expect(m.rejectedReason()).toBe("Invalid envelope address");
		expect((await env.INBOX.list()).objects.length).toBe(before);
	});

	it("non-ASCII envelope is percent-encoded on the wire and still delivered", async () => {
		const stub = inboxQueue(env);
		const row = await pendingRow("josé@erp.example.com");
		await fireNow(stub);
		const after = await stub.get(row.id);
		expect(after?.status).toBe("delivered");
		const hit = await captured(row.id);
		expect(hit!.headers["x-mail-cloudflare-envelope-to"]).toBe("jos%C3%A9@erp.example.com");
		const obj = await env.INBOX.get(row.r2Key);
		expect(obj!.customMetadata?.to).toBe("jos%C3%A9@erp.example.com");
	});

	it("ops api", async () => {
		const auth = { Authorization: "Bearer integration-ops-token" };
		const f = (path: string, init: RequestInit = {}) => worker.fetch!(new Request(`https://mail.test${path}`, init) as never, env, createExecutionContext());
		expect((await f("/health")).status).toBe(200);
		expect(await (await f("/health")).json()).toEqual({ ok: true });
		expect((await f("/inbox")).status).toBe(401);
		expect((await f("/inbox", { headers: { Authorization: "Bearer nope" } })).status).toBe(401);
		const list = await (await f("/inbox?status=delivered&limit=2", { headers: auth })).json() as { ok: boolean; items: InboxRecord[] };
		expect(list.ok).toBe(true);
		expect(list.items.length).toBeLessThanOrEqual(2);
		expect((await f("/inbox?status=bogus", { headers: auth })).status).toBe(400);
		expect((await f("/inbox/not-a-ulid", { headers: auth })).status).toBe(404);
		expect((await f("/inbox/01ARZ3NDEKTSV4RRFFQ69G5FAV", { headers: auth })).status).toBe(404);
		const row = await pendingRow("ops@erp.example.com");
		const raw = await f(`/inbox/${row.id}/raw`, { headers: auth });
		expect(raw.status).toBe(200);
		expect(raw.headers.get("Content-Type")).toBe("message/rfc822");
		expect((await raw.text()).startsWith("Delivered-To: ops@erp.example.com")).toBe(true);
		expect((await f(`/inbox/${row.id}/retry`, { method: "POST", headers: auth })).status).toBe(409);
		const bulk = await f("/inbox/retry?status=dead", { method: "POST", headers: auth });
		expect(bulk.status).toBe(202);
		expect((await f(`/inbox/${row.id}`, { method: "DELETE", headers: auth })).status).toBe(200);
		expect((await f(`/inbox/${row.id}`, { headers: auth })).status).toBe(404);
		expect((await f("/nope", { headers: auth })).status).toBe(404);
	});

	it("purge deletes the object and row once purge_at passes", async () => {
		const stub = inboxQueue(env);
		const row = await pendingRow("purge@erp.example.com");
		await fireNow(stub);
		expect((await stub.get(row.id))?.status).toBe("delivered");
		await runInDurableObject(stub, async (_i, state) => {
			state.storage.sql.exec("UPDATE inbox SET purge_at = 1 WHERE id = ?", row.id);
			await state.storage.setAlarm(Date.now());
		});
		expect(await runDurableObjectAlarm(stub)).toBe(true);
		expect(await stub.get(row.id)).toBeNull();
		expect(await env.INBOX.get(row.r2Key)).toBeNull();
	});
});
