# Copyright 2026 Avunu LLC (avu.nu)
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl.html).
"""``POST /mail_cloudflare/inbound/<key>`` over a real HTTP request.

Requests are built by ``MockCloudflareCase._cf_post`` exactly the way the
Email Worker builds them (raw ``message/rfc822`` body, ``X-Mail-Cloudflare-*``
headers, ``v1=<hex HMAC-SHA256(secret, "<timestamp>." + body)>``), so the
signature the controller accepts here is the one the Worker computes.

``res.partner`` is the fallback model: it is the only business ``mail.thread``
model the ``mail`` module alone guarantees, and its ``message_new`` creates a
partner named after the subject.
"""

import time
from unittest.mock import patch

from odoo.tests.common import HttpCase, tagged
from odoo.tools import mute_logger

from odoo.addons.mail_cloudflare.controllers.main import (
    HEADER_ENVELOPE_TO,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
)
from odoo.addons.mail_cloudflare.models.fetchmail_server import (
    SIGNATURE_TOLERANCE,
    FetchmailServer,
)
from odoo.addons.mail_cloudflare.tests.common import (
    CF_INBOUND_ROUTE,
    MIME_TEMPLATE,
    CloudflareCommon,
)

CONTROLLER_LOGGER = "odoo.addons.mail_cloudflare.controllers.main"
MAIL_THREAD_LOGGER = "odoo.addons.mail.models.mail_thread"


