/// <reference types="@cloudflare/workers-types" />
import type { MailWorkerEnv } from "./env";
import { handleEmail } from "./handlers/email";
import { handleOps } from "./handlers/ops";
import { json } from "./lib/http";
import { logEvent } from "./lib/util";

// Public package surface: the worker factory, the ready-made env-driven handler (default), the
// Durable Object class (which the thin wrapper must re-export from its entry so wrangler can bind
// it), and the env/config/record contracts.
export { InboxQueue } from "./inbox-do";
export type { EnqueueInput, InboxRecord, InboxStatus, RetryResult } from "./inbox-do";
export { ConfigError, loadConfig } from "./config";
export type { AccessCredentials, WorkerConfig } from "./config";
export type { MailWorkerEnv, MailWorkerVars } from "./env";
export { VERSION } from "./version";

export interface MailWorkerOptions {
	/**
	 * R2 key prefix for stored messages (default "inbox/"). Lets a wrapper share one bucket between
	 * several Workers without their objects colliding.
	 */
	keyPrefix?: string;
}

/**
 * Build the Worker handler for one Odoo instance.
 *
 * ```ts
 * export { InboxQueue } from "@avunu/mail-cloudflare-worker";
 * import { createWorker } from "@avunu/mail-cloudflare-worker";
 * export default createWorker();
 * ```
 */
export function createWorker(options: MailWorkerOptions = {}): ExportedHandler<MailWorkerEnv> {
	const keyPrefix = options.keyPrefix ?? "inbox/";
	return {
		// Errors propagate on purpose: an unhandled error here makes Cloudflare answer the sending
		// MTA with a temporary failure, so a message the worker could not store is retried by the
		// sender rather than lost (handleEmail logs the cause before rethrowing).
		async email(message: ForwardableEmailMessage, env: MailWorkerEnv, _ctx: ExecutionContext) {
			await handleEmail(message, env, { keyPrefix });
		},
		async fetch(request: Request, env: MailWorkerEnv, _ctx: ExecutionContext): Promise<Response> {
			try {
				return await handleOps(request, env);
			} catch (error) {
				logEvent("error", "unhandled_error", {
					path: new URL(request.url).pathname,
					message: error instanceof Error ? `${error.name}: ${error.message}` : String(error),
				});
				return json({ ok: false, error: "internal error" }, 500);
			}
		},
	};
}

/** The env-driven handler, for wrappers that pass their configuration entirely through wrangler. */
export default createWorker();
