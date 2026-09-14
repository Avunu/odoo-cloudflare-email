// ---------------------------------------------------------------------------
// Retry policy for the push to Odoo.
//
// Delivery is attempted from a Durable Object alarm, so the schedule is a
// list of delays rather than a formula: operators can read it straight off
// the BACKOFF_SECONDS var and tune it without touching code. The default
// climbs from a minute to six hours so a brief Odoo restart costs one minute
// and an overnight outage does not hammer it, and with MAX_ATTEMPTS = 32 the
// message survives about a week before it is marked dead.
// ---------------------------------------------------------------------------

/** 1 min, 5 min, 15 min, 1 h, then every 6 h. */
export const DEFAULT_BACKOFF_SECONDS: readonly number[] = [60, 300, 900, 3600, 21_600];

/**
 * Parse a BACKOFF_SECONDS var: a comma-separated list of positive integers. Strict on purpose — a
 * silently ignored typo (`60,3OO`) would change retry behaviour in production without any warning,
 * so anything that is not exactly a positive integer is rejected.
 */
export function parseSchedule(raw: string): number[] {
	const schedule: number[] = [];
	for (const item of raw.split(",")) {
		const trimmed = item.trim();
		if (!/^\d+$/.test(trimmed)) {
			throw new TypeError("backoff schedule must be a comma-separated list of positive integers");
		}
		const seconds = Number(trimmed);
		// Zero would re-arm the alarm for "now" and spin on a failing endpoint.
		if (!Number.isSafeInteger(seconds) || seconds < 1) {
			throw new RangeError("backoff schedule entries must be at least 1 second");
		}
		schedule.push(seconds);
	}
	return schedule;
}

/**
 * How long to wait after attempt number `attempt` (1-based) has failed. The schedule's last value
 * repeats for every attempt beyond its length, so a short schedule still retries forever (until
 * MAX_ATTEMPTS) rather than falling off the end.
 */
export function backoffMs(schedule: readonly number[], attempt: number): number {
	const last = schedule.at(-1);
	if (last === undefined) {
		throw new RangeError("backoff schedule must not be empty");
	}
	const index = Math.min(Math.max(attempt, 1), schedule.length) - 1;
	return (schedule[index] ?? last) * 1000;
}

/**
 * What a delivery attempt's HTTP status means for the queue row:
 *
 * - `delivered` — Odoo accepted the message; the row is done.
 * - `retry` — a transient failure; keep the row pending and try again on the schedule.
 * - `rejected` — Odoo answered but will not take the message; keep the row for an operator.
 */
export type Outcome = "delivered" | "retry" | "rejected";

/**
 * 2xx delivered; 408 (request timeout), 429 (rate limited) and every 5xx retry. Everything else is
 * rejected — the other 4xx are Odoo's own verdicts (401 wrong secret, 404 unknown key, 422 no
 * route), and a 3xx means the inbound URL is wrong (deliveries are sent with `redirect: "manual"`,
 * since following a redirect would replay the signed body to an origin nobody vetted). None of
 * those get better by retrying.
 */
export function classifyStatus(status: number): Outcome {
	if (status >= 200 && status < 300) {
		return "delivered";
	}
	if (status === 408 || status === 429 || (status >= 500 && status < 600)) {
		return "retry";
	}
	return "rejected";
}
