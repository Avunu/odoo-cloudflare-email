# Cloudflare Email Transport

Send and receive Odoo email through Cloudflare, with no SMTP or IMAP provider in the loop.
Outbound mail goes to the **Cloudflare Email Sending** REST API straight from `ir.mail_server`;
inbound mail arrives from a **Cloudflare Email Worker** (the companion
[`@avunu/mail-cloudflare-worker`](../worker/) package) over a signed HTTPS webhook and is fed to
`mail.thread.message_process()` — the same path IMAP polling and `odoo-mailgate.py` use, so
aliases, reply threading, bounces and the Incoming Mail Server options all behave as they do today.

## What it does

**Outbound.** An outgoing mail server with authentication *Cloudflare Email Sending* holds an
account id and an API token instead of a host and a password. When Odoo sends, the message it
built is turned into one REST call: `From`/`To`/`Cc` from the headers, `Bcc` from the envelope,
text and HTML bodies, attachments (inline ones by `Content-ID`), `Reply-To`, and the headers
Cloudflare allows (`In-Reply-To`, `References`, `List-*`, `Precedence`, `X-*`, …). Odoo keeps its own
`Message-Id` — the value `mail.message` stores and matches replies against — and adds it to
`References`, because Cloudflare assigns a `Message-ID` of its own; replying clients propagate
`References`, so replies still thread. Rate limits and transient errors are retried briefly; an
invalid token fails the rest of the batch fast; recipients Cloudflare bounces or suppresses
outright put the mail in the *Exception* state with the reason. `Test Connection` verifies the
token. Everything else in the sending path (`mail.mail` queue, failure handling, `From` rewriting
when the sender is not on your domain) is stock Odoo — only `connect()` is overridden.

