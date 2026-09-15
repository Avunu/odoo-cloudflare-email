# Copyright 2026 Avunu LLC (avu.nu)
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl.html).
"""``ir.mail_server`` extension: the "Cloudflare Email Sending" authentication.

``connect()`` hands core a ``CloudflareSendingSession`` (see
``cloudflare_api``) instead of an SMTP connection; everything else in the
outbound path - ``build_email``, ``_prepare_email_message``, ``send_email``,
``mail.mail._send`` - stays untouched. That is the same seam core's own
``TestingSMTPSession`` uses: the session object only has to expose
``from_filter``/``smtp_from`` (read by ``_prepare_email_message``),
``mail_server_name`` (delivery-failure message), ``send_message`` and
``quit``.

Nothing overrides ``send_email`` on purpose: it keeps returning Odoo's
``Message-Id`` and ``mail.mail`` writes that into ``mail.message.message_id``.
Cloudflare rewrites the ``Message-ID`` and answers with its own id, but one
``mail.mail`` fans out into one send per recipient, so there is no single
Cloudflare id to store anyway - the session only logs it.
"""

from odoo import api, fields, models, modules
from odoo.exceptions import UserError, ValidationError
from odoo.tools import human_size

from odoo.addons.mail_cloudflare import cloudflare_api
from odoo.addons.mail_cloudflare.cloudflare_api import (
    MAX_EMAIL_SIZE_MB,
    CloudflareEmailError,
    CloudflareSendingSession,
)


