import { env } from "cloudflare:workers";
import { beforeEach, describe, expect, it } from "vitest";
import type { MailWorkerEnv } from "../../src/env";
import { signBody } from "../../src/lib/sign";
import { VERSION } from "../../src/version";
import {
	FIXTURE_BYTES,
	FIXTURE_MESSAGE_ID,
	alarmAt,
	captured,
	deliverToWorker,
	ingest,
	makeDue,
	mockMessage,
	queue,
	rowOf,
	runPass,
	resetQueue,
} from "./helpers";
import { ODOO_WEBHOOK_SECRET } from "./outbound";

const DAY_MS = 86_400_000;
const decoder = new TextDecoder();

beforeEach(resetQueue);

describe("email(): store first", () => {
	it("stores the raw message in R2 with the envelope prepended and metadata alongside", async () => {
		const row = await ingest("support@erp.example.com");
		expect(row.status).toBe("pending");
		expect(row.attempts).toBe(0);
		expect(row.messageId).toBe(FIXTURE_MESSAGE_ID);
		expect(row.from).toBe("alice@example.com");
		expect(row.r2Key).toBe(`inbox/${row.id}.eml`);

		const object = await env.INBOX.get(row.r2Key);
		expect(object).not.toBeNull();
		if (object === null) {
			return;
		}
		const stored = new Uint8Array(await object.arrayBuffer());
		const envelope =
			"Delivered-To: support@erp.example.com\r\nReturn-Path: <alice@example.com>\r\n";
		expect(decoder.decode(stored.subarray(0, envelope.length))).toBe(envelope);
		// The original bytes follow untouched.
		expect(stored.subarray(envelope.length)).toEqual(FIXTURE_BYTES);
		expect(row.size).toBe(stored.byteLength);
		expect(object.httpMetadata?.contentType).toBe("message/rfc822");
		expect(object.customMetadata).toMatchObject({
			from: "alice@example.com",
			to: "support@erp.example.com",
			messageId: FIXTURE_MESSAGE_ID,
			size: String(stored.byteLength),
		});
	});

	it("arms the alarm for the configured delivery delay, not for now", async () => {
		const before = Date.now();
		const row = await ingest("delayed@erp.example.com");
		const alarm = await alarmAt(queue());
		expect(alarm).not.toBeNull();
		expect(row.nextAttemptAt).toBeGreaterThanOrEqual(before + 3_600_000);
		expect(alarm).toBeLessThanOrEqual(row.nextAttemptAt ?? 0);
	});

	it("writes the null reverse-path for a bounce", async () => {
		const row = await ingest("bounces@erp.example.com", "");
		const object = await env.INBOX.get(row.r2Key);
		const text = decoder.decode(await object?.arrayBuffer());
		expect(text.startsWith("Delivered-To: bounces@erp.example.com\r\nReturn-Path: <>\r\n")).toBe(
			true,
		);
	});

	it("rejects an envelope that would inject headers, storing nothing", async () => {
		const before = (await env.INBOX.list()).objects.length;
		const message = mockMessage("alice@example.com", "victim@erp.example.com\r\nBcc: evil@x");
		await deliverToWorker(message);
		expect(message.rejectedWith()).toBe("Invalid envelope address");
		expect((await env.INBOX.list()).objects.length).toBe(before);
		expect((await queue().list(null, 1000)).some((r) => r.to.includes("victim"))).toBe(false);
	});

	it("rethrows when the message cannot be stored, so the sender retries", async () => {
		const failingBucket = { put: () => Promise.reject(new Error("r2 down")) };
		const override = { ...env, INBOX: failingBucket } as unknown as MailWorkerEnv;
		await expect(
			deliverToWorker(mockMessage("alice@example.com", "unlucky@erp.example.com"), override),
		).rejects.toThrow("r2 down");
	});
});