@tagged("post_install", "-at_install")
class TestInboundController(CloudflareCommon, HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.partner_model = cls.env["ir.model"]._get("res.partner")
        cls.server = cls.env["fetchmail.server"].create(
            {
                "name": "Cloudflare Worker",
                "server_type": "cloudflare",
                "object_id": cls.partner_model.id,
            }
        )
        # HttpCase points web.base.url at http://127.0.0.1:<port>, a loopback
        # host, so the real confirmation path runs here.
        cls.server.button_confirm_login()
        # same routing options, no fallback model: only an alias can route
        cls.server_no_fallback = cls.env["fetchmail.server"].create(
            {"name": "Cloudflare Worker (aliases only)", "server_type": "cloudflare"}
        )
        cls.server_no_fallback.button_confirm_login()
        cls.support = f"support@{cls.alias_domain}"

    # -- helpers ---------------------------------------------------------------

    def _mime(self, **values):
        values.setdefault("email_from", "Sylvie Lelitre <sylvie@agrolait.com>")
        values.setdefault("to", self.support)
        values.setdefault("subject", "Cloudflare inbound")
        values.setdefault("msg_id", f"<{time.time():.7f}-inbound@agrolait.com>")
        return self.format(MIME_TEMPLATE, **values).encode()

    def _post(self, body=None, server=None, **kwargs):
        kwargs.setdefault("envelope_to", self.support)
        return self._cf_post(server or self.server, body or self._mime(), **kwargs)

    def _partner(self, subject):
        return self.env["res.partner"].search([("name", "=", subject)])

    def assertJson(self, response, status, **expected):
        self.assertEqual(response.status_code, status, response.text)
        self.assertIn("application/json", response.headers["Content-Type"])
        payload = response.json()
        self.assertEqual(payload["ok"], status == 200)
        for key, value in expected.items():
            self.assertEqual(payload.get(key), value, payload)
        return payload

    # -- delivered -------------------------------------------------------------

    def test_inbound_creates_record(self):
        self.assertFalse(self.server.date)
        response = self._post(
            self._mime(subject="Cloudflare inbound new", msg_id="<new@agrolait.com>"),
            worker_id="01JWORKERID000000000000000",
        )
        partner = self._partner("Cloudflare inbound new")
        self.assertEqual(len(partner), 1)
        payload = self.assertJson(
            response, 200, thread_id=partner.id, id="01JWORKERID000000000000000"
        )
        self.assertNotIn("error", payload)
        self.assertEqual(partner.email_normalized, "sylvie@agrolait.com")
        message = partner.message_ids.filtered(lambda m: m.message_type == "email")
        self.assertEqual(message.message_id, "<new@agrolait.com>")
        self.assertIn("Please call me as soon as possible", message.body)
        # the delivery timestamp lands on the server, written by the request
        self.server.invalidate_recordset(["date"])
        self.assertTrue(self.server.date)

    def test_inbound_reply_threads(self):
        self._post(
            self._mime(
                subject="Cloudflare inbound thread", msg_id="<root@agrolait.com>"
            )
        )
        partner = self._partner("Cloudflare inbound thread")
        self.assertEqual(len(partner), 1)
        response = self._post(
            self._mime(
                subject="Re: Cloudflare inbound thread",
                msg_id="<reply@agrolait.com>",
                extra="In-Reply-To: <root@agrolait.com>",
            )
        )
        self.assertJson(response, 200, thread_id=partner.id)
        self.assertFalse(self._partner("Re: Cloudflare inbound thread"))
        partner.invalidate_recordset(["message_ids"])
        emails = partner.message_ids.filtered(lambda m: m.message_type == "email")
        self.assertEqual(
            sorted(emails.mapped("message_id")),
            ["<reply@agrolait.com>", "<root@agrolait.com>"],
        )
        reply = emails.filtered(lambda m: m.message_id == "<reply@agrolait.com>")
        self.assertEqual(reply.parent_id.message_id, "<root@agrolait.com>")

    def test_inbound_alias_via_envelope_to(self):
        """The envelope recipient becomes ``Delivered-To`` and routes an alias
        the ``To`` header does not name."""
        self.env["mail.alias"].create(
            {
                "alias_domain_id": self.mail_alias_domain.id,
                "alias_contact": "everyone",
                "alias_model_id": self.partner_model.id,
                "alias_name": "cf-support",
            }
        )
        body = self._mime(
            subject="Cloudflare inbound alias",
            to="someone@elsewhere.example.com",
            msg_id="<alias@agrolait.com>",
        )
        response = self._post(
            body,
            server=self.server_no_fallback,
            envelope_to=f"cf-support@{self.alias_domain}",
        )
        partner = self._partner("Cloudflare inbound alias")
        self.assertEqual(len(partner), 1)
        self.assertJson(response, 200, thread_id=partner.id)
        # without the envelope there is nothing to match: not routed at all
        with mute_logger(MAIL_THREAD_LOGGER):
            response = self._post(
                self._mime(
                    subject="Cloudflare inbound no envelope",
                    to="someone@elsewhere.example.com",
                    msg_id="<alias-2@agrolait.com>",
                ),
                server=self.server_no_fallback,
                headers={HEADER_ENVELOPE_TO: None},
            )
        self.assertJson(response, 422)
        self.assertFalse(self._partner("Cloudflare inbound no envelope"))

    def test_inbound_duplicate_message_id(self):
        body = self._mime(
            subject="Cloudflare inbound twice", msg_id="<dup@agrolait.com>"
        )
        first = self.assertJson(self._post(body), 200)
        self.assertTrue(first["thread_id"])
        # a Worker retry after a timeout is harmless
        self.assertJson(self._post(body, attempt=2), 200, thread_id=False)
        self.assertEqual(len(self._partner("Cloudflare inbound twice")), 1)

    # -- rejected --------------------------------------------------------------

    @mute_logger(CONTROLLER_LOGGER)
    def test_inbound_bad_signature(self):
        body = self._mime(subject="Cloudflare inbound forged")
        secret = self.server.cloudflare_webhook_secret
        now = int(time.time())
        good = self._cf_sign(secret, now, body)
        for signature in (
            "v1=" + "0" * 64,
            good.replace("v1=", "v2="),
            good.partition("=")[2],
            self._cf_sign("not-the-secret", now, body),
            self._cf_sign(secret, now, body + b"!"),
            self._cf_sign(secret, now + 1, body),
        ):
            response = self._post(body, timestamp=now, signature=signature)
            self.assertJson(response, 401, error="invalid signature")
        self.assertFalse(self._partner("Cloudflare inbound forged"))
        # the unmodified signature for that timestamp passes
        self.assertJson(self._post(body, timestamp=now, signature=good), 200)

    @mute_logger(CONTROLLER_LOGGER)
    def test_inbound_stale_timestamp(self):
        body = self._mime(subject="Cloudflare inbound stale")
        now = int(time.time())
        for timestamp in (
            now - SIGNATURE_TOLERANCE - 5,
            now + SIGNATURE_TOLERANCE + 5,
            "yesterday",
        ):
            response = self._post(body, timestamp=timestamp)
            self.assertJson(response, 401)
        # a properly signed timestamp inside the window still passes
        self.assertJson(self._post(body, timestamp=now - SIGNATURE_TOLERANCE + 5), 200)

    @mute_logger(CONTROLLER_LOGGER)
    def test_inbound_missing_headers(self):
        body = self._mime(subject="Cloudflare inbound headless")
        self.assertJson(self._post(body, signature=False), 401)
        self.assertJson(self._post(body, headers={HEADER_TIMESTAMP: None}), 401)
        self.assertJson(self._post(body, headers={HEADER_SIGNATURE: ""}), 401)
        self.assertFalse(self._partner("Cloudflare inbound headless"))

    @mute_logger(CONTROLLER_LOGGER)
    def test_inbound_unknown_key(self):
        body = self._mime(subject="Cloudflare inbound lost")
        self.assertJson(
            self._post(body, key="no-such-key"), 404, error="unknown webhook key"
        )
        self.assertJson(
            self._post(body, key=self.server.cloudflare_webhook_key[:-1]), 404
        )
        self.assertFalse(self._partner("Cloudflare inbound lost"))

    @mute_logger(CONTROLLER_LOGGER)
    def test_inbound_server_not_confirmed(self):
        draft = self.env["fetchmail.server"].create(
            {
                "name": "Cloudflare Worker (draft)",
                "server_type": "cloudflare",
                "object_id": self.partner_model.id,
            }
        )
        body = self._mime(subject="Cloudflare inbound draft")
        self.assertJson(self._post(body, server=draft), 404)
        draft.button_confirm_login()
        self.assertJson(self._post(body, server=draft), 200)
        draft.set_draft()
        self.assertJson(self._post(body, server=draft), 404)
        draft.write({"state": "done", "active": False})
        self.assertJson(self._post(body, server=draft), 404)

    @mute_logger(CONTROLLER_LOGGER, MAIL_THREAD_LOGGER)
    def test_inbound_unroutable(self):
        body = self._mime(
            subject="Cloudflare inbound nowhere", to=f"nobody@{self.alias_domain}"
        )
        response = self._post(
            body,
            server=self.server_no_fallback,
            envelope_to=f"nobody@{self.alias_domain}",
        )
        payload = self.assertJson(response, 422)
        self.assertIn("No possible route", payload["error"])
        self.assertNotIn("Traceback", payload["error"])
        self.assertFalse(self._partner("Cloudflare inbound nowhere"))

    @mute_logger(CONTROLLER_LOGGER)
    def test_inbound_internal_error(self):
        """Anything unexpected is a 500 (the Worker retries) with nothing of
        the failed attempt left behind and nothing of the error exposed."""

        def explode(server, body, envelope_from, envelope_to):
            server.env["res.partner"].create({"name": "Cloudflare inbound broken"})
            raise RuntimeError("secret detail")

        body = self._mime(subject="Cloudflare inbound broken")
        with patch.object(FetchmailServer, "_cloudflare_process_inbound", explode):
            response = self._post(body)
        payload = self.assertJson(response, 500, error="internal error")
        self.assertNotIn("secret detail", response.text)
        self.assertEqual(set(payload), {"ok", "error"})
        self.assertFalse(self._partner("Cloudflare inbound broken"), "rolled back")
        # the same message goes through once the fault is gone
        self.assertJson(self._post(body, attempt=2), 200)
        self.assertEqual(len(self._partner("Cloudflare inbound broken")), 1)

    def test_inbound_get_not_allowed(self):
        response = self.url_open(
            CF_INBOUND_ROUTE.format(key=self.server.cloudflare_webhook_key)
        )
        self.assertEqual(response.status_code, 405)
