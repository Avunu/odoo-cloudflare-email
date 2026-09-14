/// <reference types="@cloudflare/workers-types" />
import type { MailWorkerEnv } from "./env";

// Public package surface: the worker factory, the ready-made env-driven handler (default), the
// Durable Object class (which the thin wrapper must re-export from its entry so wrangler can bind
// it), and the env contract.
export { InboxQueue } from "./inbox-do";
export type { MailWorkerEnv } from "./env";

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
export function createWorker(_options: MailWorkerOptions = {}): ExportedHandler<MailWorkerEnv> {
	return {
		// TEMPORARY: the store-and-enqueue path (handlers/email.ts) lands in the next stage. Until
		// then every message is bounced with a permanent error rather than accepted and dropped, so
		// a premature deployment is loud instead of lossy.
		email(message: ForwardableEmailMessage, _env: MailWorkerEnv, _ctx: ExecutionContext): void {
			message.setReject("not implemented");
		},
		// TEMPORARY: the ops API (handlers/ops.ts) lands in the next stage.
		fetch(_request: Request, _env: MailWorkerEnv, _ctx: ExecutionContext): Response {
			return new Response(null, { status: 404 });
		},
	};
}

/** The env-driven handler, for wrappers that pass their configuration entirely through wrangler. */
export default createWorker();
