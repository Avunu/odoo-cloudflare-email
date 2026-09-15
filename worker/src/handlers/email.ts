import type { MailWorkerEnv } from "../env";
import { asciiValue, inboxQueue } from "../inbox-do";
import {
	EnvelopeError,
	assertHeaderSafe,
	envelopeHeaders,
	messageIdOf,
	prependHeaders,
} from "../lib/rfc822";
import { logEvent, ulid } from "../lib/util";

export interface EmailHandlerOptions {
	/** R2 key prefix for stored messages, e.g. "inbox/". */
	keyPrefix: string;
}

/** Longest envelope address kept as R2 metadata; the row and the message itself keep the full value. */
const MAX_METADATA_ADDRESS = 256;

/**
 * The Email Routing entry point: store the message, then queue it.
 *
 * Nothing is pushed to Odoo from here. The raw message goes to R2 first, with the SMTP envelope
 * written into it as Delivered-To / Return-Path the way a delivering MTA would, and only then is a
 * queue row created; the Durable Object's alarm does the push. That ordering is the whole guarantee
 * of this worker — once this function returns normally the message exists on disk and will be
 * retried until Odoo takes it or an operator decides otherwise.
 *
 * The two ways out are deliberate. An envelope address that cannot be written into a header
 * (control characters, or longer than a header line) is rejected with a permanent SMTP error: it
 * cannot be stored faithfully, and a sender producing one is not one we want to accept mail from. A
 * failure to store or queue is rethrown so Cloudflare answers the sending MTA with a temporary
 * failure and it retries later — the alternative, accepting and dropping, is the one outcome an
 * inbound gateway must never produce.
 */
export async function handleEmail(
	message: ForwardableEmailMessage,
	env: MailWorkerEnv,
	options: EmailHandlerOptions,
): Promise<void> {
	const { from, to } = message;
	try {
		assertHeaderSafe("Delivered-To", to);
		assertHeaderSafe("Return-Path", from);
	} catch (error) {
		if (error instanceof EnvelopeError) {
			logEvent("warn", "email_rejected", { reason: error.message, size: message.rawSize });
			message.setReject("Invalid envelope address");
			return;
		}
		throw error;
	}

	const id = ulid();
	const receivedAt = Date.now();
	const raw = await new Response(message.raw).arrayBuffer();
	// One concatenation; the message bytes are never decoded (attachments must survive intact).
	const stored = prependHeaders(raw, envelopeHeaders(from, to));
	const messageId = messageIdOf(message.headers);
	const r2Key = `${options.keyPrefix}${id}.eml`;

	try {
		await env.INBOX.put(r2Key, stored, {
			httpMetadata: { contentType: "message/rfc822" },
			// Enough to identify an object from a bucket listing alone, should the queue ever be
			// lost. Custom metadata must be ASCII and is capped in size, hence the encoding and the
			// truncation; the queue row carries the exact values.
			customMetadata: {
				from: asciiValue(from.slice(0, MAX_METADATA_ADDRESS)),
				to: asciiValue(to.slice(0, MAX_METADATA_ADDRESS)),
				...(messageId === null ? {} : { messageId }),
				receivedAt: new Date(receivedAt).toISOString(),
				size: String(stored.byteLength),
			},
		});
		await inboxQueue(env).enqueue({
			id,
			r2Key,
			from,
			to,
			messageId,
			size: stored.byteLength,
			receivedAt,
		});
	} catch (error) {
		logEvent("error", "email_store_failed", {
			id,
			from,
			to,
			size: stored.byteLength,
			message: error instanceof Error ? `${error.name}: ${error.message}` : String(error),
		});
		throw error;
	}

	logEvent("info", "email_stored", { id, from, to, messageId, size: stored.byteLength });
}
