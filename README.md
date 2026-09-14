# odoo-cloudflare-email

Bidirectional email transport for Odoo 18 through Cloudflare: outbound via the
Cloudflare Email Sending API, inbound via an Email Routing Worker that stores
each message and pushes it to Odoo over a signed HTTPS webhook.

## Layout

- `mail_cloudflare/` — the Odoo addon (AGPL-3.0)
- `worker/` — the Cloudflare Email Worker, `@avunu/mail-cloudflare-worker` (MIT)
- `odoo/` — OCB 18.0 shallow submodule, used only for local development and
  the `checks.odoo-tests` flake check
