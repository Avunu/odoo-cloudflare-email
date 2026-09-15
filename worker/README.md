# @avunu/mail-cloudflare-worker

The **Email Routing Worker** half of [`odoo-cloudflare-email`](../): receives every message
Cloudflare Email Routing hands it, keeps the raw RFC 5322 bytes in R2, and pushes them to the
[`mail_cloudflare`](../mail_cloudflare/) Odoo module over signed HTTPS with durable, alarm-driven
retries.

This package is the **reusable core**. It is published to GitHub Packages and consumed by thin
per-instance wrappers in the private fleet repo — one Worker per Odoo instance. **Deployment lives
in the fleet repo, not here.**

## What it exports

```ts
import { createWorker } from "@avunu/mail-cloudflare-worker";

export { InboxQueue } from "@avunu/mail-cloudflare-worker"; // Durable Object — must be re-exported from the entry
export default createWorker();
```

The default export is the same handler with default options. `createWorker({ keyPrefix })` lets
several Workers share one R2 bucket (default prefix `inbox/`). Also exported: the `MailWorkerEnv`
binding/var contract, `loadConfig`/`ConfigError`, the `InboxRecord`/`InboxStatus` types the ops
API returns, and `VERSION`.

## How it works

1. **`email()` stores first.** The SMTP envelope is written into the message as `Delivered-To:`
   and `Return-Path:` (what a delivering MTA would add, and what Odoo's `message_parse` reads for
   recipients and bounces), the bytes go to R2 as `inbox/<ulid>.eml`, and only then is a queue row
   created. An envelope address that cannot be written into a header line (control characters,
   over-long) is rejected with a permanent SMTP error; a failure to store is **rethrown** so
   Cloudflare answers the sending MTA with a temporary failure and it retries — accepting and
   dropping is the one thing an inbound gateway must never do.
2. **The `InboxQueue` Durable Object pushes.** One SQLite-backed instance per Worker holds a row per
   message and delivers from its alarm: a signed `POST` of the stored bytes to Odoo, then a single
   `UPDATE … WHERE status = 'pending'` with the outcome, so an operator retry racing an in-flight
   attempt resolves inside the database. The default schedule retries after 1 m, 5 m, 15 m, 1 h and
   then every 6 h, up to `MAX_ATTEMPTS` (32 ≈ 7 days).
3. **Rows end in one of four states.** `delivered` (Odoo took it; the object is purged after
   `RETENTION_DAYS`), `pending` (retrying), `rejected` (Odoo refused it for a reason a retry cannot
   fix — wrong key, no route — parked for an operator), `dead` (attempts exhausted, or the stored
   object vanished). `rejected` and `dead` rows keep their message until an operator retries or
   deletes them.

## Inbound contract

Each attempt is `POST <ODOO_INBOUND_URL>` with the stored message as the body:

| Header                                    | Value                                                                       |
| ----------------------------------------- | --------------------------------------------------------------------------- |
| `Content-Type`                            | `message/rfc822`                                                            |
| `X-Mail-Cloudflare-Id`                    | the row's ULID (stable across attempts — Odoo can deduplicate on it)        |
| `X-Mail-Cloudflare-Timestamp`             | Unix seconds, fresh per attempt                                             |
| `X-Mail-Cloudflare-Signature`             | `v1=` + hex `HMAC-SHA256(ODOO_WEBHOOK_SECRET, "<timestamp>." + body bytes)` |
| `X-Mail-Cloudflare-Envelope-From` / `-To` | the SMTP envelope (percent-encoded if not printable ASCII)                  |
| `X-Mail-Cloudflare-Attempt`               | 1-based attempt number                                                      |
| `User-Agent`                              | `mail-cloudflare-worker/<version>`                                          |
| `CF-Access-Client-Id` / `-Secret`         | only when `CF_ACCESS_CLIENT_*` are configured                               |

Python check, as the Odoo module does it:

```python
expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
ok = hmac.compare_digest(signature, f"v1={expected}") and abs(time.time() - int(timestamp)) <= 300
```

How Odoo's answer is classified:

| Odoo responds                                              | Row becomes                                                       |
| ---------------------------------------------------------- | ----------------------------------------------------------------- |
| `2xx` (`{"ok": true, "thread_id": …}`)                     | `delivered` — `thread_id` recorded when present                   |
| `408`, `429`, `5xx`, timeout, network error                | `pending` — retried on the schedule; `dead` once attempts run out |
| any other `4xx`, or a `3xx` (redirects are never followed) | `rejected`                                                        |

## Ops API

`GET /health` is public and answers `{"ok":true}`, nothing more. Every other route requires
`Authorization: Bearer <OPS_TOKEN>` and **does not exist** (404, like any unknown path) while
`OPS_TOKEN` is unset.

| Route                                                          | Effect                                                                        |
| -------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| `GET /inbox?status=pending\|delivered\|rejected\|dead&limit=N` | rows, newest first, metadata only (limit ≤ 500)                               |
| `GET /inbox/:id`                                               | one row                                                                       |
| `GET /inbox/:id/raw`                                           | the stored message, streamed as `message/rfc822`                              |
| `POST /inbox/:id/retry`                                        | requeue one parked row with a fresh attempt budget (202; 409 if pending)      |
| `POST /inbox/retry?status=dead\|rejected`                      | requeue every row in that state — after an outage, or after fixing the secret |
| `DELETE /inbox/:id`                                            | drop the row and its object                                                   |

