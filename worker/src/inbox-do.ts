import { DurableObject } from "cloudflare:workers";
import type { MailWorkerEnv } from "./env";

// Placeholder so wrangler and miniflare can resolve the INBOX_QUEUE binding while the skeleton is
// wired up. The queue itself — SQLite schema, enqueue, alarm-driven delivery, purge, and the ops
// methods — lands in the next stage.
export class InboxQueue extends DurableObject<MailWorkerEnv> {}