**Inbound.** An incoming mail server of type *Cloudflare Email Worker* has no host or login; it
carries a **webhook key** (part of the URL) and a **signing secret**, both generated when the
record is created, and shows the exact Worker configuration in its *Configuration* tab. The Worker
stores each routed message, then `POST`s the raw RFC 5322 bytes to
`/mail_cloudflare/inbound/<key>` with an HMAC signature and the SMTP envelope. Odoo verifies the
signature and timestamp, prepends `Delivered-To`/`Return-Path` from the envelope when the message
lacks them (that is how Bcc'd aliases and bounces are recognised), and runs `message_process()`
with the server's fallback model and attachment options. Duplicates are ignored by `Message-Id`,
so a retried push is harmless. Nothing is polled: the standard fetchmail cron stays off unless an
IMAP/POP server exists too.

## Setup

### 1. Cloudflare

- **Email Sending** (Workers Paid plan): *Compute → Email Service → Email Sending → Onboard Domain*
  for every domain you send from (Cloudflare adds the bounce MX/SPF/DKIM/DMARC records). Create
  an account-owned API token with the **Email Sending: Edit** permission and note the account id.
- **Email Routing** (free): enable it on the zone, then route the addresses Odoo should receive
  (or the catch-all) to the deployed Worker with *Send to a Worker*. See the Worker README for the
  deployment itself.

### 2. Outgoing mail server

*Settings → Technical → Outgoing Mail Servers → New*:

| Field | Value |
| --- | --- |
| Authenticate with | Cloudflare Email Sending |
| Cloudflare Account ID / API Token | from step 1 |
| FROM Filtering | the onboarded domain (`example.com`) — required: Cloudflare only sends from onboarded domains, and this is what makes Odoo rewrite a foreign `From` to the notifications address |
| Maximum Email Size | 5 MB (set automatically; Cloudflare's limit for the whole message) |

Click *Test Connection*. Also make sure the company's alias domain (*Settings → General → Discuss →
Alias Domain*) is the domain routed in step 1, so `catchall@`, `bounce@` and `notifications@` exist
on it.

### 3. Incoming mail server

*Settings → Technical → Incoming Mail Servers → New*, type **Cloudflare Email Worker**, optionally a
*Create a New Record* fallback model, then *Test & Confirm*. The form shows the **Webhook URL**
(derived from `web.base.url`, which must be `https://` — plain `http://` is accepted only on
`localhost` for `wrangler dev`) and the **Webhook Secret**; copy them into the Worker's
`ODOO_INBOUND_URL` and `ODOO_WEBHOOK_SECRET`. *Regenerate Secret* rotates the secret only (the URL
stays), so only the Worker's secret needs updating afterwards.

### The webhook contract

`POST /mail_cloudflare/inbound/<key>`, body `message/rfc822`, headers:

| Header | Meaning |
| --- | --- |
| `X-Mail-Cloudflare-Id` | the Worker's queue id (echoed back) |
| `X-Mail-Cloudflare-Timestamp` | Unix seconds; rejected outside ± 300 s |
| `X-Mail-Cloudflare-Signature` | `v1=` + hex `HMAC-SHA256(secret, "<timestamp>." + body)` |
| `X-Mail-Cloudflare-Envelope-From` / `-To` | the SMTP envelope; written into the message as `Return-Path` / `Delivered-To` when absent |

| Response | Meaning | Worker reaction |
| --- | --- | --- |
| `200 {"ok": true, "thread_id": <id or false>}` | processed (`false`: duplicate, bounce or loop, deliberately ignored) | delivered |
| `401` | bad or missing signature, stale timestamp | parked as rejected |
| `404` | unknown key, server not confirmed or archived | parked as rejected |
| `422` | no route: no alias matched and no fallback model | parked as rejected |
| `500` | anything else | retried with back-off |

The route is `auth="none"`; it never creates a session and never reveals why a signature failed.
On a multi-database host it needs `dbfilter`/`db_name` to resolve the database — a `?db=` redirect
would lose the POST body.

## Limitations

- Asynchronous bounces never reach Odoo's `bounce@` alias: `Return-Path` is a Cloudflare-controlled
  header and bounces go to Cloudflare's own `cf-bounce` subdomain. Only the recipients Cloudflare
  rejects synchronously (`permanent_bounces`, `suppressed_recipients`) are surfaced, as a failed
  mail.
- Cloudflare rewrites `Message-ID`. Reply matching relies on the replying client propagating
  `References` (RFC 5322 §3.6.4), which every mainstream client does.
- The whole message must fit Cloudflare's 5 MiB limit. Attachments owned by the business record
  are linked instead of embedded above `Maximum Email Size` (stock Odoo); attachments added in the
  composer are always embedded, and a message that still exceeds the limit fails with a clear
  reason before any request is made.
- A mail forced onto a Cloudflare server (`mail_server_id`) whose `From` is not on the onboarded
  domain is sent as is and refused by Cloudflare — stock Odoo only rewrites `From` when the
  server is chosen through `FROM Filtering`.
- Messages without a `Message-Id` cannot be deduplicated on retry.
- Uninstalling the module resets Cloudflare-typed servers to the default type with no host —
  archive them first.
- Zero-code alternative for sending only: a stock SMTP server at `smtp.mx.cloudflare.net:465`
  (SSL/TLS, user `api_token`, password = the token). No per-recipient result, and whether
  `Message-ID` survives is unverified.

## Development & tests

In the repository's dev shell (`direnv allow`, `devenv up`), with the dev mail catcher off — it is
server-wide and would otherwise swallow every send the tests make:

```sh
ODOO_MAILCATCH_ENABLED=0 python odoo/odoo-bin -c odoo.conf -d mc_test \
  -i mail_cloudflare --test-enable --test-tags /mail_cloudflare --stop-after-init
ruff check mail_cloudflare && ruff format --check mail_cloudflare
```

What CI runs, from the repository root (a sandboxed Postgres and OCB install, no network):

```sh
nix build .#checks.x86_64-linux.ruff -L
nix build .#checks.x86_64-linux.odoo-tests -L
```

The test harness (`tests/common.py`) never opens a connection: `MockCloudflareCase` scripts the
REST responses and captures every request, and the controller tests sign real HTTP requests
against the running test server.

## Related

- [`../worker/`](../worker/) — the Cloudflare Email Worker this module receives from.
- [Cloudflare Email Service docs](https://developers.cloudflare.com/email-service/) — sending API,
  header allow-list, limits, Email Routing.
