import { env } from "cloudflare:workers";
import { runDurableObjectAlarm, runInDurableObject } from "cloudflare:test";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { alarmAt, ingest, makeDue, queue, rowOf, runDuePass, runPass, resetQueue } from "./helpers";

beforeEach(resetQueue);

describe("InboxQueue", () => {
	it("enqueue is idempotent on the id", async () => {
		const stub = queue();
		const row = await ingest("once@erp.example.com");
		const again = await stub.enqueue({
			id: row.id,
			r2Key: row.r2Key,
			from: row.from,
			to: row.to,
			messageId: row.messageId,
			size: row.size,
			receivedAt: row.receivedAt,
		});
		expect(again).toBe(false);
		expect((await stub.list(null, 1000)).filter((r) => r.id === row.id)).toHaveLength(1);
	});

	it("lists newest first, filtered by status, capped by limit", async () => {
		const stub = queue();
		const first = await ingest("first@erp.example.com");
		const second = await ingest("second@erp.example.com");
		const third = await ingest("reject422@erp.example.com");
		await makeDue(stub, third.id);
		await runPass(stub);

		const all = await stub.list(null, 1000);
		expect(all.map((r) => r.id)).toEqual([third.id, second.id, first.id]);
		expect((await stub.list("pending", 1000)).map((r) => r.id)).toEqual([second.id, first.id]);
		expect((await stub.list("rejected", 1000)).map((r) => r.id)).toEqual([third.id]);
		expect(await stub.list(null, 1)).toHaveLength(1);
		expect(await stub.get(first.id)).toEqual(first);
		expect(await stub.get("01ARZ3NDEKTSV4RRFFQ69G5FAV")).toBeNull();
	});

	it("delivers due rows in batches and comes straight back for the rest", async () => {
		const stub = queue();
		const rows = [];
		for (let i = 0; i < 12; i += 1) {
			rows.push(await ingest(`batch${i}@erp.example.com`));
		}
		expect(await runDuePass(stub)).toBe(true);
		const delivered = (await stub.list("delivered", 1000)).length;
		// One pass takes ALARM_BATCH rows; the rest are still due, so the pass re-armed for now
		// and the runtime finishes them on its own.
		expect(delivered).toBeGreaterThanOrEqual(10);
		await vi.waitFor(
			async () => {
				expect((await stub.list("delivered", 1000)).length).toBe(rows.length);
			},
			{ timeout: 5000, interval: 50 },
		);
	});

	it("purges the object and the row once retention has run out", async () => {
		const stub = queue();
		const row = await ingest("purge@erp.example.com");
		await makeDue(stub, row.id);
		await runPass(stub);
		expect((await rowOf(stub, row.id)).status).toBe("delivered");
		await runInDurableObject(stub, (_instance, state) => {
			state.storage.sql.exec("UPDATE inbox SET purge_at = ? WHERE id = ?", Date.now() - 1, row.id);
		});
		expect(await runPass(stub)).toBe(true);
		expect(await stub.get(row.id)).toBeNull();
		expect(await env.INBOX.get(row.r2Key)).toBeNull();
		expect(await alarmAt(stub)).toBeNull();
	});

	it("retry requeues a parked row with a fresh budget and leaves a pending one alone", async () => {
		const stub = queue();
		const row = await ingest("reject422@erp.example.com");
		await makeDue(stub, row.id);
		await runPass(stub);
		expect((await rowOf(stub, row.id)).status).toBe("rejected");

		expect(await stub.retry(row.id)).toBe("requeued");
		// The requeue arms the alarm for "now", which the runtime fires on its own; the attempt
		// budget was reset, so the fresh attempt is number 1 and Odoo parks the row again.
		await vi.waitFor(
			async () => {
				const again = await rowOf(stub, row.id);
				expect(again.status).toBe("rejected");
				expect(again.attempts).toBe(1);
			},
			{ timeout: 5000, interval: 50 },
		);

		const fresh = await ingest("waiting@erp.example.com");
		expect(await stub.retry(fresh.id)).toBe("already_pending");
		expect(await stub.retry("01ARZ3NDEKTSV4RRFFQ69G5FAV")).toBe("not_found");
	});

	it("retryAll requeues every row in one parked state", async () => {
		const stub = queue();
		const a = await ingest("reject422@erp.example.com");
		const b = await ingest("redirect@erp.example.com");
		const c = await ingest("fine@erp.example.com");
		for (const row of [a, b, c]) {
			await makeDue(stub, row.id);
		}
		await runPass(stub);
		expect(await stub.retryAll("dead")).toBe(0);
		expect(await stub.retryAll("rejected")).toBe(2);
		await vi.waitFor(
			async () => {
				// Both were attempted again (budget reset → attempt 1) and parked again.
				expect((await rowOf(stub, a.id)).attempts).toBe(1);
				expect((await rowOf(stub, b.id)).attempts).toBe(1);
			},
			{ timeout: 5000, interval: 50 },
		);
		expect((await rowOf(stub, a.id)).status).toBe("rejected");
		expect((await rowOf(stub, c.id)).status).toBe("delivered");
	});

	it("remove drops the row and its object", async () => {
		const stub = queue();
		const row = await ingest("gone@erp.example.com");
		expect(await stub.remove(row.id)).toBe(true);
		expect(await stub.get(row.id)).toBeNull();
		expect(await env.INBOX.get(row.r2Key)).toBeNull();
		expect(await stub.remove(row.id)).toBe(false);
	});

	it("an alarm with nothing due is a no-op that clears itself", async () => {
		const stub = queue();
		await runInDurableObject(stub, (_instance, state) =>
			state.storage.setAlarm(Date.now() + 60_000),
		);
		expect(await runDurableObjectAlarm(stub)).toBe(true);
		expect(await alarmAt(stub)).toBeNull();
	});
});
