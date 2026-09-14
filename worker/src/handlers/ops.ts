import { opsTokenOf } from "../config";
import type { MailWorkerEnv } from "../env";
import { inboxQueue, isInboxStatus } from "../inbox-do";
import type { InboxStatus } from "../inbox-do";
import { bearerToken, intParam, json, notFound, unauthorized } from "../lib/http";
import { ULID_PATTERN, timingSafeEqual } from "../lib/util";

/** Default and ceiling for `?limit=` on GET /inbox. */
const LIST_DEFAULT = 50;
const LIST_MAX = 500;

/** `/inbox/<id>`, `/inbox/<id>/raw`, `/inbox/<id>/retry`. The id is validated separately. */
const ITEM_ROUTE = /^\/inbox\/([^/]+)(?:\/(raw|retry))?$/;

/**
 * The ops API behind `fetch()`.
 *
 * `GET /health` is public and says nothing but `{"ok":true}` — enough for an uptime check, not
 * enough to learn what the worker is. Everything else is for operators and scripts, and does not
 * exist unless OPS_TOKEN is set: with no token every other path answers 404, the same as any
 * unknown path, so a deployment that never configured the API is indistinguishable from a worker
 * with no API at all. With a token, a missing or wrong bearer gets a 401; the comparison is
 * constant-time.
 *
 * Routes:
 *
 * - `GET /inbox?status=&limit=` — queue rows, newest first, metadata only.
 * - `GET /inbox/:id` — one row.
 * - `GET /inbox/:id/raw` — the stored message, streamed from R2 as message/rfc822.
 * - `POST /inbox/:id/retry` — requeue one row: 202 requeued, 409 already pending, 404 unknown.
 * - `POST /inbox/retry?status=dead|rejected` — requeue every row in that state (after an outage).
 * - `DELETE /inbox/:id` — drop the row and its R2 object.
 *
 * An `:id` is checked against the ULID alphabet before it goes anywhere near SQL or R2, and every
 * unknown or malformed route is a 404.
 */
export async function handleOps(request: Request, env: MailWorkerEnv): Promise<Response> {
	const url = new URL(request.url);
	const { pathname } = url;
	const { method } = request;

	if (method === "GET" && pathname === "/health") {
		return json({ ok: true });
	}

	const opsToken = opsTokenOf(env);
	if (opsToken === null) {
		return notFound();
	}
	const presented = bearerToken(request);
	if (presented === null || !timingSafeEqual(presented, opsToken)) {
		return unauthorized();
	}

	const queue = inboxQueue(env);

	if (pathname === "/inbox" && method === "GET") {
		const rawStatus = url.searchParams.get("status");
		let status: InboxStatus | null = null;
		if (rawStatus !== null) {
			if (!isInboxStatus(rawStatus)) {
				return json({ ok: false, error: "invalid status" }, 400);
			}
			status = rawStatus;
		}
		const limit = intParam(url.searchParams, "limit", LIST_DEFAULT, LIST_MAX);
		return json({ ok: true, items: await queue.list(status, limit) });
	}

	if (pathname === "/inbox/retry" && method === "POST") {
		const status = url.searchParams.get("status");
		if (status !== "dead" && status !== "rejected") {
			return json({ ok: false, error: "status must be dead or rejected" }, 400);
		}
		const requeued = await queue.retryAll(status);
		return json({ ok: true, status, requeued }, 202);
	}

	const match = ITEM_ROUTE.exec(pathname);
	const id = match?.[1];
	if (id === undefined || !ULID_PATTERN.test(id)) {
		return notFound();
	}
	const action = match?.[2];

	if (action === undefined && method === "GET") {
		const item = await queue.get(id);
		return item === null ? notFound() : json({ ok: true, item });
	}

	if (action === undefined && method === "DELETE") {
		return (await queue.remove(id)) ? json({ ok: true, id }) : notFound();
	}

	if (action === "raw" && method === "GET") {
		const item = await queue.get(id);
		if (item === null) {
			return notFound();
		}
		const object = await env.INBOX.get(item.r2Key);
		if (object === null) {
			return notFound("stored message missing from R2");
		}
		return new Response(object.body, {
			headers: {
				"Content-Type": "message/rfc822",
				"Content-Length": String(object.size),
				"Content-Disposition": `attachment; filename="${id}.eml"`,
			},
		});
	}

	if (action === "retry" && method === "POST") {
		const result = await queue.retry(id);
		if (result === "requeued") {
			return json({ ok: true, id }, 202);
		}
		if (result === "already_pending") {
			return json({ ok: false, error: "already pending" }, 409);
		}
		return notFound();
	}

	return notFound();
}
