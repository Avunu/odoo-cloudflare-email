# Copyright 2026 Avunu LLC (avu.nu)
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl.html).
"""The ``ir.mail_server`` extension: configuration, ``connect`` and ``send_email``.

The HTTP behaviour of ``CloudflareSendingSession`` is covered in depth by
``test_cloudflare_api``; here the same scripted responses go through core's
``send_email`` so that what ``mail.mail`` sees (``MailDeliveryException``,
returned ``Message-Id``, no retry on 400, ...) is what is asserted.
"""

import requests

from odoo.exceptions import UserError, ValidationError
from odoo.tests import Form, tagged
from odoo.tools import mute_logger
from odoo.tools.mail import email_split_tuples

from odoo.addons.base.models.ir_mail_server import (
    MailDeliveryException,
    SMTPConnection,
)
from odoo.addons.mail_cloudflare import cloudflare_api
from odoo.addons.mail_cloudflare.cloudflare_api import (
    MAX_EMAIL_SIZE_MB,
    SEND_URL,
    VERIFY_URL,
    CloudflareSendingSession,
)
from odoo.addons.mail_cloudflare.tests.common import CloudflareCommon


@tagged("post_install", "-at_install")
class TestIrMailServer(CloudflareCommon):
    # -- helpers ---------------------------------------------------------------

    def _build(self, **values):
        values.setdefault("email_from", "Sender <sender@cf.example.com>")
        values.setdefault("email_to", ["Alice <alice@example.com>"])
        values.setdefault("subject", "Subject")
        values.setdefault("body", "<p>Hello</p>")
        values.setdefault("subtype", "html")
        return self.env["ir.mail_server"].build_email(**values)

    def _send(self, responses=None, message=None, **kwargs):
        """``send_email`` through the Cloudflare server with scripted responses."""
        message = message or self._build()
        kwargs.setdefault("mail_server_id", self.mail_server_cloudflare.id)
        with self.mock_cloudflare(responses):
            return self.env["ir.mail_server"].send_email(message, **kwargs)

    def _enable_smtp_handshake(self, sender_code=250):
        """Give core's ``TestingSMTPSession`` the SMTP verbs
        ``test_smtp_connection`` drives (it only stubs the sending ones)."""
        session = self.testing_smtp_session
        session.mail = lambda email_from: (sender_code, b"OK")
        session.rcpt = lambda email_to: (250, b"OK")
        session.putcmd = lambda command: None
        session.getreply = lambda: (354, b"Go ahead")
        session.close = lambda: None
        session.esmtp_features = {"size": str(20 * 1024 * 1024)}

    # -- configuration ---------------------------------------------------------

    def test_server_created_by_common(self):
        server = self.mail_server_cloudflare
        self.assertTrue(server, "the cloudflare authentication is registered")
        self.assertEqual(server.smtp_authentication, "cloudflare")
        self.assertEqual(server.cloudflare_account_id, self.CF_ACCOUNT_ID)
        self.assertEqual(server.cloudflare_api_token, self.CF_API_TOKEN)
        self.assertFalse(server.smtp_host)

    def test_constraints(self):
        for missing in ("cloudflare_account_id", "cloudflare_api_token", "from_filter"):
            with (
                self.subTest(missing=missing),
                self.assertRaises(ValidationError),
            ):
                self._create_cloudflare_mail_server(**{missing: False})
        # switching an SMTP server needs the credentials as well
        with self.assertRaises(ValidationError):
            self.mail_server_domain.smtp_authentication = "cloudflare"
        # and a Cloudflare server cannot lose its filter afterwards
        with self.assertRaises(ValidationError):
            self.mail_server_cloudflare.from_filter = False
        # SMTP servers are not concerned
        self.mail_server_domain.from_filter = False

    def test_authentication_info(self):
        self.assertIn(
            "Cloudflare Email Sending",
            self.mail_server_cloudflare.smtp_authentication_info,
        )
        self.assertIn(
            "username and password",
            self.mail_server_domain.smtp_authentication_info,
        )

    def test_form_new_server_defaults(self):
        """The form creates a Cloudflare server without any SMTP setting."""
        form = Form(self.env["ir.mail_server"])
        form.name = "Cloudflare"
        form.from_filter = "cf.example.com"
        form.smtp_authentication = "cloudflare"
        self.assertFalse(form.smtp_host)
        self.assertEqual(form.smtp_port, 0)
        self.assertEqual(form.smtp_encryption, "none")
        self.assertEqual(form.max_email_size, MAX_EMAIL_SIZE_MB)
        form.cloudflare_account_id = self.CF_ACCOUNT_ID
        form.cloudflare_api_token = self.CF_API_TOKEN
        server = form.save()
        self.assertEqual(server.smtp_authentication, "cloudflare")
        self.assertFalse(server.smtp_host)
        self.assertEqual(server.smtp_port, 0)
        self.assertEqual(server.max_email_size, MAX_EMAIL_SIZE_MB)

    def test_form_switch_from_smtp_keeps_port_cleared(self):
        """Switching an SMTP server: the encryption change core reacts to by
        resetting the port must not bring the port back."""
        server = self.mail_server_domain
        server.write({"smtp_encryption": "starttls", "smtp_port": 587})
        with Form(server) as form:
            form.smtp_authentication = "cloudflare"
            self.assertFalse(form.smtp_host)
            self.assertEqual(form.smtp_port, 0)
            self.assertEqual(form.smtp_encryption, "none")
            self.assertEqual(form.max_email_size, MAX_EMAIL_SIZE_MB)
            form.cloudflare_account_id = self.CF_ACCOUNT_ID
            form.cloudflare_api_token = self.CF_API_TOKEN
        self.assertEqual(server.smtp_authentication, "cloudflare")
        self.assertEqual(server.smtp_port, 0)
        self.assertEqual(server.smtp_encryption, "none")

    def test_onchange_encryption_smtp_servers_unchanged(self):
        server = self.mail_server_domain
        server.smtp_encryption = "ssl"
        server._onchange_encryption()
        self.assertEqual(server.smtp_port, 465)
        cloudflare = self.mail_server_cloudflare
        cloudflare.smtp_port = 0
        self.assertEqual(cloudflare._onchange_encryption(), {})
        self.assertEqual(cloudflare.smtp_port, 0)

    def test_get_max_email_size(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "base.default_max_email_size", "12"
        )
        server = self.mail_server_cloudflare
        self.assertFalse(server.max_email_size)
        self.assertEqual(server._get_max_email_size(), MAX_EMAIL_SIZE_MB)
        server.max_email_size = 2.5
        self.assertEqual(server._get_max_email_size(), 2.5)
        # SMTP servers and the "no server" case keep core's fallback
        self.assertEqual(self.mail_server_domain._get_max_email_size(), 12.0)
        self.assertEqual(self.env["ir.mail_server"]._get_max_email_size(), 12.0)

    def test_find_mail_server_by_from_filter(self):
        IrMailServer = self.env["ir.mail_server"]
        server, email_from = IrMailServer._find_mail_server(
            "Sender <sender@cf.example.com>"
        )
        self.assertEqual(server, self.mail_server_cloudflare)
        self.assertEqual(email_from, "Sender <sender@cf.example.com>")
        server, _email_from = IrMailServer._find_mail_server(
            "someone@test.mycompany.com"
        )
        self.assertEqual(server, self.mail_server_domain)
        # a notifications address on the onboarded domain catches the rest
        server, email_from = IrMailServer.with_context(
            domain_notifications_email="notifications@cf.example.com"
        )._find_mail_server('"Name" <test@unknown_domain.com>')
        self.assertEqual(server, self.mail_server_cloudflare)
        self.assertEqual(email_from, "notifications@cf.example.com")

    # -- connect ---------------------------------------------------------------

    def test_connect_returns_session(self):
        server = self.mail_server_cloudflare
        with self.mock_cloudflare():
            session = self.env["ir.mail_server"].connect(
                mail_server_id=server.id, smtp_from="sender@cf.example.com"
            )
            self.assertIsInstance(session, CloudflareSendingSession)
            self.assertEqual(session.from_filter, "cf.example.com")
            self.assertEqual(session.smtp_from, "sender@cf.example.com")
            self.assertEqual(session.mail_server_name, server.display_name)
            session.quit()
        # opening a session is side-effect free
        self.assertEqual(self.cf_requests, [])

    def test_connect_resolves_server_from_smtp_from(self):
        IrMailServer = self.env["ir.mail_server"]
        with self.mock_cloudflare():
            session = IrMailServer.connect(smtp_from="Sender <sender@cf.example.com>")
            self.assertIsInstance(session, CloudflareSendingSession)
            self.assertEqual(session.smtp_from, "Sender <sender@cf.example.com>")
            # the address _find_mail_server settles on is what core would use
            session = IrMailServer.with_context(
                domain_notifications_email="notifications@cf.example.com"
            ).connect(smtp_from='"Name" <test@unknown_domain.com>')
            self.assertIsInstance(session, CloudflareSendingSession)
            self.assertEqual(session.smtp_from, "notifications@cf.example.com")

    def test_connect_none_in_test_mode(self):
        # outside mock_cloudflare, modules.module.current_test is the running test
        self.assertIsNone(
            self.env["ir.mail_server"].connect(
                mail_server_id=self.mail_server_cloudflare.id
            )
        )

    def test_connect_archived(self):
        server = self.mail_server_cloudflare
        server.active = False
        with self.mock_cloudflare():
            with self.assertRaises(UserError):
                self.env["ir.mail_server"].connect(mail_server_id=server.id)
            session = self.env["ir.mail_server"].connect(
                mail_server_id=server.id, allow_archived=True
            )
            self.assertIsInstance(session, CloudflareSendingSession)
        found, _email_from = self.env["ir.mail_server"]._find_mail_server(
            "sender@cf.example.com"
        )
        self.assertNotEqual(found, server)

    def test_connect_smtp_servers_untouched(self):
        IrMailServer = self.env["ir.mail_server"]
        with self.mock_smtplib_connection():
            # core wraps the (mocked) smtplib session in its SMTPConnection
            session = IrMailServer.connect(
                mail_server_id=self.mail_server_domain.id,
                smtp_from="someone@test.mycompany.com",
            )
            self.assertIsInstance(session, SMTPConnection)
            self.assertEqual(session.from_filter, "test.mycompany.com")
            self.assertEqual(session.mail_server_name, "Domain based server")
            session = IrMailServer.connect(smtp_from="someone@test.mycompany.com")
            self.assertIsInstance(session, SMTPConnection)
            self.assertEqual(session.from_filter, "test.mycompany.com")
            self.assertEqual(session.smtp_from, "someone@test.mycompany.com")
            # a full SMTP send still works the way core tests it
            IrMailServer.send_email(
                self._build(email_from="someone@test.mycompany.com"),
                mail_server_id=self.mail_server_domain.id,
            )
        self.assertEqual(self.connect_mocked.call_count, 3)
        # the server resolved for the smtp_from lookup is handed to core:
        # one search, not one here and another one in core's connect
        self.assertEqual(self.find_mail_server_mocked.call_count, 1)
        self.assertSMTPEmailsSent(
            message_from="someone@test.mycompany.com",
            mail_server=self.mail_server_domain,
        )

    # -- send_email ------------------------------------------------------------

    def test_send_email_returns_odoo_message_id(self):
        message = self._build()
        returned = self._send(message=message)
        self.assertEqual(returned, message["Message-Id"])
        self.assertEqual(len(self.cf_requests), 1)
        request = self.cf_requests[0]
        self.assertEqual(request["method"], "POST")
        self.assertEqual(request["url"], SEND_URL.format(account_id=self.CF_ACCOUNT_ID))
        self.assertEqual(
            request["headers"]["Authorization"], f"Bearer {self.CF_API_TOKEN}"
        )
        payload = request["json"]
        self.assertEqual(
            payload["from"], {"address": "sender@cf.example.com", "name": "Sender"}
        )
        self.assertEqual(
            payload["to"], [{"address": "alice@example.com", "name": "Alice"}]
        )
        self.assertEqual(payload["subject"], "Subject")
        # Odoo's id travels in References since Cloudflare rewrites Message-ID
        self.assertEqual(payload["headers"]["References"], str(message["Message-Id"]))
        self.assertEqual(self.cf_sleeps, [])

    def test_send_email_encapsulates_from_on_filter_mismatch(self):
        message = self._build(email_from='"Name" <test@unknown_domain.com>')
        IrMailServer = self.env["ir.mail_server"].with_context(
            domain_notifications_email="notifications@cf.example.com"
        )
        with self.mock_cloudflare():
            IrMailServer.send_email(message)
        # core rewrote the header itself (the old address survives as the name)
        self.assertEqual(
            email_split_tuples(str(message["From"])),
            [("Name", "notifications@cf.example.com")],
        )
        payload = self.cf_requests[0]["json"]
        self.assertEqual(
            payload["from"],
            {"address": "notifications@cf.example.com", "name": "Name"},
        )

    def test_send_email_400_no_retry(self):
        responses = [self._cf_error(400, 10001, "invalid_request_schema")]
        with self.assertRaisesRegex(MailDeliveryException, "invalid_request_schema"):
            self._send(responses)
        self.assertEqual(len(self.cf_requests), 1)
        self.assertEqual(self.cf_sleeps, [])

    def test_send_email_error_names_the_server(self):
        responses = [self._cf_error(400, 10001, "invalid_request_schema")]
        with (
            self.assertRaisesRegex(
                MailDeliveryException, "CloudflareEmailError: Cloudflare refused"
            ) as raised,
            self.assertLogs("odoo.addons.base.models.ir_mail_server", "INFO"),
        ):
            self._send(responses)
        self.assertIn(
            "via SMTP server 'Cloudflare Email Sending'", str(raised.exception)
        )

    def test_send_email_401_fails_fast(self):
        IrMailServer = self.env["ir.mail_server"]
        responses = [self._cf_error(401, 10101, "authentication.unauthorized")]
        with self.mock_cloudflare(responses):
            session = IrMailServer.connect(
                mail_server_id=self.mail_server_cloudflare.id,
                smtp_from="sender@cf.example.com",
            )
            with self.assertRaisesRegex(MailDeliveryException, "HTTP 401"):
                IrMailServer.send_email(self._build(), smtp_session=session)
            # the rest of the batch shares the session and never hits the API
            with self.assertRaisesRegex(MailDeliveryException, "Not retried"):
                IrMailServer.send_email(self._build(), smtp_session=session)
        self.assertEqual(len(self.cf_requests), 1)
        self.assertEqual(self.cf_sleeps, [])

    def test_send_email_429_honours_retry_after(self):
        message = self._build()
        responses = [
            self._cf_error(429, 10004, "rate_limited", headers={"Retry-After": "3"})
        ]
        returned = self._send(responses, message=message)
        self.assertEqual(returned, message["Message-Id"])
        self.assertEqual(len(self.cf_requests), 2)
        self.assertEqual(self.cf_sleeps, [3])

    def test_send_email_5xx_exhausted(self):
        responses = [self._cf_error(500, 10002, "internal_error")] * 3
        with self.assertRaisesRegex(MailDeliveryException, "Giving up after 3"):
            self._send(responses)
        self.assertEqual(len(self.cf_requests), 3)
        self.assertEqual(self.cf_sleeps, [1, 2])

    def test_send_email_network_error_then_success(self):
        message = self._build()
        responses = [requests.ConnectionError("connection reset")]
        returned = self._send(responses, message=message)
        self.assertEqual(returned, message["Message-Id"])
        self.assertEqual(len(self.cf_requests), 2)
        self.assertEqual(self.cf_sleeps, [1])

    def test_send_email_success_false(self):
        body = {
            "success": False,
            "errors": [{"code": 10001, "message": "rejected"}],
            "result": None,
        }
        with self.assertRaisesRegex(MailDeliveryException, "without success: true"):
            self._send([(200, body)])
        self.assertEqual(len(self.cf_requests), 1)

    def test_send_email_all_bounced_raises(self):
        responses = [self._cf_success(permanent_bounces=["alice@example.com"])]
        with self.assertRaisesRegex(
            MailDeliveryException, "No recipient could be reached"
        ):
            self._send(responses)

    def test_send_email_partial_bounce_warns(self):
        message = self._build(email_cc=["Bob <bob@example.com>"])
        responses = [
            self._cf_success(
                delivered=["alice@example.com"], permanent_bounces=["bob@example.com"]
            )
        ]
        with self.assertLogs(cloudflare_api._logger, level="WARNING") as logs:
            returned = self._send(responses, message=message)
        self.assertEqual(returned, message["Message-Id"])
        self.assertTrue(any("bob@example.com" in line for line in logs.output))

    def test_send_email_too_large_before_http(self):
        message = self._build(
            attachments=[
                ("big.bin", b"\x00" * (4 * 1024 * 1024), "application/octet-stream")
            ]
        )
        with self.assertRaisesRegex(MailDeliveryException, "MiB"):
            self._send(message=message)
        self.assertEqual(self.cf_requests, [])

    # -- test_smtp_connection --------------------------------------------------

    def test_smtp_connection_ok(self):
        server = self.mail_server_cloudflare
        with self.mock_cloudflare():
            result = server.test_smtp_connection()
        self.assertEqual(result["type"], "ir.actions.client")
        self.assertEqual(result["tag"], "display_notification")
        self.assertEqual(result["params"]["type"], "success")
        self.assertEqual(result["params"]["message"], "Connection Test Successful!")
        self.assertEqual(len(self.cf_requests), 1)
        request = self.cf_requests[0]
        self.assertEqual(request["method"], "GET")
        self.assertEqual(request["url"], VERIFY_URL)
        self.assertEqual(
            request["headers"]["Authorization"], f"Bearer {self.CF_API_TOKEN}"
        )
        # a plain test does not touch the size
        self.assertFalse(server.max_email_size)

    def test_smtp_connection_bad_token(self):
        responses = [self._cf_error(401, 1000, "Invalid API Token")]
        with (
            self.mock_cloudflare(responses),
            self.assertRaisesRegex(UserError, "Invalid API Token"),
        ):
            self.mail_server_cloudflare.test_smtp_connection()

    def test_smtp_connection_inactive_token(self):
        body = {"success": True, "errors": [], "result": {"status": "expired"}}
        with (
            self.mock_cloudflare([(200, body)]),
            self.assertRaisesRegex(UserError, "not active"),
        ):
            self.mail_server_cloudflare.test_smtp_connection()

    def test_smtp_connection_unreachable(self):
        with (
            self.mock_cloudflare([requests.ConnectionError("dns")]),
            self.assertRaisesRegex(UserError, "Could not reach Cloudflare"),
        ):
            self.mail_server_cloudflare.test_smtp_connection()

    def test_smtp_connection_mixed(self):
        servers = self.mail_server_domain | self.mail_server_cloudflare
        with self.mock_smtplib_connection(), self.mock_cloudflare():
            self._enable_smtp_handshake()
            result = servers.test_smtp_connection()
        self.assertEqual(result["params"]["message"], "Connection Test Successful!")
        # the SMTP server went through core's handshake, the Cloudflare one
        # through the token check
        self.assertEqual(self.connect_mocked.call_count, 1)
        self.assertEqual(len(self.cf_requests), 1)
        self.assertEqual(self.cf_requests[0]["url"], VERIFY_URL)

    @mute_logger("odoo.addons.base.models.ir_mail_server")
    def test_smtp_connection_mixed_smtp_failure(self):
        servers = self.mail_server_domain | self.mail_server_cloudflare
        with self.mock_smtplib_connection(), self.mock_cloudflare():
            self._enable_smtp_handshake(sender_code=550)
            with self.assertRaisesRegex(UserError, "refused the sender address"):
                servers.test_smtp_connection()
        # core's handshake runs first and its failure stops the test
        self.assertEqual(self.cf_requests, [])

    def test_smtp_connection_autodetect(self):
        server = self.mail_server_cloudflare
        with self.mock_cloudflare():
            result = server.action_retrieve_max_email_size()
        self.assertEqual(server.max_email_size, MAX_EMAIL_SIZE_MB)
        self.assertIn("Email maximum size updated", result["params"]["message"])
        self.assertIn(server.name, result["params"]["message"])

    def test_smtp_connection_autodetect_mixed(self):
        servers = self.mail_server_domain | self.mail_server_cloudflare
        with self.mock_smtplib_connection(), self.mock_cloudflare():
            self._enable_smtp_handshake()
            result = servers.test_smtp_connection(autodetect_max_email_size=True)
        self.assertEqual(self.mail_server_domain.max_email_size, 20.0)
        self.assertEqual(self.mail_server_cloudflare.max_email_size, MAX_EMAIL_SIZE_MB)
        message = result["params"]["message"]
        self.assertIn(self.mail_server_domain.name, message)
        self.assertIn(self.mail_server_cloudflare.name, message)
