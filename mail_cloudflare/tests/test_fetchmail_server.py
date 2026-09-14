# Copyright 2026 Avunu LLC (avu.nu)
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl.html).
"""The Cloudflare ``fetchmail.server`` type, without HTTP.

Everything the controller relies on (credentials, key lookup, signature
verification, envelope header injection, ``message_process`` plumbing) is
exercised directly on the model here; ``test_inbound_controller`` then only
has to prove the wiring over a real request.
"""

import email
import email.policy
import time
from email.message import EmailMessage
from unittest.mock import patch

import psycopg2

from odoo.exceptions import UserError
from odoo.tests import Form
from odoo.tests.common import tagged
from odoo.tools import mute_logger

from odoo.addons.mail_cloudflare.models.fetchmail_server import (
    INBOUND_PATH,
    SIGNATURE_TOLERANCE,
    FetchmailServer,
)
from odoo.addons.mail_cloudflare.tests.common import MIME_TEMPLATE, CloudflareCommon

MAIL_THREAD_LOGGER = "odoo.addons.mail.models.mail_thread"


@tagged("post_install", "-at_install")
class TestFetchmailServer(CloudflareCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.cron = cls.env.ref("mail.ir_cron_mail_gateway_action")
        cls.partner_model = cls.env["ir.model"]._get("res.partner")
        cls.env["ir.config_parameter"].sudo().set_param(
            "web.base.url", "https://odoo.example.com"
        )

    # -- helpers ---------------------------------------------------------------

    def _create_server(self, **values):
        return self.env["fetchmail.server"].create(
            {"name": "Cloudflare Worker", "server_type": "cloudflare", **values}
        )

    def _mime(self, **values):
        values.setdefault("email_from", "Sylvie Lelitre <sylvie@agrolait.com>")
        values.setdefault("to", f"support@{self.alias_domain}")
        values.setdefault("subject", "Inbound subject")
        values.setdefault("msg_id", "<inbound-1@agrolait.com>")
        return self.format(MIME_TEMPLATE, **values).encode()

    def _mime_with_attachment(self, subject, msg_id):
        message = EmailMessage(policy=email.policy.SMTP)
        message["From"] = "Sylvie Lelitre <sylvie@agrolait.com>"
        message["To"] = f"support@{self.alias_domain}"
        message["Subject"] = subject
        message["Message-ID"] = msg_id
        message.set_content("See the attachment")
        message.add_attachment(
            b"%PDF-1.4", maintype="application", subtype="pdf", filename="doc.pdf"
        )
        return message.as_bytes()

    # -- credentials -----------------------------------------------------------

    def test_create_generates_credentials(self):
        server = self._create_server()
        self.assertEqual(server.state, "draft")
        key, secret = server.cloudflare_webhook_key, server.cloudflare_webhook_secret
        # 32 random bytes, URL-safe base64 without padding
        self.assertEqual(len(key), 43)
        self.assertEqual(len(secret), 43)
        self.assertNotEqual(key, secret)
        self.assertNotEqual(key, self._create_server().cloudflare_webhook_key)

    def test_create_other_types_untouched(self):
        server = self.env["fetchmail.server"].create(
            {"name": "IMAP", "server_type": "imap", "server": "imap.example.com"}
        )
        self.assertFalse(server.cloudflare_webhook_key)
        self.assertFalse(server.cloudflare_webhook_secret)
        self.assertFalse(server.cloudflare_webhook_url)

    def test_type_switch_generates_credentials_once(self):
        server = self.env["fetchmail.server"].create(
            {"name": "Was IMAP", "server_type": "imap", "server": "imap.example.com"}
        )
        self.assertFalse(server.configuration)
        server.write({"server_type": "cloudflare"})
        key, secret = server.cloudflare_webhook_key, server.cloudflare_webhook_secret
        self.assertTrue(key)
        self.assertTrue(secret)
        self.assertIn("ODOO_WEBHOOK_SECRET", server.configuration)
        # switching away and back keeps the URL the Worker was deployed with
        server.write({"server_type": "imap"})
        server.write({"server_type": "cloudflare"})
        self.assertEqual(server.cloudflare_webhook_key, key)
        self.assertEqual(server.cloudflare_webhook_secret, secret)

    def test_copy_drops_credentials(self):
        server = self._create_server()
        duplicate = server.copy()
        self.assertEqual(duplicate.server_type, "cloudflare")
        self.assertTrue(duplicate.cloudflare_webhook_key)
        self.assertNotEqual(
            duplicate.cloudflare_webhook_key, server.cloudflare_webhook_key
        )
        self.assertNotEqual(
            duplicate.cloudflare_webhook_secret, server.cloudflare_webhook_secret
        )

    def test_key_unique(self):
        server = self._create_server()
        with (
            self.assertRaises(psycopg2.IntegrityError),
            mute_logger("odoo.sql_db"),
        ):
            self._create_server(cloudflare_webhook_key=server.cloudflare_webhook_key)

    def test_webhook_url(self):
        server = self._create_server()
        self.assertEqual(
            server.cloudflare_webhook_url,
            f"https://odoo.example.com{INBOUND_PATH}/{server.cloudflare_webhook_key}",
        )
        # a trailing slash on web.base.url does not double up
        self.env["ir.config_parameter"].sudo().set_param(
            "web.base.url", "https://odoo.example.com/"
        )
        server.invalidate_recordset(["cloudflare_webhook_url"])
        self.assertEqual(
            server.cloudflare_webhook_url,
            f"https://odoo.example.com{INBOUND_PATH}/{server.cloudflare_webhook_key}",
        )

    def test_regenerate_secret(self):
        server = self._create_server()
        key, secret = server.cloudflare_webhook_key, server.cloudflare_webhook_secret
        action = server.action_regenerate_cloudflare_webhook_secret()
        self.assertEqual(server.cloudflare_webhook_key, key)
        self.assertNotEqual(server.cloudflare_webhook_secret, secret)
        self.assertEqual(len(server.cloudflare_webhook_secret), 43)
        self.assertEqual(action["tag"], "display_notification")
        self.assertIn("ODOO_WEBHOOK_SECRET", action["params"]["message"])

    # -- confirm / fetch / cron ------------------------------------------------

    def test_confirm_without_network(self):
        server = self._create_server()
        with patch("socket.create_connection", side_effect=AssertionError):
            self.assertIsNone(server.connect())
            server.button_confirm_login()
        self.assertEqual(server.state, "done")
        server.set_draft()
        self.assertEqual(server.state, "draft")

    def test_confirm_requires_https(self):
        server = self._create_server()
        self.env["ir.config_parameter"].sudo().set_param(
            "web.base.url", "http://odoo.example.com"
        )
        with self.assertRaises(UserError):
            server.button_confirm_login()
        self.assertEqual(server.state, "draft")
        # plain http is fine on the wrangler dev / devenv loop
        for base_url in ("http://localhost:8069", "http://127.0.0.1:8069"):
            self.env["ir.config_parameter"].sudo().set_param("web.base.url", base_url)
            server.set_draft()
            server.button_confirm_login()
            self.assertEqual(server.state, "done")

    def test_confirm_requires_credentials(self):
        server = self._create_server()
        # bypass the ORM the way a hand-edited row would
        self.env.cr.execute(
            "UPDATE fetchmail_server SET cloudflare_webhook_secret = NULL"
            " WHERE id = %s",
            [server.id],
        )
        server.invalidate_recordset()
        with self.assertRaises(UserError):
            server.button_confirm_login()

    def test_connect_archived(self):
        server = self._create_server(active=False)
        with self.assertRaises(UserError):
            server.connect()
        self.assertIsNone(server.connect(allow_archived=True))

    def test_fetch_mail_skips_cloudflare(self):
        server = self._create_server()
        server.button_confirm_login()
        with patch.object(FetchmailServer, "connect", side_effect=AssertionError):
            self.assertTrue(server.fetch_mail())
            # the cron entry point selects every non-local confirmed server
            self.assertTrue(self.env["fetchmail.server"]._fetch_mails())
        self.assertFalse(server.date)

    def test_cron_stays_inactive(self):
        self.assertFalse(self.cron.active)
        server = self._create_server()
        server.button_confirm_login()
        self.assertFalse(self.cron.active)
        # a confirmed IMAP server still enables it, and only that one counts
        imap = self.env["fetchmail.server"].create(
            {"name": "IMAP", "server_type": "imap", "server": "imap.example.com"}
        )
        imap.write({"state": "done"})
        self.assertTrue(self.cron.active)
        imap.set_draft()
        self.assertFalse(self.cron.active)

    # -- form ------------------------------------------------------------------

    def test_onchange_server_type(self):
        with Form(self.env["fetchmail.server"]) as form:
            form.name = "Cloudflare Worker"
            form.server_type = "imap"
            self.assertEqual(form.port, 143)
            form.server_type = "cloudflare"
            self.assertEqual(form.port, 0)
            self.assertFalse(form.server)
            self.assertFalse(form.is_ssl)
            self.assertIn("Cloudflare Email Worker", form.server_type_info)
            configuration = form.configuration
            for expected in (
                "ODOO_INBOUND_URL",
                "ODOO_WEBHOOK_SECRET",
                "X-Mail-Cloudflare-Timestamp",
                "X-Mail-Cloudflare-Signature",
                "X-Mail-Cloudflare-Envelope-To",
                f"+/- {SIGNATURE_TOLERANCE} s",
                "v1=",
                "401",
                "404",
                "422",
                "500",
            ):
                self.assertIn(expected, configuration)
            # the stored text never embeds the URL or the secret
            self.assertNotIn(INBOUND_PATH, configuration)
        server = form.record
        self.assertEqual(server.server_type, "cloudflare")
        self.assertTrue(server.cloudflare_webhook_key)
        self.assertTrue(server.cloudflare_webhook_secret)
        # persisted server-side: the client never saves the readonly field
        self.assertEqual(server.configuration, configuration)

    # -- key lookup ------------------------------------------------------------

    def test_find_by_key(self):
        Server = self.env["fetchmail.server"]
        server = self._create_server()
        key = server.cloudflare_webhook_key
        self.assertFalse(Server._cloudflare_find_by_key(key), "draft server")
        server.button_confirm_login()
        self.assertEqual(Server._cloudflare_find_by_key(key), server)
        self.assertFalse(Server._cloudflare_find_by_key(False))
        self.assertFalse(Server._cloudflare_find_by_key(""))
        self.assertFalse(Server._cloudflare_find_by_key(key[:-1]))
        server.write({"active": False})
        self.assertFalse(Server._cloudflare_find_by_key(key), "archived server")
        server.write({"active": True, "server_type": "imap"})
        self.assertFalse(Server._cloudflare_find_by_key(key), "other server type")

    # -- signature -------------------------------------------------------------

    def test_verify_signature(self):
        server = self._create_server()
        secret = server.cloudflare_webhook_secret
        body = b"From: a@example.com\r\n\r\nhello"
        now = 1_700_000_000
        good = self._cf_sign(secret, now, body)
        verify = server._cloudflare_verify_signature
        self.assertTrue(verify(str(now), good, body, now=now))
        # tolerance is inclusive on both sides
        self.assertTrue(verify(str(now), good, body, now=now + SIGNATURE_TOLERANCE))
        self.assertTrue(verify(str(now), good, body, now=now - SIGNATURE_TOLERANCE))
        self.assertFalse(
            verify(str(now), good, body, now=now + SIGNATURE_TOLERANCE + 1)
        )
        self.assertFalse(
            verify(str(now), good, body, now=now - SIGNATURE_TOLERANCE - 1)
        )
        # a str body is signed as its UTF-8 bytes, an upper-case digest is fine
        self.assertTrue(verify(str(now), good, body.decode(), now=now))
        self.assertTrue(
            verify(str(now), good.upper().replace("V1", "v1"), body, now=now)
        )
        # tampering
        self.assertFalse(verify(str(now), good, body + b"!", now=now))
        self.assertFalse(verify(str(now + 1), good, body, now=now))
        self.assertFalse(
            verify(str(now), self._cf_sign("other", now, body), body, now=now)
        )
        # scheme / shape
        digest = good.partition("=")[2]
        self.assertFalse(verify(str(now), digest, body, now=now), "no scheme")
        self.assertFalse(verify(str(now), f"v2={digest}", body, now=now))
        self.assertFalse(verify(str(now), "v1=", body, now=now))
        self.assertFalse(verify(str(now), f"v1={digest[:-2]}", body, now=now))
        self.assertFalse(verify(str(now), "v1=é" * 8, body, now=now), "non-ASCII")
        self.assertFalse(verify(str(now), None, body, now=now))
        self.assertFalse(verify(str(now), "", body, now=now))
        self.assertFalse(verify(None, good, body, now=now))
        self.assertFalse(verify("", good, body, now=now))
        self.assertFalse(verify("soon", good, body, now=now))
        self.assertFalse(verify("1.5", good, body, now=now))
        # the default clock is the wall clock
        with patch("time.time", return_value=now + 1.0):
            self.assertTrue(verify(str(now), good, body))

    def test_verify_signature_uses_current_secret(self):
        server = self._create_server()
        body = b"x"
        now = int(time.time())
        old = self._cf_sign(server.cloudflare_webhook_secret, now, body)
        server.action_regenerate_cloudflare_webhook_secret()
        self.assertFalse(server._cloudflare_verify_signature(str(now), old, body))
        new = self._cf_sign(server.cloudflare_webhook_secret, now, body)
        self.assertTrue(server._cloudflare_verify_signature(str(now), new, body))

    def test_verify_signature_no_secret(self):
        server = self._create_server()
        self.env.cr.execute(
            "UPDATE fetchmail_server SET cloudflare_webhook_secret = NULL"
            " WHERE id = %s",
            [server.id],
        )
        server.invalidate_recordset()
        now = int(time.time())
        self.assertFalse(
            server._cloudflare_verify_signature(
                str(now), self._cf_sign("", now, b"x"), b"x"
            )
        )

    # -- envelope headers ------------------------------------------------------

    def test_prepare_message_prepends_envelope(self):
        Server = self.env["fetchmail.server"]
        body = b"From: a@example.com\r\nTo: b@example.com\r\n\r\nhello\r\n"
        prepared = Server._cloudflare_prepare_message(
            body, "alice@example.com", f"support@{self.alias_domain}"
        )
        self.assertEqual(
            prepared,
            f"Delivered-To: support@{self.alias_domain}\r\n"
            "Return-Path: <alice@example.com>\r\n".encode()
            + body,
        )
        message = email.message_from_bytes(prepared, policy=email.policy.SMTP)
        self.assertEqual(message["Delivered-To"], f"support@{self.alias_domain}")
        self.assertEqual(message["Return-Path"], "<alice@example.com>")
        self.assertEqual(message["From"], "a@example.com")
        self.assertEqual(message.get_content().strip(), "hello")
        # ... and message_parse sees the envelope recipient
        parsed = self.env["mail.thread"].message_parse(message)
        self.assertIn(f"support@{self.alias_domain}", parsed["to"])
        self.assertIn(f"support@{self.alias_domain}", parsed["recipients"])

    def test_prepare_message_keeps_existing_headers(self):
        Server = self.env["fetchmail.server"]
        body = (
            b"delivered-to: already@example.com\r\n"
            b"Return-Path: <bounce@example.com>\r\n"
            b"From: a@example.com\r\n\r\nDelivered-To: not-a-header\r\n"
        )
        self.assertEqual(
            Server._cloudflare_prepare_message(body, "x@example.com", "y@example.com"),
            body,
        )
        # only the missing one is added, whatever the line endings
        body = b"Return-Path: <bounce@example.com>\nFrom: a@example.com\n\nhello\n"
        self.assertEqual(
            Server._cloudflare_prepare_message(body, "x@example.com", "y@example.com"),
            b"Delivered-To: y@example.com\r\n" + body,
        )
        # a folded header value is not mistaken for a header name
        body = b"Subject: a\r\n Delivered-To: b\r\n\r\nhello"
        self.assertTrue(
            Server._cloudflare_prepare_message(body, None, "y@example.com").startswith(
                b"Delivered-To: y@example.com\r\n"
            )
        )

    def test_prepare_message_envelope_edge_cases(self):
        Server = self.env["fetchmail.server"]
        body = b"From: a@example.com\r\n\r\nhello"
        # null sender (a bounce) is the null return path
        self.assertEqual(
            Server._cloudflare_prepare_message(body, "", "y@example.com"),
            b"Delivered-To: y@example.com\r\nReturn-Path: <>\r\n" + body,
        )
        # absent envelope headers add nothing
        self.assertEqual(Server._cloudflare_prepare_message(body, None, None), body)
        self.assertEqual(Server._cloudflare_prepare_message(body, None, ""), body)
        # str bodies and whitespace around values are tolerated
        self.assertEqual(
            Server._cloudflare_prepare_message(body.decode(), " x@example.com ", None),
            b"Return-Path: <x@example.com>\r\n" + body,
        )
        # header injection through the envelope is refused
        for bad in ("y@example.com\r\nBcc: z@example.com", "y\n", "y\x00", "y\x7f"):
            with self.assertRaises(ValueError):
                Server._cloudflare_prepare_message(body, "x@example.com", bad)
            with self.assertRaises(ValueError):
                Server._cloudflare_prepare_message(body, bad, "y@example.com")

    # -- processing ------------------------------------------------------------

    def test_process_inbound_fallback_model(self):
        server = self._create_server(object_id=self.partner_model.id)
        server.button_confirm_login()
        self.assertFalse(server.date)
        thread_id = server._cloudflare_process_inbound(
            self._mime(subject="Cloudflare inbound partner"),
            "sylvie@agrolait.com",
            f"support@{self.alias_domain}",
        )
        partner = self.env["res.partner"].browse(thread_id)
        self.assertEqual(partner.name, "Cloudflare inbound partner")
        self.assertEqual(partner.email_normalized, "sylvie@agrolait.com")
        message = partner.message_ids.filtered(lambda m: m.message_type == "email")
        self.assertEqual(len(message), 1)
        self.assertEqual(message.message_id, "<inbound-1@agrolait.com>")
        self.assertIn("Please call me as soon as possible", message.body)
        self.assertTrue(server.date)

    def test_process_inbound_duplicate(self):
        server = self._create_server(object_id=self.partner_model.id)
        server.button_confirm_login()
        body = self._mime(subject="Cloudflare inbound duplicate")
        thread_id = server._cloudflare_process_inbound(body, "s@agrolait.com", None)
        self.assertTrue(thread_id)
        self.assertFalse(
            server._cloudflare_process_inbound(body, "s@agrolait.com", None)
        )
        self.assertEqual(
            self.env["res.partner"].search_count(
                [("name", "=", "Cloudflare inbound duplicate")]
            ),
            1,
        )

    @mute_logger(MAIL_THREAD_LOGGER)
    def test_process_inbound_unroutable(self):
        server = self._create_server()
        server.button_confirm_login()
        with self.assertRaises(ValueError):
            server._cloudflare_process_inbound(
                self._mime(subject="Cloudflare inbound unroutable"),
                "sylvie@agrolait.com",
                f"nobody@{self.alias_domain}",
            )
        self.assertFalse(server.date)

    def test_process_inbound_attachment_options(self):
        """``attach`` / ``original`` reach ``message_process`` like fetch_mail."""
        Partner = self.env["res.partner"]
        server = self._create_server(object_id=self.partner_model.id)
        server.button_confirm_login()
        thread_id = server._cloudflare_process_inbound(
            self._mime_with_attachment("Cloudflare kept", "<kept@agrolait.com>"),
            "sylvie@agrolait.com",
            None,
        )
        self.assertEqual(
            Partner.browse(thread_id).message_ids.attachment_ids.mapped("name"),
            ["doc.pdf"],
        )
        server.write({"original": True})
        thread_id = server._cloudflare_process_inbound(
            self._mime_with_attachment("Cloudflare original", "<orig@agrolait.com>"),
            "sylvie@agrolait.com",
            None,
        )
        self.assertEqual(
            sorted(Partner.browse(thread_id).message_ids.attachment_ids.mapped("name")),
            ["doc.pdf", "original_email.eml"],
        )
        # stripping drops every attachment, the kept original included (core)
        server.write({"attach": False})
        thread_id = server._cloudflare_process_inbound(
            self._mime_with_attachment("Cloudflare stripped", "<strip@agrolait.com>"),
            "sylvie@agrolait.com",
            None,
        )
        self.assertFalse(Partner.browse(thread_id).message_ids.attachment_ids)
