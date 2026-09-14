# @avunu/mail-cloudflare-worker

The **Email Routing Worker** half of [`odoo-cloudflare-email`](../): receives every message
Cloudflare Email Routing hands it, keeps the raw RFC 5322 bytes in R2, and pushes them to the
`mail_cloudflare` Odoo module over signed HTTPS with durable, alarm-driven retries.

This package is the **reusable core**. It is published to GitHub Packages and consumed by thin
per-instance wrappers in the private fleet repo. **Deployment lives in the fleet repo, not here.**

> Skeleton stage — each section below is filled in as the corresponding code lands.

## What it exports

_TODO: `createWorker()`, `InboxQueue`, `MailWorkerEnv`; the wrapper snippet._

## How it works

_TODO: store first, then push; the InboxQueue Durable Object; status lifecycle._

## Inbound contract

_TODO: headers (`X-Mail-Cloudflare-*`), signature scheme, Odoo status codes → worker outcomes._

## Ops API

_TODO: `/health`, `/inbox`, `/inbox/:id/raw`, retry, delete._

## Configuration

_TODO: vars vs secrets, defaults, `CF_ACCESS_CLIENT_*`._

## Deployment

_TODO: Email Routing → "Send to a Worker"; `wrangler r2 bucket create`; `wrangler secret put`;
the fleet wrapper's `wrangler.jsonc` (r2_buckets, durable_objects, migrations, vars); `.npmrc`._

## Develop & test

_TODO: `npm install`, `npm run types`, `.dev.vars`, `npm run dev` + `curl … /cdn-cgi/local/email`
with `test/fixtures/simple.eml`; `npm run check`; `npm test`; `npm run build`._

## Publishing

_TODO: Release Please bumps the version and publishes to GitHub Packages on each worker release._
