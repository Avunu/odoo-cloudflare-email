import { defineConfig } from "vitest/config";
import { cloudflareTest } from "@cloudflare/vitest-pool-workers";

const captured = new Map<string, { headers: Record<string, string>; body: Uint8Array }>();

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
					DELIVERY_DELAY_SECONDS: "3600",
				},
				async outboundService(request: Request): Promise<Response> {
					const url = new URL(request.url);
					if (url.host === "control.test") {
						const id = url.pathname.split("/").pop() ?? "";
						const hit = captured.get(id);
						if (!hit) return new Response("none", { status: 404 });
						return Response.json({ headers: hit.headers, body: Array.from(hit.body) });
					}
					if (url.host !== "odoo.test" || request.method !== "POST") {
						return new Response(`unexpected outbound request to ${request.url}`, { status: 502 });
					}
					const headers: Record<string, string> = {};
					request.headers.forEach((v, k) => {
						headers[k] = v;
					});
					const body = new Uint8Array(await request.arrayBuffer());
					const id = headers["x-mail-cloudflare-id"] ?? "";
					captured.set(id, { headers, body });
					const to = headers["x-mail-cloudflare-envelope-to"] ?? "";
					const local = to.split("@")[0];
					if (local === "fail500") return Response.json({ ok: false }, { status: 500 });
					if (local === "reject422")
						return Response.json({ ok: false, error: "No possible route found" }, { status: 422 });
					if (local === "redirect")
						return new Response(null, {
							status: 302,
							headers: { Location: "https://elsewhere.test/" },
						});
					return Response.json({ ok: true, thread_id: 42, id });
				},
			},
		}),
	],
	test: { include: ["test/integration/_scratch*.test.ts"] },
});
