// ---------------------------------------------------------------------------
// Response and request helpers for the ops API. Small on purpose: the API is
// a handful of JSON routes behind one bearer token, used by operators and
// scripts, not browsers — so no cookies, no CORS, no content negotiation.
// ---------------------------------------------------------------------------

export function json(data: unknown, status = 200): Response {
	return Response.json(data, { status });
}

/** The one shape every negative answer takes, so scripts can always read `error`. */
export function notFound(message = "not found"): Response {
	return json({ ok: false, error: message }, 404);
}

/**
 * 401 with the challenge RFC 6750 §3 requires. No realm or error detail: the header exists so a
 * generic HTTP client recognises the auth scheme, not to explain what was wrong with the token.
 */
export function unauthorized(): Response {
	return Response.json(
		{ ok: false, error: "unauthorized" },
		{ status: 401, headers: { "WWW-Authenticate": "Bearer" } },
	);
}

/**
 * The token from an `Authorization: Bearer <token>` header, or null when the header is missing or
 * uses another scheme. The scheme is case-insensitive (RFC 9110 §11.1); the token is returned
 * verbatim for the caller's constant-time comparison.
 */
export function bearerToken(request: Request): string | null {
	const header = request.headers.get("Authorization");
	if (header === null) {
		return null;
	}
	const match = /^Bearer\s+(\S+)\s*$/i.exec(header);
	return match?.[1] ?? null;
}

/**
 * A positive integer query parameter, clamped to `max`; `fallback` when it is absent or not a
 * base-10 positive integer. Lenient rather than 400 because these are paging knobs on read-only
 * listings — a typo should show the default page, not an error.
 */
export function intParam(
	params: URLSearchParams,
	name: string,
	fallback: number,
	max: number,
): number {
	const raw = params.get(name);
	if (raw === null || !/^\d+$/.test(raw)) {
		return fallback;
	}
	const value = Number(raw);
	if (!Number.isSafeInteger(value) || value < 1) {
		return fallback;
	}
	return Math.min(value, max);
}
