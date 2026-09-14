import { z } from "zod";
import type { MailWorkerVars } from "./env";
import { DEFAULT_BACKOFF_SECONDS, parseSchedule } from "./lib/backoff";

// ---------------------------------------------------------------------------
// Configuration, loaded and validated from Worker vars/secrets.
//
// Everything the delivery path needs is resolved here, once, so the Durable
// Object and the handlers never read `env` piecemeal and never have to agree
// on a default between themselves. Anything missing or malformed throws
// ConfigError rather than letting the worker push unsigned requests or retry
// on a schedule nobody chose. Error messages reference field NAMES only, never
// values: the inbound URL embeds Odoo's per-server key and the rest are secrets.
// ---------------------------------------------------------------------------

/** The var names config.ts reads; anything else on `env` (the bindings) is ignored. */
const VAR_NAMES = [
	"ODOO_INBOUND_URL",
	"ODOO_WEBHOOK_SECRET",
	"CF_ACCESS_CLIENT_ID",
	"CF_ACCESS_CLIENT_SECRET",
	"OPS_TOKEN",
	"RETENTION_DAYS",
	"MAX_ATTEMPTS",
	"BACKOFF_SECONDS",
	"DELIVERY_TIMEOUT_SECONDS",
	"DELIVERY_DELAY_SECONDS",
] as const;

type VarName = (typeof VAR_NAMES)[number];

const DAY_MS = 86_400_000;
const MAX_TIMEOUT_SECONDS = 60;
const MAX_DELAY_SECONDS = 7 * 86_400;
const MAX_RETENTION_DAYS = 3650;
const MAX_ATTEMPTS_CEILING = 10_000;

/**
 * A whole number written in decimal — nothing else. Numbers arrive as strings from wrangler, and
 * `z.coerce.number()` would also accept `1e3`, hex and whitespace, which is more lenience than a
 * retry budget deserves.
 */
const wholeNumber = z
	.string()
	.regex(/^\d+$/, "must be a whole number")
	.transform(Number)
	.pipe(z.number().int().safe());

/**
 * The schedule parser is strict on purpose (see backoff.ts); its TypeError/RangeError becomes a zod
 * issue so the operator sees it under BACKOFF_SECONDS next to any other problem, not as a crash.
 */
const schedule = z.string().transform((raw, ctx): number[] => {
	try {
		return parseSchedule(raw);
	} catch (error) {
		ctx.addIssue({
			code: "custom",
			message: error instanceof Error ? error.message : "invalid backoff schedule",
		});
		return z.NEVER;
	}
});

const EnvSchema = z
	.object({
		// Only http(s): the Odoo controller is an HTTPS route, and `fetch` would silently fail on
		// anything else. Not z.httpUrl(), whose hostname rule rejects `localhost` (wrangler dev).
		ODOO_INBOUND_URL: z.url({ protocol: /^https?$/ }),
		ODOO_WEBHOOK_SECRET: z.string().min(16),
		CF_ACCESS_CLIENT_ID: z.string().optional(),
		CF_ACCESS_CLIENT_SECRET: z.string().optional(),
		OPS_TOKEN: z.string().optional(),
		RETENTION_DAYS: wholeNumber.pipe(z.number().max(MAX_RETENTION_DAYS)).default(30),
		MAX_ATTEMPTS: wholeNumber.pipe(z.number().min(1).max(MAX_ATTEMPTS_CEILING)).default(32),
		BACKOFF_SECONDS: schedule.default([...DEFAULT_BACKOFF_SECONDS]),
		DELIVERY_TIMEOUT_SECONDS: wholeNumber
			.pipe(z.number().min(1).max(MAX_TIMEOUT_SECONDS))
			.default(30),
		DELIVERY_DELAY_SECONDS: wholeNumber.pipe(z.number().max(MAX_DELAY_SECONDS)).default(0),
	})
	.superRefine((v, ctx) => {
		// Half a service token is worse than none: Access would reject every delivery with a 403,
		// which the queue treats as a permanent rejection of each message.
		if ((v.CF_ACCESS_CLIENT_ID === undefined) !== (v.CF_ACCESS_CLIENT_SECRET === undefined)) {
			const missing =
				v.CF_ACCESS_CLIENT_ID === undefined ? "CF_ACCESS_CLIENT_ID" : "CF_ACCESS_CLIENT_SECRET";
			ctx.addIssue({
				code: "custom",
				path: [missing],
				message: "CF_ACCESS_CLIENT_ID and CF_ACCESS_CLIENT_SECRET must be set together",
			});
		}
	});

