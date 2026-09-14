# odoo-cloudflare-email

Send and receive Odoo email through Cloudflare, with no SMTP or IMAP provider in the
loop. Two packages live in this repository:

| Package | What | Where | License |
| --- | --- | --- | --- |
| `mail_cloudflare` | Odoo 18 addon: outbound via the Cloudflare Email Sending REST API, inbound via a signed webhook | [`mail_cloudflare/`](mail_cloudflare/) | AGPL-3.0-or-later |
| `@avunu/mail-cloudflare-worker` | Cloudflare Email Worker library: receives routed mail, stores it, pushes it to Odoo with retries | [`worker/`](worker/) | MIT |

```
                 ┌────────────────────────── Cloudflare ──────────────────────────┐
 outbound        │  Email Sending REST API   ◄── HTTPS (API token) ──────┐        │
                 │                                                       │        │
 inbound  MX ──► │  Email Routing ──► Worker email()                     │        │
                 │     │  raw .eml ──► R2 bucket                          │        │
                 │     └─ enqueue ──► InboxQueue Durable Object (SQLite)  │        │
                 │                      │ signed POST (HMAC + timestamp)  │        │
                 └──────────────────────┼───────────────────────────────┼────────┘
                                        ▼                               │
                                      Odoo                              │
                     POST /mail_cloudflare/inbound/<key> ──► fetchmail.server ──► mail.thread
                     ir.mail_server (Cloudflare Email Sending) ─────────────────────────┘
```

- **Outbound.** An `ir.mail_server` of type *Cloudflare Email Sending* turns each message Odoo
  builds into a REST call. Odoo keeps its own `Message-Id` and adds it to `References`, so
  replies still thread even though Cloudflare assigns its own `Message-ID`.
- **Inbound.** Email Routing hands every routed message to the Worker, which stores the raw
  message in R2, queues it, and POSTs it to Odoo signed with a shared secret. Odoo verifies the
  signature and feeds the message to `mail.thread.message_process()` — the same path IMAP
  polling and `odoo-mailgate.py` use. If Odoo is down the Worker retries with backoff; nothing
  is lost.

Setup, the webhook contract and the limitations are documented in each package's README:
[`mail_cloudflare/README.md`](mail_cloudflare/README.md) and
[`worker/README.md`](worker/README.md).

## Repository layout

```
mail_cloudflare/   the Odoo addon — at the root so the repository itself is an addons path
worker/            the Worker library (npm package, published to GitHub Packages)
odoo/              OCB 18.0 (shallow git submodule) — only for local development and the
                   test check; consumers never need it
flake.nix          odoo-nix project: dev shell (Postgres + Odoo + Mailpit), nix checks
pyproject.toml     Python manifest built by uv2nix; `uv.lock` is committed
modules.txt        the module(s) `provision-db` installs
.github/           CI (Check), releases (release-please), the `addons` mirror branch
```

## Using the module in an odoo-nix project

Track the **`addons` branch**, not `main`. `main` carries the OCB submodule for development;
an odoo-nix consumer (`self.submodules = true`) mounting `main` would recurse into that gitlink
and fetch a second copy of OCB. The `addons` branch is mirrored from `main` by CI and holds just
the module:

```sh
git submodule add -b addons https://github.com/Avunu/odoo-cloudflare-email.git modules/odoo-cloudflare-email
echo mail_cloudflare >> modules.txt
odoo-update          # re-aggregates Python deps and re-locks
provision-db         # or: odoo-bin -d <db> -i mail_cloudflare
```

Anyone not on odoo-nix can drop the `mail_cloudflare/` directory (or the zip attached to each
release) into any addons path.

## Development

```sh
direnv allow                  # or: nix develop --no-pure-eval
devenv up                     # Postgres :5433, Odoo :8169, Mailpit :8125 (SMTP :1026)
provision-db                  # create the DB + install mail_cloudflare
```

Ports are deliberately offset from odoo-nix's defaults so this project runs beside another
odoo-nix checkout. The dev shell redirects **all** outgoing mail to Mailpit, including mail sent
through a Cloudflare server (odoo-nix's `dev_mailcatch` intercepts non-SMTP sessions too).

Odoo module:

```sh
odoo-test mail_cloudflare                      # in the dev shell (no browser tours → ODOO_TEST_ALLOW_SKIP=1)
nix build .#checks.x86_64-linux.ruff -L        # lint/format gate
nix build .#checks.x86_64-linux.odoo-tests -L  # what CI runs: sandboxed Postgres + full suite
```

Worker:

```sh
cd worker
npm ci                 # `npx -y npm@11 install` when changing the lockfile (npm 10 chokes on it)
npm run check          # oxfmt + oxlint (+ type-aware) + tsc
npm test               # vitest unit + workerd integration
npm run dev            # wrangler dev; see worker/README.md for the local email recipe
```

Pre-push git hooks (ruff, ruff format, the worker gate) are installed on shell entry. Commits
follow Conventional Commits; release-please turns them into releases.

## Releases

Two independent release streams, both driven by release-please from `main`:

- `vX.Y.Z` — the Odoo module. The manifest version is `18.0.X.Y.Z`; a zip of the module is
  attached to the GitHub release and the `addons` branch is updated.
- `mail-cloudflare-worker-vX.Y.Z` — the Worker, published to GitHub Packages as
  `@avunu/mail-cloudflare-worker`. Deployment happens from the private fleet repository, one
  Worker per Odoo instance.

## License

`mail_cloudflare/` is licensed under the AGPL-3.0-or-later (see [`LICENSE`](LICENSE));
`worker/` under the MIT License (see [`worker/LICENSE`](worker/LICENSE)). Everything else in
the repository is AGPL-3.0-or-later.
