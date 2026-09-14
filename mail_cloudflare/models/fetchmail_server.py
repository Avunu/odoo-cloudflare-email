# Copyright 2026 Avunu LLC (avu.nu)
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl.html).
"""``fetchmail.server`` extension: the "Cloudflare Email Worker" server type.

Nothing is polled: the Email Worker pushes every inbound message to the
``/mail_cloudflare/inbound/<key>`` controller, signed with the server's
webhook secret, and the record only carries the key, the secret and the usual
``object_id``/``attach``/``original`` routing options for ``message_process``.

Reusing ``fetchmail.server`` (rather than a new model) keeps the standard
Incoming Mail Server UI, the ``message_ids`` back-reference on ``mail.mail``
and the ``default_fetchmail_server_id`` context every gateway path already
understands; the price is a handful of overrides that keep the IMAP/POP
machinery (``connect``, ``fetch_mail``, the polling cron) away from records
of this type.
"""

import hashlib
import hmac
import re
import secrets
import time
from urllib.parse import urlsplit

from odoo import SUPERUSER_ID, _, api, fields, models
from odoo.exceptions import UserError

# Path the controller is mounted on; the server's key is the last segment.
INBOUND_PATH = "/mail_cloudflare/inbound"
# Signature scheme the Worker sends as ``X-Mail-Cloudflare-Signature``. The
# ``v1=`` prefix leaves room for rotating the algorithm without breaking
# deployed Workers.
SIGNATURE_SCHEME = "v1"
# Seconds a signed request stays valid on either side of the server clock.
# Wide enough for a Worker attempt queued behind a slow request, narrow enough
# that a captured request cannot be replayed at leisure.
SIGNATURE_TOLERANCE = 300
# Hosts on which a plain-http ``web.base.url`` is accepted for the webhook
# URL: the local ``wrangler dev`` + ``devenv up`` loop has no TLS. Anything
# else must be https, because the URL carries the key and the body carries
# customer mail.
LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")

# Header field names are printable US-ASCII except the colon (RFC 5322 §3.6.8)
# and only ever start a line: folded continuation lines start with whitespace,
# so this cannot match inside a value.
_HEADER_NAME_RE = re.compile(rb"^([\x21-\x39\x3b-\x7e]+):", re.MULTILINE)
_HEADER_BLOCK_END_RE = re.compile(rb"\r?\n\r?\n")
_ENVELOPE_FORBIDDEN_RE = re.compile(r"[\x00-\x1f\x7f]")


def _existing_header_names(body):
    """Lower-cased names of the header fields at the top of ``body``."""
    match = _HEADER_BLOCK_END_RE.search(body)
    head = body[: match.start()] if match else body
    return {name.decode("ascii").lower() for name in _HEADER_NAME_RE.findall(head)}