## Configuration

Read from `env` — committed `vars` or `wrangler secret`, the Worker does not care which — and
validated once per alarm pass. A blank value counts as unset.

| Name                                              | Secret | Default                 | Meaning                                                                                      |
| ------------------------------------------------- | ------ | ----------------------- | -------------------------------------------------------------------------------------------- |
| `ODOO_INBOUND_URL`                                | yes    | —                       | the "Webhook URL" of the Cloudflare-typed Incoming Mail Server in Odoo (embeds its key)      |
| `ODOO_WEBHOOK_SECRET`                             | yes    | —                       | the signing secret shown beside it (≥ 16 characters)                                         |
| `OPS_TOKEN`                                       | yes    | unset                   | bearer token for the ops API; unset disables it                                              |
| `CF_ACCESS_CLIENT_ID` / `CF_ACCESS_CLIENT_SECRET` | yes    | unset                   | Access service token, when the inbound route sits behind Cloudflare Access — both or neither |
| `RETENTION_DAYS`                                  |        | `30`                    | how long a delivered message stays in R2; `0` deletes it on delivery                         |
| `MAX_ATTEMPTS`                                    |        | `32`                    | attempts before a row is `dead`                                                              |
| `BACKOFF_SECONDS`                                 |        | `60,300,900,3600,21600` | retry delays; the last one repeats                                                           |
| `DELIVERY_TIMEOUT_SECONDS`                        |        | `30`                    | per-attempt HTTP timeout (max 60)                                                            |
| `DELIVERY_DELAY_SECONDS`                          |        | `0`                     | wait before the first attempt (the tests raise it)                                           |

Bindings: `INBOX` (R2 bucket) and `INBOX_QUEUE` (Durable Object namespace for `InboxQueue`).

## Deployment

In the Cloudflare dashboard, on the zone whose addresses Odoo owns: **Email → Email Routing** →
enable (Cloudflare adds the MX/SPF records) → **Routes** → a custom address or the catch-all →
action **Send to a Worker** → the fleet Worker. Sending is configured separately on the Odoo side
(Email Sending onboarding + an API token; see the module README).

The fleet wrapper is a package with a `.npmrc` (`@avunu:registry=https://npm.pkg.github.com` plus a
token with `read:packages`), the three-line entry above, and a `wrangler.jsonc` of its own:

```jsonc
{
	"name": "mail-worker-acme",
	"main": "src/index.ts",
	"compatibility_date": "2026-07-10",
	"r2_buckets": [{ "binding": "INBOX", "bucket_name": "mail-inbox-acme" }],
	"durable_objects": { "bindings": [{ "name": "INBOX_QUEUE", "class_name": "InboxQueue" }] },
	"migrations": [{ "tag": "v1", "new_sqlite_classes": ["InboxQueue"] }],
	"vars": { "RETENTION_DAYS": "30" },
}
```

Durable Object migrations are per script, so the wrapper must declare the `v1` tag itself
(`new_sqlite_classes` — the queue uses SQLite storage) and never edit it afterwards; a future class
gets a `v2`. Then:

```sh
npx wrangler r2 bucket create mail-inbox-acme
npx wrangler secret put ODOO_INBOUND_URL
npx wrangler secret put ODOO_WEBHOOK_SECRET
npx wrangler secret put OPS_TOKEN                 # optional
npx wrangler secret put CF_ACCESS_CLIENT_ID       # only behind Access
npx wrangler secret put CF_ACCESS_CLIENT_SECRET
npx wrangler deploy
```

No `nodejs_compat` is required: the package uses Web APIs only, so a wrapper cannot break by
omitting the flag.

## Develop & test

```sh
npm ci                     # `npx -y npm@11 install` when changing the lockfile (npm 10 chokes on it)
npm run check              # oxfmt --check, oxlint (+ type-aware), tsc for src and the test harness
npm test                   # vitest unit (Node) + workerd integration (@cloudflare/vitest-pool-workers)
npm run build              # dist/, what gets published
```

The integration suites deliver to a fake Odoo (`test/integration/outbound.ts`) that answers by
recipient (`fail500@…`, `reject422@…`, `slow@…`, …) and records what it was sent, so they verify
the real bytes and headers, signature included. Storage is shared across a file's tests and wiped
before each one.

Against the repository's own dev Odoo (`devenv up` at the root, port 8169): create a Cloudflare
Email Worker incoming server in Odoo, copy its URL and secret into `.dev.vars`
(`cp .dev.vars.example .dev.vars`), then:

```sh
npm run dev
curl -X POST 'http://localhost:8787/cdn-cgi/local/email?from=alice@example.com&to=support@erp.example.com' \
  --data-binary @test/fixtures/simple.eml       # the body must carry a Message-ID header
curl -H "Authorization: Bearer $OPS_TOKEN" 'http://localhost:8787/inbox?status=delivered'
```

## Publishing

Release Please bumps the version (`package.json` and `src/version.ts`) from Conventional Commits and
publishes to GitHub Packages on each `mail-cloudflare-worker-v*` release; see the repository's
`release.yml`.