class IrMailServer(models.Model):
    _inherit = "ir.mail_server"

    smtp_authentication = fields.Selection(
        selection_add=[("cloudflare", "Cloudflare Email Sending")],
        ondelete={"cloudflare": "set default"},
    )
    cloudflare_account_id = fields.Char(
        string="Cloudflare Account ID",
        help="The Cloudflare account that owns the onboarded sending domain "
        "(Account Home > Overview > API section).",
    )
    cloudflare_api_token = fields.Char(
        string="Cloudflare API Token",
        groups="base.group_system",
        help='An API token with the "Email Sending: Edit" permission on the '
        "account above.",
    )

    # -- presentation ---------------------------------------------------------

    @api.depends("smtp_authentication")
    def _compute_smtp_authentication_info(self):
        cloudflare_servers = self.filtered(
            lambda server: server.smtp_authentication == "cloudflare"
        )
        cloudflare_servers.smtp_authentication_info = self.env._(
            "Send through the Cloudflare Email Sending REST API instead of SMTP. \n"
            "The FROM Filtering must be a domain onboarded for Email Sending on "
            "the account: Cloudflare only sends from onboarded domains and "
            "returns per-recipient results synchronously."
        )
        super(
            IrMailServer, self - cloudflare_servers
        )._compute_smtp_authentication_info()

    @api.onchange("smtp_encryption")
    def _onchange_encryption(self):
        # Core resets smtp_port to 25/465 on every encryption change; a
        # Cloudflare server has no SMTP endpoint, so leave it cleared.
        if self.smtp_authentication == "cloudflare":
            return {}
        return super()._onchange_encryption()

    @api.onchange("smtp_authentication")
    def _onchange_smtp_authentication_cloudflare(self):
        if self.smtp_authentication == "cloudflare":
            self.smtp_host = False
            self.smtp_port = False
            self.smtp_encryption = "none"
            # Cloudflare's hard limit; ``mail.mail`` links record attachments
            # out above it instead of embedding them.
            self.max_email_size = MAX_EMAIL_SIZE_MB

    @api.constrains(
        "smtp_authentication",
        "cloudflare_account_id",
        "cloudflare_api_token",
        "from_filter",
    )
    def _check_cloudflare_credentials(self):
        for server in self.filtered(
            lambda server: server.smtp_authentication == "cloudflare"
        ):
            # sudo: the token is a group_system field and the constraint also
            # runs for whoever flips smtp_authentication on an existing record
            server = server.sudo()
            if not server.cloudflare_account_id:
                raise ValidationError(
                    self.env._(
                        "The Cloudflare Account ID is required for the outgoing "
                        "mail server “%s”.",
                        server.name,
                    )
                )
            if not server.cloudflare_api_token:
                raise ValidationError(
                    self.env._(
                        "The Cloudflare API Token is required for the outgoing "
                        "mail server “%s”.",
                        server.name,
                    )
                )
            # Without a filter core would spoof the From header to the
            # notifications address for every sender it cannot match, and
            # that address would then have to be on an onboarded domain too.
            if not server.from_filter:
                raise ValidationError(
                    self.env._(
                        "The outgoing mail server “%s” needs a FROM Filtering set "
                        "to a domain onboarded for Cloudflare Email Sending.",
                        server.name,
                    )
                )

    # -- sending ------------------------------------------------------------

    def _get_max_email_size(self):
        # Consumed by mail.mail._prepare_outgoing_list: record-owned attachments
        # of a bigger mail become download links. Core's fallback is the
        # base.default_max_email_size parameter (10 MiB), which Cloudflare
        # would refuse once the attachments are base64-encoded.
        if self.smtp_authentication == "cloudflare":
            return self.max_email_size or MAX_EMAIL_SIZE_MB
        return super()._get_max_email_size()

    def _cloudflare_session(self, smtp_from=None):
        """The session object ``connect()`` returns for this Cloudflare server."""
        self.ensure_one()
        # sudo: connect() runs as whoever triggers the send and the token is
        # a group_system field; core reads smtp_pass the same way
        server = self.sudo()
        return CloudflareSendingSession(
            server.cloudflare_account_id,
            server.cloudflare_api_token,
            from_filter=server.from_filter,
            smtp_from=smtp_from,
            mail_server_name=server.display_name,
        )

    def connect(
        self,
        host=None,
        port=None,
        user=None,
        password=None,
        encryption=None,
        smtp_from=None,
        ssl_certificate=None,
        ssl_private_key=None,
        smtp_debug=False,
        mail_server_id=None,
        allow_archived=False,
    ):
        """Return a ``CloudflareSendingSession`` for Cloudflare servers.

        Mirrors the resolution at the top of core's ``connect``: nothing is
        opened in test mode, an explicit ``mail_server_id`` wins, and without
        explicit SMTP parameters the server is looked up from ``smtp_from``.
        SMTP servers go through core unchanged.
        """
        if modules.module.current_test:
            return None
        mail_server = None
        if mail_server_id:
            mail_server = self.sudo().browse(mail_server_id)
        elif not host:
            mail_server, smtp_from = self.sudo()._find_mail_server(smtp_from)
        if mail_server and mail_server.smtp_authentication == "cloudflare":
            if not allow_archived and not mail_server.active:
                raise UserError(
                    self.env._(
                        'The server "%s" cannot be used because it is archived.',
                        mail_server.display_name,
                    )
                )
            return mail_server._cloudflare_session(smtp_from=smtp_from)
        return super().connect(
            host=host,
            port=port,
            user=user,
            password=password,
            encryption=encryption,
            smtp_from=smtp_from,
            ssl_certificate=ssl_certificate,
            ssl_private_key=ssl_private_key,
            smtp_debug=smtp_debug,
            # Hand core the server already resolved (with the smtp_from that
            # came with it) so an SMTP send does not search the servers twice.
            mail_server_id=mail_server.id if mail_server else mail_server_id,
            allow_archived=allow_archived,
        )

    def test_smtp_connection(self, autodetect_max_email_size=False):
        """Check the API token for Cloudflare servers, core's handshake for the rest.

        There is no SMTP dialogue to simulate: ``GET /user/tokens/verify``
        proves the token is active. The max email size is not detected but
        known (Cloudflare's 5 MiB limit), so "Detect Max Limit" sets it.
        """
        cloudflare_servers = self.filtered(
            lambda server: server.smtp_authentication == "cloudflare"
        )
        if not cloudflare_servers:
            return super().test_smtp_connection(
                autodetect_max_email_size=autodetect_max_email_size
            )
        other_servers = self - cloudflare_servers
        if other_servers:
            # raises a UserError naming the failing SMTP server
            super(IrMailServer, other_servers).test_smtp_connection(
                autodetect_max_email_size=autodetect_max_email_size
            )
        for server in cloudflare_servers:
            try:
                cloudflare_api.verify_token(server.sudo().cloudflare_api_token)
            except CloudflareEmailError as exc:
                raise UserError(
                    self.env._(
                        "Connection Test Failed for %(server)s!\n%(error)s",
                        server=server.name,
                        error=exc.message,
                    )
                ) from exc
            if autodetect_max_email_size:
                server.max_email_size = MAX_EMAIL_SIZE_MB
        # Same notification core returns, rebuilt here so the details cover
        # every server of a mixed recordset.
        if autodetect_max_email_size:
            message = self.env._(
                "Email maximum size updated (%(details)s).",
                details=", ".join(
                    f"{server.name}: {human_size(server.max_email_size * 1024**2)}"
                    for server in self
                ),
            )
        else:
            message = self.env._("Connection Test Successful!")
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "message": message,
                "type": "success",
                "sticky": False,
                "next": {"type": "ir.actions.act_window_close"},  # force a form reload
            },
        }