class FetchmailServer(models.Model):
    _inherit = "fetchmail.server"

    server_type = fields.Selection(
        selection_add=[("cloudflare", "Cloudflare Email Worker")],
        ondelete={"cloudflare": "set default"},
    )
    # The key is the secret segment of the webhook URL (which is why it must
    # be unique) and the secret signs every request; both are generated here,
    # never typed in, and ``copy=False`` gives a duplicated server its own.
    cloudflare_webhook_key = fields.Char(
        string="Webhook Key",
        copy=False,
        index=True,
        readonly=True,
        help="Secret last segment of the webhook URL. Generated when the "
        "server becomes a Cloudflare Email Worker; a duplicated server gets "
        "its own.",
    )
    cloudflare_webhook_secret = fields.Char(
        string="Webhook Secret",
        copy=False,
        readonly=True,
        groups="base.group_system",
        help="Key the Email Worker signs every request with (its "
        "ODOO_WEBHOOK_SECRET). Regenerate it to revoke a leaked secret, then "
        "update the Worker.",
    )
    cloudflare_webhook_url = fields.Char(
        string="Webhook URL",
        compute="_compute_cloudflare_webhook_url",
        help="Where the Email Worker posts inbound messages (its "
        "ODOO_INBOUND_URL). Built from the web.base.url system parameter.",
    )

    _sql_constraints = [  # noqa: RUF012 - Odoo's constraint declaration
        (
            "cloudflare_webhook_key_unique",
            "unique(cloudflare_webhook_key)",
            "The webhook key must be unique per incoming mail server.",
        )
    ]

    # -- computes / onchanges ------------------------------------------------

    @api.depends("server_type", "cloudflare_webhook_key")
    def _compute_cloudflare_webhook_url(self):
        base_url = self.env["ir.config_parameter"].sudo().get_param("web.base.url")
        base_url = (base_url or "").rstrip("/")
        for server in self:
            if server.server_type == "cloudflare" and server.cloudflare_webhook_key:
                server.cloudflare_webhook_url = (
                    f"{base_url}{INBOUND_PATH}/{server.cloudflare_webhook_key}"
                )
            else:
                server.cloudflare_webhook_url = False

    @api.depends("server_type")
    def _compute_server_type_info(self):
        cloudflare_servers = self.filtered(lambda s: s.server_type == "cloudflare")
        cloudflare_servers.server_type_info = _(
            "Messages are pushed by a Cloudflare Email Worker over HTTPS, "
            "nothing is polled. Deploy the Worker with the webhook URL and "
            "secret below, then route the alias domain's addresses to it "
            "(Cloudflare > Email Routing > Send to a Worker)."
        )
        super(FetchmailServer, self - cloudflare_servers)._compute_server_type_info()

    @api.onchange("server_type", "is_ssl", "object_id")
    def onchange_server_type(self):
        """Blank the IMAP/POP connection fields and show the Worker contract."""
        if self.server_type != "cloudflare":
            return super().onchange_server_type()
        self.server = False
        self.port = 0
        self.is_ssl = False
        self.user = False
        self.password = False
        self.configuration = self._cloudflare_configuration()

    @api.model
    def _cloudflare_configuration(self):
        """Contract the deployed Worker follows, as shown in the form.

        Kept free of the URL and secret themselves (they have their own
        fields) so the stored text never goes stale when ``web.base.url``
        changes or the secret is regenerated.
        """
        return f"""Deploy the Cloudflare Email Worker (@avunu/mail-cloudflare-worker) and route
the alias domain's addresses to it: Cloudflare > Email Routing > Send to a Worker.

Worker environment:
  ODOO_INBOUND_URL     the Webhook URL below (wrangler.jsonc "vars")
  ODOO_WEBHOOK_SECRET  the Webhook Secret below (wrangler secret put)

Every inbound message is POSTed to the Webhook URL as Content-Type
message/rfc822 (the raw RFC 5322 message) with these headers:
  X-Mail-Cloudflare-Id             Worker queue id of the message
  X-Mail-Cloudflare-Timestamp      Unix seconds; rejected outside +/- {SIGNATURE_TOLERANCE} s
  X-Mail-Cloudflare-Signature      {SIGNATURE_SCHEME}=<hex HMAC-SHA256(secret, "<timestamp>." + body)>
  X-Mail-Cloudflare-Envelope-From  SMTP MAIL FROM (empty for bounces)
  X-Mail-Cloudflare-Envelope-To    SMTP RCPT TO, the routed address
  X-Mail-Cloudflare-Attempt        delivery attempt, starting at 1

Responses (JSON):
  200  delivered; {{"ok": true, "thread_id": <record id or false>}}
  401  bad or missing signature, stale timestamp     -> Worker marks it rejected
  404  unknown key, server not confirmed or archived -> rejected
  422  no route: no alias matched, no fallback model -> rejected
  500  anything else                                 -> retried with back-off
"""  # noqa: E501 - header/contract lines are meant to be read as a table

    # -- create / write ------------------------------------------------------

    @staticmethod
    def _cloudflare_token():
        # 32 random bytes -> 43 URL-safe characters: fits in a URL segment and
        # in the Worker's secret store, and needs no escaping in either.
        return secrets.token_urlsafe(32)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("server_type") == "cloudflare":
                vals.setdefault("cloudflare_webhook_key", self._cloudflare_token())
                vals.setdefault("cloudflare_webhook_secret", self._cloudflare_token())
                vals.setdefault("configuration", self._cloudflare_configuration())
        return super().create(vals_list)

    def write(self, values):
        if values.get("server_type") != "cloudflare":
            return super().write(values)
        # A server switched to this type gets credentials once; a switch away
        # and back keeps them so the URL the Worker was deployed with stays
        # valid. The contract text is (re)written here as well: the web client
        # does not save readonly fields an onchange filled in.
        result = True
        for server in self:
            server_values = dict(values)
            server_values.setdefault("configuration", self._cloudflare_configuration())
            if not server.cloudflare_webhook_key:
                server_values.setdefault(
                    "cloudflare_webhook_key", self._cloudflare_token()
                )
            if not server.sudo().cloudflare_webhook_secret:
                server_values.setdefault(
                    "cloudflare_webhook_secret", self._cloudflare_token()
                )
            result = super(FetchmailServer, server).write(server_values)
        return result

    def action_regenerate_cloudflare_webhook_secret(self):
        """New signing secret; the key (hence the URL) is left alone so only
        the Worker's ``ODOO_WEBHOOK_SECRET`` needs updating."""
        for server in self:
            server.write({"cloudflare_webhook_secret": self._cloudflare_token()})
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "warning",
                "title": _("Webhook secret regenerated"),
                "message": _(
                    "Update ODOO_WEBHOOK_SECRET in the Email Worker: requests "
                    "signed with the previous secret are rejected from now on."
                ),
                "sticky": True,
            },
        }

    # -- IMAP/POP machinery kept away from Cloudflare servers ----------------

    def connect(self, allow_archived=False):
        """No connection to open: the Worker connects to us."""
        self.ensure_one()
        if self.server_type != "cloudflare":
            return super().connect(allow_archived=allow_archived)
        if not allow_archived and not self.active:
            raise UserError(
                _(
                    'The server "%s" cannot be used because it is archived.',
                    self.display_name,
                )
            )
        return None

    def button_confirm_login(self):
        cloudflare_servers = self.filtered(lambda s: s.server_type == "cloudflare")
        for server in cloudflare_servers:
            if not (
                server.cloudflare_webhook_key
                and server.sudo().cloudflare_webhook_secret
            ):
                raise UserError(
                    _(
                        'The server "%s" has no webhook key or secret. '
                        "Regenerate the secret and try again.",
                        server.display_name,
                    )
                )
            server._cloudflare_check_base_url()
            server.write({"state": "done"})
        return super(FetchmailServer, self - cloudflare_servers).button_confirm_login()

    def _cloudflare_check_base_url(self):
        """The webhook URL must be https (plain http only on a loopback host)."""
        base_url = self.env["ir.config_parameter"].sudo().get_param("web.base.url")
        parts = urlsplit(base_url or "")
        if parts.scheme == "https" or (
            parts.scheme == "http" and parts.hostname in LOOPBACK_HOSTS
        ):
            return
        raise UserError(
            _(
                "The webhook URL is built from the system parameter web.base.url "
                "(%s), which must be an https address: it carries the webhook "
                "key and the Worker delivers your mail to it. Set it to the "
                "public https URL (and freeze it with web.base.url.freeze).",
                base_url or "",
            )
        )

    def fetch_mail(self, raise_exception=True):
        """Cloudflare servers have nothing to fetch; ``_fetch_mails`` (the cron)
        selects every non-local server, so they are filtered out here rather
        than in every caller."""
        return super(
            FetchmailServer, self.filtered(lambda s: s.server_type != "cloudflare")
        ).fetch_mail(raise_exception=raise_exception)

    @api.model
    def _update_cron(self):
        """Core's body with Cloudflare servers excluded from the domain, so a
        Cloudflare-only setup never enables the polling cron. Not chained to
        ``super()``: that would toggle the cron on with core's domain and off
        again with ours on every write."""
        if self.env.context.get("fetchmail_cron_running"):
            return
        try:
            cron = self.env.ref("mail.ir_cron_mail_gateway_action")
            cron.toggle(
                model=self._name,
                domain=[
                    ("state", "=", "done"),
                    ("server_type", "not in", ("local", "cloudflare")),
                ],
            )
        except ValueError:
            pass

    # -- inbound -------------------------------------------------------------

    @api.model
    def _cloudflare_find_by_key(self, key):
        """The confirmed, active Cloudflare server owning ``key`` (or empty)."""
        if not key:
            return self.browse()
        return self.search(
            [
                ("cloudflare_webhook_key", "=", key),
                ("server_type", "=", "cloudflare"),
                ("state", "=", "done"),
            ],
            limit=1,
        )

    def _cloudflare_verify_signature(self, timestamp, signature, body, now=None):
        """Whether ``signature`` is ``v1=<hex HMAC-SHA256(secret, "<ts>." +
        body)>`` for a ``timestamp`` within ``SIGNATURE_TOLERANCE`` of ``now``.

        Any malformed input is a plain ``False``: the controller answers 401
        without saying which check failed.
        """
        self.ensure_one()
        secret = self.sudo().cloudflare_webhook_secret
        if not secret or not timestamp or not signature:
            return False
        if isinstance(body, str):
            body = body.encode()
        timestamp = str(timestamp).strip()
        try:
            issued_at = int(timestamp)
        except ValueError:
            return False
        now = int(time.time()) if now is None else int(now)
        if abs(now - issued_at) > SIGNATURE_TOLERANCE:
            return False
        scheme, _sep, digest = str(signature).strip().partition("=")
        if scheme != SIGNATURE_SCHEME or not digest:
            return False
        # Sign the timestamp exactly as received: the Worker signed the string
        # it sent, and a re-formatted integer would not round-trip "0042".
        expected = hmac.new(
            secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256
        ).hexdigest()
        try:
            return hmac.compare_digest(expected, digest.lower())
        except TypeError:  # non-ASCII digest
            return False

    @api.model
    def _cloudflare_prepare_message(self, body, envelope_from, envelope_to):
        """Prepend the envelope as ``Delivered-To`` / ``Return-Path`` headers
        when the raw message lacks them.

        ``message_parse`` builds ``to``/``recipients`` from ``Delivered-To``
        (``mail_thread.py``, ``message_parse``), which is what alias and
        catchall matching run on, and bounces are addressed to
        ``Return-Path``. The Worker injects the same headers before storing
        the message; this is belt-and-braces for messages posted by anything
        else (``curl``, a replay from the ops API).
        """
        if isinstance(body, str):
            body = body.encode()
        envelope_from = self._cloudflare_check_envelope("envelope-from", envelope_from)
        envelope_to = self._cloudflare_check_envelope("envelope-to", envelope_to)
        present = _existing_header_names(body)
        lines = []
        if envelope_to and "delivered-to" not in present:
            lines.append(f"Delivered-To: {envelope_to}")
        if envelope_from is not None and "return-path" not in present:
            # An empty envelope sender (a bounce) is written as the null path.
            lines.append(f"Return-Path: <{envelope_from}>")
        if not lines:
            return body
        return "".join(f"{line}\r\n" for line in lines).encode() + body

    @staticmethod
    def _cloudflare_check_envelope(name, value):
        """``value`` stripped, or ``None`` when absent; raises ``ValueError``
        on anything that could smuggle a second header line."""
        if value is None:
            return None
        value = str(value)
        if _ENVELOPE_FORBIDDEN_RE.search(value):
            raise ValueError(f"Invalid {name} address: control characters")
        return value.strip()

    def _cloudflare_process_inbound(self, body, envelope_from, envelope_to):
        """Route one raw message the way ``fetch_mail`` routes a fetched one.

        Returns the id of the record the message was posted on, or ``False``
        when ``message_process`` dropped it (duplicate ``Message-Id``, bounce,
        loop) - not an error, and the Worker must not retry those.
        """
        self.ensure_one()
        message = self._cloudflare_prepare_message(body, envelope_from, envelope_to)
        thread_id = (
            self.env["mail.thread"]
            .with_user(SUPERUSER_ID)
            .with_context(
                default_fetchmail_server_id=self.id, fetchmail_cron_running=True
            )
            .message_process(
                self.object_id.model or False,
                message,
                save_original=self.original,
                strip_attachments=not self.attach,
            )
        )
        # ``fetchmail_cron_running`` keeps ``write`` from re-evaluating the
        # polling cron on every delivery.
        self.with_context(fetchmail_cron_running=True).write(
            {"date": fields.Datetime.now()}
        )
        return thread_id or False