/** A Cloudflare Access service token, sent on every delivery when the route sits behind Access. */
export interface AccessCredentials {
	readonly clientId: string;
	readonly clientSecret: string;
}

export interface WorkerConfig {
	/** Where deliveries are POSTed. Embeds Odoo's per-server key — never log it. */
	readonly inboundUrl: string;
	readonly webhookSecret: string;
	readonly access: AccessCredentials | null;
	/** Null disables the ops API entirely (every route answers 404). */
	readonly opsToken: string | null;
	/** How long a delivered message stays in R2; 0 deletes it the moment Odoo accepts it. */
	readonly retentionMs: number;
	readonly maxAttempts: number;
	/** Retry delays in seconds, applied by attempt number; the last one repeats. */
	readonly backoffSeconds: readonly number[];
	readonly deliveryTimeoutMs: number;
	/** Wait before the first attempt. Zero in production; tests raise it so alarms fire on demand. */
	readonly deliveryDelayMs: number;
}

export class ConfigError extends Error {
	constructor(fields: string[]) {
		super(`Invalid worker configuration: ${fields.join("; ")}`);
		this.name = "ConfigError";
	}
}

/**
 * The vars as zod should see them: only the known names, and only when they carry something.
 * Wrangler has no way to express "unset" in a `.dev.vars` file or a dashboard form other than
 * leaving the value blank, so an empty or whitespace-only string means "not configured" everywhere
 * — a blank OPS_TOKEN disables the ops API rather than opening it to the empty bearer token.
 */
function presentVars(env: Partial<MailWorkerVars>): Partial<Record<VarName, string>> {
	const out: Partial<Record<VarName, string>> = {};
	for (const name of VAR_NAMES) {
		const value = env[name];
		if (typeof value === "string" && value.trim() !== "") {
			out[name] = value.trim();
		}
	}
	return out;
}

/**
 * The ops token alone, with the same "blank means unset" rule as loadConfig. The ops API reads it
 * through this rather than loadConfig so a deployment whose _delivery_ settings are broken can
 * still be inspected — that is exactly when an operator needs GET /inbox.
 */
export function opsTokenOf(env: Partial<MailWorkerVars>): string | null {
	return presentVars(env).OPS_TOKEN ?? null;
}

export function loadConfig(env: Partial<MailWorkerVars>): WorkerConfig {
	const parsed = EnvSchema.safeParse(presentVars(env));
	if (!parsed.success) {
		throw new ConfigError(
			parsed.error.issues.map((i) => `${i.path.join(".") || "(root)"} ${i.message}`),
		);
	}
	const v = parsed.data;
	return {
		inboundUrl: v.ODOO_INBOUND_URL,
		webhookSecret: v.ODOO_WEBHOOK_SECRET,
		access:
			v.CF_ACCESS_CLIENT_ID !== undefined && v.CF_ACCESS_CLIENT_SECRET !== undefined
				? { clientId: v.CF_ACCESS_CLIENT_ID, clientSecret: v.CF_ACCESS_CLIENT_SECRET }
				: null,
		opsToken: v.OPS_TOKEN ?? null,
		retentionMs: v.RETENTION_DAYS * DAY_MS,
		maxAttempts: v.MAX_ATTEMPTS,
		backoffSeconds: v.BACKOFF_SECONDS,
		deliveryTimeoutMs: v.DELIVERY_TIMEOUT_SECONDS * 1000,
		deliveryDelayMs: v.DELIVERY_DELAY_SECONDS * 1000,
	};
}
