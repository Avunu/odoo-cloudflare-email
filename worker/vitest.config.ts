import { defineConfig } from "vitest/config";

// Unit tests exercise the pure signing / RFC 5322 / backoff / config logic. Node 20+ provides the
// same WebCrypto SubtleCrypto primitives (HMAC, SHA-256) as the Workers Runtime, so these run in
// plain Node. Durable Object / handler integration tests use @cloudflare/vitest-pool-workers instead.
export default defineConfig({
	test: {
		include: ["test/unit/**/*.test.ts"],
		environment: "node",
	},
});
