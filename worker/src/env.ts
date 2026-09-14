/// <reference types="@cloudflare/workers-types" />
import type { InboxQueue } from "./inbox-do";

/**
 * Bindings + configuration every deployment provides. Config values may arrive as committed `vars`
 * or as per-Worker secrets — the worker reads them identically through `env`, so this interface
 * only notes which ones must never be committed. Consumers (thin wrappers) supply these via their
 * `wrangler.jsonc` bindings/vars plus `wrangler secret` for the sensitive ones. Everything here is
 * the raw string form; parsing, defaults and validation live in config.ts.
 */
export interface MailWorkerEnv {
	// --- Bindings ---
	/** Raw inbound messages, one `inbox/<ulid>.eml` object each, written before any delivery attempt. */
	INBOX: R2Bucket;
	/** The delivery queue. A single instance, addressed as idFromName("inbox"). */
	INBOX_QUEUE: DurableObjectNamespace<InboxQueue>;

	// --- Odoo (secrets — the URL embeds the per-server key, so it is never logged) ---
	/** `https://<odoo>/mail_cloudflare/inbound/<key>`, copied from the Incoming Mail Server form. */
	ODOO_INBOUND_URL: string;
	/** HMAC-SHA256 key behind the X-Mail-Cloudflare-Signature header (≥ 16 characters). */
	ODOO_WEBHOOK_SECRET: string;
	/**
	 * Cloudflare Access service token, for when the inbound route sits behind an Access policy. Sent
	 * as CF-Access-Client-Id / CF-Access-Client-Secret. Both or neither.
	 */
	CF_ACCESS_CLIENT_ID?: string;
	CF_ACCESS_CLIENT_SECRET?: string;

	// --- Ops API (secret) ---
	/** Bearer token for the /inbox routes. Unset = every ops route answers 404. */
	OPS_TOKEN?: string;

	// --- Tunables (committed vars) ---
	/** Days a delivered message stays in R2 (default 30; 0 deletes the object on delivery). */
	RETENTION_DAYS?: string;
	/** Attempts before a message is marked dead (default 32 ≈ 7 days on the default schedule). */
	MAX_ATTEMPTS?: string;
	/**
	 * Comma-separated retry delays in seconds; the last value repeats (default
	 * 60,300,900,3600,21600).
	 */
	BACKOFF_SECONDS?: string;
	/** Per-attempt HTTP timeout in seconds (default 30, capped at 60). */
	DELIVERY_TIMEOUT_SECONDS?: string;
	/** Delay before the first attempt (default 0). Tests set it high so alarms only fire on demand. */
	DELIVERY_DELAY_SECONDS?: string;
}
