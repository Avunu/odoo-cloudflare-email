import { defineConfig } from "vitest/config";
import { cloudflareTest } from "@cloudflare/vitest-pool-workers";

// Integration tests run inside the real Workers runtime (workerd) so we can exercise R2, the
// InboxQueue Durable Object (SQLite + alarm) and the ops API end-to-end: a message is stored before
// it is pushed, a failed push is retried on the schedule, and a rejected one is not.
//
// Vitest-pool-workers v0.18+ (for vitest 4) exposes its runtime as a Vite plugin,
// `cloudflareTest(workersConfig)`, rather than the older `poolOptions.workers` config.
export default defineConfig({
	plugins: [
		cloudflareTest({
			wrangler: { configPath: "./wrangler.jsonc" },
			miniflare: {
				bindings: {
					ODOO_INBOUND_URL: "https://odoo.test/mail_cloudflare/inbound/testkey",
					ODOO_WEBHOOK_SECRET: "integration-test-secret",
					OPS_TOKEN: "integration-ops-token",
					RETENTION_DAYS: "30",
					MAX_ATTEMPTS: "3",
					BACKOFF_SECONDS: "60,300",
					// High enough that a freshly enqueued message never delivers on its own; tests fire the
					// alarm deliberately with runDurableObjectAlarm().
					DELIVERY_DELAY_SECONDS: "3600",
				},
				// TEMPORARY: the fake Odoo (test/integration/outbound.ts) arrives with the delivery logic.
				// Until then anything outbound is refused rather than allowed through: a test that starts
				// depending on the network should fail loudly.
				outboundService(request: Request): Response {
					return new Response(`unexpected outbound request to ${request.url}`, { status: 502 });
				},
			},
		}),
	],
	test: {
		include: ["test/integration/**/*.test.ts"],
	},
});