describe("delivery", () => {
	it("posts the stored bytes with a valid signature and records Odoo's answer", async () => {
		const stub = queue();
		const row = await ingest("support@erp.example.com");
		await makeDue(stub, row.id);
		expect(await runPass(stub)).toBe(true);

		const after = await rowOf(stub, row.id);
		expect(after.status).toBe("delivered");
		expect(after.attempts).toBe(1);
		expect(after.lastStatus).toBe(200);
		expect(after.threadId).toBe(42);
		expect(after.lastError).toBeNull();
		expect(after.nextAttemptAt).toBeNull();
		expect(after.deliveredAt).not.toBeNull();
		expect(after.purgeAt).toBeGreaterThan(Date.now() + 29 * DAY_MS);
		// Nothing else is pending, so the alarm now waits for the purge.
		expect(await alarmAt(stub)).toBe(after.purgeAt);

		const hit = await captured(row.id);
		expect(hit).not.toBeNull();
		if (hit === null) {
			return;
		}
		expect(hit.method).toBe("POST");
		expect(hit.path).toBe("/mail_cloudflare/inbound/test-server-key");
		const { headers } = hit;
		expect(headers["content-type"]).toBe("message/rfc822");
		expect(headers["user-agent"]).toBe(`mail-cloudflare-worker/${VERSION}`);
		expect(headers["x-mail-cloudflare-id"]).toBe(row.id);
		expect(headers["x-mail-cloudflare-attempt"]).toBe("1");
		expect(headers["x-mail-cloudflare-envelope-from"]).toBe("alice@example.com");
		expect(headers["x-mail-cloudflare-envelope-to"]).toBe("support@erp.example.com");
		expect(headers["cf-access-client-id"]).toBeUndefined();

		const timestamp = Number(headers["x-mail-cloudflare-timestamp"]);
		expect(Math.abs(timestamp - Date.now() / 1000)).toBeLessThan(60);
		const body = new Uint8Array(hit.body);
		expect(headers["x-mail-cloudflare-signature"]).toBe(
			await signBody(ODOO_WEBHOOK_SECRET, timestamp, body),
		);
		const object = await env.INBOX.get(row.r2Key);
		expect(object).not.toBeNull();
		if (object !== null) {
			expect(body).toEqual(new Uint8Array(await object.arrayBuffer()));
		}
	});

	it("keeps a row pending with backoff after a 500 and gives up after MAX_ATTEMPTS", async () => {
		const stub = queue();
		const row = await ingest("fail500@erp.example.com");

		await makeDue(stub, row.id);
		await runPass(stub);
		let after = await rowOf(stub, row.id);
		expect(after.status).toBe("pending");
		expect(after.attempts).toBe(1);
		expect(after.lastStatus).toBe(500);
		expect(after.lastError).toBe("HTTP 500: internal error");
		expect(after.nextAttemptAt).toBeGreaterThan(Date.now() + 55_000);
		expect(after.nextAttemptAt).toBeLessThan(Date.now() + 65_000);
		expect(await alarmAt(stub)).toBe(after.nextAttemptAt);

		await makeDue(stub, row.id);
		await runPass(stub);
		after = await rowOf(stub, row.id);
		expect(after.attempts).toBe(2);
		expect(after.nextAttemptAt).toBeGreaterThan(Date.now() + 295_000);

		await makeDue(stub, row.id);
		await runPass(stub);
		after = await rowOf(stub, row.id);
		expect(after.status).toBe("dead");
		expect(after.attempts).toBe(3);
		expect(after.nextAttemptAt).toBeNull();
		expect((await captured(row.id))?.headers["x-mail-cloudflare-attempt"]).toBe("3");
		// A dead row keeps its message for the operator.
		expect(await env.INBOX.get(row.r2Key)).not.toBeNull();
	});

	it("parks an unroutable message (422) as rejected without retrying", async () => {
		const stub = queue();
		const row = await ingest("reject422@erp.example.com");
		await makeDue(stub, row.id);
		await runPass(stub);
		const after = await rowOf(stub, row.id);
		expect(after.status).toBe("rejected");
		expect(after.attempts).toBe(1);
		expect(after.lastStatus).toBe(422);
		expect(after.lastError).toBe("HTTP 422: No possible route found");
		expect(after.nextAttemptAt).toBeNull();
		expect(await alarmAt(stub)).toBeNull();
	});

	it("never follows a redirect: a 3xx is a rejection", async () => {
		const stub = queue();
		const row = await ingest("redirect@erp.example.com");
		await makeDue(stub, row.id);
		await runPass(stub);
		const after = await rowOf(stub, row.id);
		expect(after.status).toBe("rejected");
		expect(after.lastStatus).toBe(302);
		expect(after.lastError).toBe("HTTP 302");
	});

	it("treats a timeout as transient", async () => {
		const stub = queue();
		const row = await ingest("slow@erp.example.com");
		await makeDue(stub, row.id);
		await runPass(stub);
		const after = await rowOf(stub, row.id);
		expect(after.status).toBe("pending");
		expect(after.attempts).toBe(1);
		expect(after.lastStatus).toBeNull();
		expect(after.lastError).toContain("TimeoutError");
	});

	it("counts a 200 without a thread id as delivered", async () => {
		const stub = queue();
		const ignored = await ingest("nothread@erp.example.com");
		const plain = await ingest("plain200@erp.example.com");
		await makeDue(stub, ignored.id);
		await makeDue(stub, plain.id);
		await runPass(stub);
		for (const row of [ignored, plain]) {
			const after = await rowOf(stub, row.id);
			expect(after.status).toBe("delivered");
			expect(after.threadId).toBeNull();
			expect(after.lastStatus).toBe(200);
		}
	});

	it("marks a row dead when its stored message is gone", async () => {
		const stub = queue();
		const row = await ingest("vanished@erp.example.com");
		await env.INBOX.delete(row.r2Key);
		await makeDue(stub, row.id);
		await runPass(stub);
		const after = await rowOf(stub, row.id);
		expect(after.status).toBe("dead");
		expect(after.lastError).toBe("stored message missing from R2");
		expect(await captured(row.id)).toBeNull();
	});

	it("percent-encodes a non-ASCII envelope on the wire and in R2 metadata", async () => {
		const stub = queue();
		const row = await ingest("josé@erp.example.com");
		await makeDue(stub, row.id);
		await runPass(stub);
		expect((await rowOf(stub, row.id)).status).toBe("delivered");
		expect((await captured(row.id))?.headers["x-mail-cloudflare-envelope-to"]).toBe(
			"jos%C3%A9@erp.example.com",
		);
		const object = await env.INBOX.get(row.r2Key);
		expect(object?.customMetadata?.to).toBe("jos%C3%A9@erp.example.com");
		// The row and the stored message keep the real address.
		expect(row.to).toBe("josé@erp.example.com");
		const text = decoder.decode(await object?.arrayBuffer());
		expect(text.startsWith("Delivered-To: josé@erp.example.com\r\n")).toBe(true);
	});
});
