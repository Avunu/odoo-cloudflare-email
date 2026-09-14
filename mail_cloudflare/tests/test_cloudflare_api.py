# Copyright 2026 Avunu LLC (avu.nu)
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl.html).
"""``cloudflare_api`` on its own: MIME -> JSON mapping and the HTTP session.

Messages are built the way production builds them - core ``build_email`` then
``_prepare_email_message`` with a ``CloudflareSendingSession`` as the session
object - so the mapping is tested against what core really hands the session,
not against hand-made messages.
"""

import base64
import json

import requests

from odoo.tests.common import tagged

from odoo.addons.mail_cloudflare import cloudflare_api
from odoo.addons.mail_cloudflare.cloudflare_api import (
    MAX_HEADER_VALUE_BYTES,
    MAX_RECIPIENTS,
    REQUEST_TIMEOUT,
    SEND_URL,
    VERIFY_URL,
    CloudflareEmailError,
    CloudflareSendingSession,
    build_payload,
    verify_token,
)
from odoo.addons.mail_cloudflare.tests.common import CloudflareCommon

INNER_EML = (
    b"From: Forwarded <fwd@example.com>\r\n"
    b"To: someone@example.com\r\n"
    b"Subject: Inner subject\r\n"
    b"Message-ID: <inner@example.com>\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"\r\n"
    b"Inner body\r\n"
)


@tagged("post_install", "-at_install")
class TestCloudflareApi(CloudflareCommon):
    # -- helpers ---------------------------------------------------------------

    def _session(self, **values):
        values.setdefault("from_filter", "cf.example.com")
        values.setdefault("smtp_from", None)
        values.setdefault("mail_server_name", "Cloudflare Email Sending")
        return CloudflareSendingSession(self.CF_ACCOUNT_ID, self.CF_API_TOKEN, **values)

    def _build(self, context=None, **values):
        """``build_email`` + ``_prepare_email_message`` -> (message, smtp_from,
        smtp_to_list), exactly what ``send_email`` gives ``send_message``."""
        IrMailServer = self.env["ir.mail_server"].with_context(**(context or {}))
        values.setdefault("email_from", "Sender <sender@cf.example.com>")
        values.setdefault("email_to", ["Alice <alice@example.com>"])
        values.setdefault("subject", "Subject")
        values.setdefault("body", "<p>Hello <b>world</b></p>")
        values.setdefault("subtype", "html")
        message = IrMailServer.build_email(**values)
        smtp_from, smtp_to_list, message = IrMailServer._prepare_email_message(
            message, self._session()
        )
        return message, smtp_from, smtp_to_list

    def _payload(self, context=None, **values):
        message, _smtp_from, smtp_to_list = self._build(context=context, **values)
        return build_payload(message, smtp_to_list)

    def _addresses(self, payload, field):
        return [entry["address"] for entry in payload.get(field, [])]

    # -- payload mapping -------------------------------------------------------

    def test_payload_text_and_html(self):
        message, _smtp_from, smtp_to_list = self._build()
        payload = build_payload(message, smtp_to_list)
        self.assertEqual(
            payload["from"], {"address": "sender@cf.example.com", "name": "Sender"}
        )
        self.assertEqual(
            payload["to"], [{"address": "alice@example.com", "name": "Alice"}]
        )
        self.assertEqual(payload["subject"], "Subject")
        # build_email sets Reply-To to the sender when none is given
        self.assertEqual(payload["reply_to"], payload["from"])
        self.assertEqual(payload["html"].strip(), "<p>Hello <b>world</b></p>")
        self.assertEqual(payload["text"].strip(), "Hello *world*")
        self.assertEqual(payload["headers"], {"References": str(message["Message-Id"])})
        for absent in ("cc", "bcc", "attachments"):
            self.assertNotIn(absent, payload)
        # the body is JSON-serialisable as is
        json.dumps(payload)

    def test_payload_plain_only(self):
        payload = self._payload(body="Just text", subtype="plain")
        self.assertEqual(payload["text"].strip(), "Just text")
        self.assertNotIn("html", payload)

    def test_payload_body_alternative(self):
        payload = self._payload(
            body="<p>Rich</p>",
            body_alternative="Plain version",
            subtype_alternative="plain",
        )
        self.assertEqual(payload["html"].strip(), "<p>Rich</p>")
        self.assertEqual(payload["text"].strip(), "Plain version")

    def test_payload_reply_to(self):
        payload = self._payload(reply_to='"Support" <support@cf.example.com>')
        self.assertEqual(
            payload["reply_to"],
            {"address": "support@cf.example.com", "name": "Support"},
        )

    def test_payload_cc_bcc_split(self):
        payload = self._payload(
            email_to=["Alice <alice@example.com>"],
            email_cc=["Bob <bob@example.com>"],
            email_bcc=["carol@example.com"],
        )
        self.assertEqual(
            payload["to"], [{"address": "alice@example.com", "name": "Alice"}]
        )
        self.assertEqual(payload["cc"], [{"address": "bob@example.com", "name": "Bob"}])
        # core deleted the Bcc header; the address survives only in the envelope
        self.assertEqual(payload["bcc"], [{"address": "carol@example.com"}])

    def test_payload_recipients_deduplicated(self):
        payload = self._payload(
            email_to=["Alice <alice@example.com>", "ALICE@EXAMPLE.COM"],
            email_cc=["alice@example.com"],
        )
        self.assertEqual(self._addresses(payload, "to"), ["alice@example.com"])
        self.assertNotIn("cc", payload)
        self.assertNotIn("bcc", payload)

    def test_payload_send_validated_to(self):
        # mail.mail passes the normalized addresses it validated; anything the
        # header parser finds beyond that (e.g. a "Bike@Home" display name) or
        # that was filtered out must not reach Cloudflare
        payload = self._payload(
            context={"send_validated_to": ["alice@example.com"]},
            email_to=['"Bike@Home" <alice@example.com>', "Bob <bob@example.com>"],
        )
        self.assertEqual(self._addresses(payload, "to"), ["alice@example.com"])
        self.assertNotIn("cc", payload)
        self.assertNotIn("bcc", payload)

    def test_payload_attachment(self):
        payload = self._payload(
            attachments=[("report.pdf", b"%PDF-1.4 fake", "application/pdf")]
        )
        self.assertEqual(
            payload["attachments"],
            [
                {
                    "content": base64.b64encode(b"%PDF-1.4 fake").decode(),
                    "filename": "report.pdf",
                    "type": "application/pdf",
                    "disposition": "attachment",
                }
            ],
        )
        # the bodies are untouched by the attachment
        self.assertIn("<p>Hello", payload["html"])
        self.assertIn("Hello", payload["text"])

    def test_payload_attachment_without_mimetype(self):
        payload = self._payload(attachments=[("blob.bin", b"\x00\x01", False)])
        self.assertEqual(payload["attachments"][0]["type"], "application/octet-stream")

    def test_payload_inline_cid(self):
        message, _smtp_from, smtp_to_list = self._build()
        message.add_attachment(
            b"PNG1",
            "image",
            "png",
            filename="logo.png",
            cid="<logo@odoo>",
            disposition="inline",
        )
        # a Content-ID alone (disposition still "attachment") is enough: the
        # HTML references it through cid:
        message.add_attachment(
            b"PNG2", "image", "png", filename="chart.png", cid="<chart@odoo>"
        )
        payload = build_payload(message, smtp_to_list)
        self.assertEqual(
            [
                (a["filename"], a["disposition"], a["content_id"])
                for a in payload["attachments"]
            ],
            [
                ("logo.png", "inline", "logo@odoo"),
                ("chart.png", "inline", "chart@odoo"),
            ],
        )
        self.assertEqual(payload["attachments"][0]["type"], "image/png")

    def test_payload_rfc822_attachment(self):
        payload = self._payload(
            attachments=[("forwarded.eml", INNER_EML, "message/rfc822")]
        )
        # one attachment, not one per part of the attached message
        self.assertEqual(len(payload["attachments"]), 1)
        attachment = payload["attachments"][0]
        self.assertEqual(attachment["filename"], "forwarded.eml")
        self.assertEqual(attachment["type"], "message/rfc822")
        self.assertEqual(attachment["disposition"], "attachment")
        inner = base64.b64decode(attachment["content"])
        self.assertIn(b"Subject: Inner subject", inner)
        self.assertIn(b"Inner body", inner)
        # the outer bodies are still the outer bodies
        self.assertIn("<p>Hello", payload["html"])
        self.assertNotIn("Inner body", payload["text"])

    def test_payload_header_allowlist(self):
        message, _smtp_from, smtp_to_list = self._build(
            headers={
                "X-Odoo-Objects": "res.partner-1",
                "X-Auto-Response-Suppress": "OOF",
                "Precedence": "list",
                "List-Unsubscribe": "<https://cf.example.com/unsubscribe>",
                "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
                "Auto-Submitted": "auto-generated",
                "Return-Path": "bounce@cf.example.com",
                "X-Forge-To": "Hidden <hidden@example.com>",
            }
        )
        payload = build_payload(message, smtp_to_list)
        self.assertEqual(
            payload["headers"],
            {
                "X-Odoo-Objects": "res.partner-1",
                "X-Auto-Response-Suppress": "OOF",
                "Precedence": "list",
                "List-Unsubscribe": "<https://cf.example.com/unsubscribe>",
                "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
                "Auto-Submitted": "auto-generated",
                "References": str(message["Message-Id"]),
            },
        )
        # reserved, first-class and structural headers never travel in
        # ``headers``: they are either Cloudflare's or request fields
        for name in message:
            if name.lower() in (
                "date",
                "return-path",
                "content-type",
                "mime-version",
                "message-id",
                "from",
                "to",
                "subject",
                "reply-to",
            ):
                self.assertNotIn(name, payload["headers"])
        # X-Forge-To was consumed by _prepare_email_message: the To header now
        # names the forged address, which is not a recipient; the lone
        # envelope recipient is promoted to ``to`` (without a display name)
        self.assertEqual(payload["to"], [{"address": "alice@example.com"}])
        self.assertNotIn("bcc", payload)

    def test_payload_bcc_only_stays_hidden(self):
        message, _smtp_from, smtp_to_list = self._build(
            email_to=["Alice <alice@example.com>"],
            email_bcc=["bob@example.com", "carol@example.com"],
            headers={"X-Forge-To": "Everyone <everyone@example.com>"},
        )
        payload = build_payload(message, smtp_to_list)
        # several hidden recipients are never promoted (they would see each
        # other); Cloudflare is left to judge the empty ``to``
        self.assertEqual(payload["to"], [])
        self.assertEqual(
            self._addresses(payload, "bcc"),
            ["alice@example.com", "bob@example.com", "carol@example.com"],
        )

    def test_payload_repeated_header_joined(self):
        message, _smtp_from, smtp_to_list = self._build()
        message["X-Tag"] = "one"
        message["X-Tag"] = "two"
        self.assertEqual(
            build_payload(message, smtp_to_list)["headers"]["X-Tag"], "one, two"
        )

    def test_payload_references_absent(self):
        message, _smtp_from, smtp_to_list = self._build()
        self.assertFalse(message["References"])
        payload = build_payload(message, smtp_to_list)
        self.assertEqual(payload["headers"]["References"], str(message["Message-Id"]))

    def test_payload_references_appended(self):
        message, _smtp_from, smtp_to_list = self._build(
            references="<parent@example.com> <grandparent@example.com>"
        )
        payload = build_payload(message, smtp_to_list)
        self.assertEqual(
            payload["headers"]["References"],
            f"<parent@example.com> <grandparent@example.com> {message['Message-Id']}",
        )

    def test_payload_references_present(self):
        # notification mails already end with their own id
        # (_notify_by_email_get_base_mail_values): no duplicate
        message, _smtp_from, smtp_to_list = self._build(
            message_id="<own@example.com>",
            references="<parent@example.com> <own@example.com>",
        )
        payload = build_payload(message, smtp_to_list)
        self.assertEqual(
            payload["headers"]["References"], "<parent@example.com> <own@example.com>"
        )

    def test_payload_references_folded_and_comma_separated(self):
        message, _smtp_from, smtp_to_list = self._build(
            message_id="<own@example.com>",
            references="<a@example.com>,<b@example.com>, \t<c@example.com>",
        )
        payload = build_payload(message, smtp_to_list)
        self.assertEqual(
            payload["headers"]["References"],
            "<a@example.com> <b@example.com> <c@example.com> <own@example.com>",
        )

    def test_payload_references_trimmed_oldest_first(self):
        old_ids = [f"<ancestor-{index:04d}@example.com>" for index in range(300)]
        message, _smtp_from, smtp_to_list = self._build(
            message_id="<own@example.com>", references=" ".join(old_ids)
        )
        references = build_payload(message, smtp_to_list)["headers"]["References"]
        kept = references.split(" ")
        self.assertLessEqual(len(references.encode()), MAX_HEADER_VALUE_BYTES)
        self.assertEqual(kept[-1], "<own@example.com>")
        # the newest ancestors survive, the oldest are dropped
        self.assertEqual(kept[-2], old_ids[-1])
        self.assertNotIn(old_ids[0], kept)

    def test_payload_headers_too_large_raises(self):
        message, _smtp_from, smtp_to_list = self._build(
            headers={"X-Blob": "x" * (MAX_HEADER_VALUE_BYTES + 1)}
        )
        with self.assertRaises(CloudflareEmailError):
            build_payload(message, smtp_to_list)

    def test_payload_too_many_recipients(self):
        recipients = [f"r{index}@example.com" for index in range(MAX_RECIPIENTS + 1)]
        message, _smtp_from, smtp_to_list = self._build(email_to=recipients)
        with self.assertRaises(CloudflareEmailError):
            build_payload(message, smtp_to_list)
        # exactly the limit is fine
        message, _smtp_from, smtp_to_list = self._build(email_to=recipients[:-1])
        self.assertEqual(
            len(build_payload(message, smtp_to_list)["to"]), MAX_RECIPIENTS
        )

    def test_payload_no_recipient(self):
        message, _smtp_from, _smtp_to_list = self._build()
        with self.assertRaises(CloudflareEmailError):
            build_payload(message, [])

    def test_payload_empty_body(self):
        message, _smtp_from, smtp_to_list = self._build(body="", subtype="plain")
        with self.assertRaises(CloudflareEmailError):
            build_payload(message, smtp_to_list)

    # -- session: what core sees ----------------------------------------------

    def test_session_duck_types_smtp_connection(self):
        session = self._session(smtp_from="bounce@cf.example.com")
        self.assertEqual(session.from_filter, "cf.example.com")
        self.assertEqual(session.smtp_from, "bounce@cf.example.com")
        self.assertEqual(session.mail_server_name, "Cloudflare Email Sending")
        # quit()/close() never raise, even twice
        session.quit()
        session.close()

    def test_session_send_email_through_core(self):
        message, _smtp_from, _smtp_to_list = self._build(
            email_cc=["Bob <bob@example.com>"]
        )
        session = self._session()
        with self.mock_cloudflare():
            returned = self.env["ir.mail_server"].send_email(
                message, smtp_session=session
            )
        # core keeps returning Odoo's Message-Id, whatever Cloudflare answered
        self.assertEqual(returned, message["Message-Id"])
        self.assertEqual(len(self.cf_requests), 1)
        request = self.cf_requests[0]
        self.assertEqual(request["method"], "POST")
        self.assertEqual(request["url"], SEND_URL.format(account_id=self.CF_ACCOUNT_ID))
        self.assertEqual(
            request["headers"]["Authorization"], f"Bearer {self.CF_API_TOKEN}"
        )
        self.assertEqual(request["headers"]["Content-Type"], "application/json")
        self.assertEqual(request["timeout"], REQUEST_TIMEOUT)
        self.assertEqual(self._addresses(request["json"], "to"), ["alice@example.com"])
        self.assertEqual(self._addresses(request["json"], "cc"), ["bob@example.com"])
        self.assertEqual(self.cf_sleeps, [])

    def test_session_returns_cloudflare_message_id(self):
        message, smtp_from, smtp_to_list = self._build()
        with self.mock_cloudflare(
            [self._cf_success(["alice@example.com"], message_id="cf-42")]
        ):
            self.assertEqual(
                self._session().send_message(message, smtp_from, smtp_to_list), "cf-42"
            )

    def test_session_too_large_before_http(self):
        message, smtp_from, smtp_to_list = self._build(
            attachments=[
                ("big.bin", b"\x00" * (4 * 1024 * 1024), "application/octet-stream")
            ]
        )
        with self.mock_cloudflare(), self.assertRaises(CloudflareEmailError) as raised:
            self._session().send_message(message, smtp_from, smtp_to_list)
        self.assertIn("MiB", str(raised.exception))
        self.assertEqual(self.cf_requests, [])

    # -- session: retry matrix -------------------------------------------------

    def test_session_400_no_retry(self):
        message, smtp_from, smtp_to_list = self._build()
        responses = [
            self._cf_error(400, 10001, "email.sending.error.invalid_request_schema")
        ]
        with (
            self.mock_cloudflare(responses),
            self.assertRaises(CloudflareEmailError) as raised,
        ):
            self._session().send_message(message, smtp_from, smtp_to_list)
        self.assertEqual(raised.exception.status, 400)
        self.assertEqual(raised.exception.code, 10001)
        self.assertFalse(raised.exception.fatal)
        self.assertIn("invalid_request_schema", str(raised.exception))
        self.assertEqual(len(self.cf_requests), 1)
        self.assertEqual(self.cf_sleeps, [])

    def test_session_401_fatal_fails_fast(self):
        message, smtp_from, smtp_to_list = self._build()
        session = self._session()
        responses = [
            self._cf_error(
                401, 10101, "email.sending.error.authentication.unauthorized"
            )
        ]
        with self.mock_cloudflare(responses):
            with self.assertRaises(CloudflareEmailError) as raised:
                session.send_message(message, smtp_from, smtp_to_list)
            self.assertTrue(raised.exception.fatal)
            self.assertEqual(raised.exception.status, 401)
            # the rest of the batch does not even hit the network
            with self.assertRaises(CloudflareEmailError) as raised:
                session.send_message(message, smtp_from, smtp_to_list)
            self.assertTrue(raised.exception.fatal)
            self.assertIn("Not retried", str(raised.exception))
        self.assertEqual(len(self.cf_requests), 1)

    def test_session_403_fatal(self):
        message, smtp_from, smtp_to_list = self._build()
        responses = [
            self._cf_error(403, 10102, "email.sending.error.authentication.forbidden")
        ]
        with (
            self.mock_cloudflare(responses),
            self.assertRaises(CloudflareEmailError) as raised,
        ):
            self._session().send_message(message, smtp_from, smtp_to_list)
        self.assertTrue(raised.exception.fatal)
        self.assertEqual(len(self.cf_requests), 1)

    def test_session_429_honours_retry_after(self):
        message, smtp_from, smtp_to_list = self._build()
        throttled = self._cf_error(
            429, 10004, "email.sending.error.throttled", {"Retry-After": "3"}
        )
        with self.mock_cloudflare([throttled]):
            self._session().send_message(message, smtp_from, smtp_to_list)
        self.assertEqual(len(self.cf_requests), 2)
        self.assertEqual(self.cf_sleeps, [3])

    def test_session_429_default_and_capped_backoff(self):
        message, smtp_from, smtp_to_list = self._build()
        no_header = self._cf_error(429, 10004, "email.sending.error.throttled")
        huge = self._cf_error(
            429, 10004, "email.sending.error.throttled", {"Retry-After": "600"}
        )
        with self.mock_cloudflare([no_header, huge]):
            self._session().send_message(message, smtp_from, smtp_to_list)
        self.assertEqual(len(self.cf_requests), 3)
        self.assertEqual(self.cf_sleeps, [2, cloudflare_api.MAX_RETRY_AFTER])

    def test_session_5xx_exhausted(self):
        message, smtp_from, smtp_to_list = self._build()
        outage = self._cf_error(500, 10002, "email.sending.error.internal_server")
        responses = [outage] * cloudflare_api.MAX_ATTEMPTS
        with (
            self.mock_cloudflare(responses),
            self.assertRaises(CloudflareEmailError) as raised,
        ):
            self._session().send_message(message, smtp_from, smtp_to_list)
        self.assertEqual(raised.exception.status, 500)
        self.assertIn("Giving up", str(raised.exception))
        self.assertEqual(len(self.cf_requests), cloudflare_api.MAX_ATTEMPTS)
        self.assertEqual(self.cf_sleeps, [1, 2])

    def test_session_5xx_then_success(self):
        message, smtp_from, smtp_to_list = self._build()
        outage = self._cf_error(
            503, 10100, "email.sending.error.authentication.upstream"
        )
        with self.mock_cloudflare([outage]):
            self._session().send_message(message, smtp_from, smtp_to_list)
        self.assertEqual(len(self.cf_requests), 2)
        self.assertEqual(self.cf_sleeps, [1])

    def test_session_network_error_then_success(self):
        message, smtp_from, smtp_to_list = self._build()
        with self.mock_cloudflare([requests.ConnectionError("connection reset")]):
            self._session().send_message(message, smtp_from, smtp_to_list)
        self.assertEqual(len(self.cf_requests), 2)
        self.assertEqual(self.cf_sleeps, [1])

    def test_session_network_error_exhausted(self):
        message, smtp_from, smtp_to_list = self._build()
        responses = [requests.Timeout("read timed out")] * cloudflare_api.MAX_ATTEMPTS
        with (
            self.mock_cloudflare(responses),
            self.assertRaises(CloudflareEmailError) as raised,
        ):
            self._session().send_message(message, smtp_from, smtp_to_list)
        self.assertIn("read timed out", str(raised.exception))
        self.assertIsNone(raised.exception.status)
        self.assertEqual(len(self.cf_requests), cloudflare_api.MAX_ATTEMPTS)

    def test_session_success_false(self):
        message, smtp_from, smtp_to_list = self._build()
        body = {
            "success": False,
            "errors": [
                {"code": 10001, "message": "email.sending.error.invalid_request_schema"}
            ],
            "messages": [],
            "result": None,
        }
        with (
            self.mock_cloudflare([(200, body)]),
            self.assertRaises(CloudflareEmailError) as raised,
        ):
            self._session().send_message(message, smtp_from, smtp_to_list)
        self.assertEqual(raised.exception.code, 10001)
        self.assertEqual(len(self.cf_requests), 1)

    def test_session_non_json_200(self):
        message, smtp_from, smtp_to_list = self._build()
        with (
            self.mock_cloudflare([(200, "<html>proxy</html>")]),
            self.assertRaises(CloudflareEmailError),
        ):
            self._session().send_message(message, smtp_from, smtp_to_list)
        self.assertEqual(len(self.cf_requests), 1)

    # -- session: per-recipient results ---------------------------------------

    def test_session_all_bounced_raises(self):
        message, smtp_from, smtp_to_list = self._build(email_cc=["bob@example.com"])
        responses = [
            self._cf_success(
                permanent_bounces=["alice@example.com"],
                suppressed_recipients=["Bob@example.com"],
            )
        ]
        with (
            self.mock_cloudflare(responses),
            self.assertRaises(CloudflareEmailError) as raised,
        ):
            self._session().send_message(message, smtp_from, smtp_to_list)
        self.assertIn("alice@example.com", str(raised.exception))
        self.assertIn("bob@example.com", str(raised.exception))

    def test_session_nobody_reached_raises(self):
        message, smtp_from, smtp_to_list = self._build()
        # Cloudflare reports the bounce under another spelling: nothing was
        # delivered or queued, that alone is a failure
        responses = [self._cf_success(permanent_bounces=["alice@other.example"])]
        with self.mock_cloudflare(responses), self.assertRaises(CloudflareEmailError):
            self._session().send_message(message, smtp_from, smtp_to_list)

    def test_session_partial_bounce_warns(self):
        message, smtp_from, smtp_to_list = self._build(email_cc=["bob@example.com"])
        responses = [
            self._cf_success(
                delivered=["alice@example.com"],
                permanent_bounces=["bob@example.com"],
                message_id="cf-partial",
            )
        ]
        with (
            self.mock_cloudflare(responses),
            self.assertLogs(cloudflare_api._logger, level="WARNING") as logs,
        ):
            result = self._session().send_message(message, smtp_from, smtp_to_list)
        self.assertEqual(result, "cf-partial")
        self.assertTrue(any("bob@example.com" in line for line in logs.output))

    # -- verify_token ----------------------------------------------------------

    def test_verify_token_active(self):
        with self.mock_cloudflare():
            result = verify_token("secret-token")
        self.assertEqual(result["status"], "active")
        self.assertEqual(len(self.cf_requests), 1)
        request = self.cf_requests[0]
        self.assertEqual((request["method"], request["url"]), ("GET", VERIFY_URL))
        self.assertEqual(request["headers"]["Authorization"], "Bearer secret-token")

    def test_verify_token_rejected(self):
        responses = [self._cf_error(401, 1000, "Invalid API Token")]
        with (
            self.mock_cloudflare(responses),
            self.assertRaises(CloudflareEmailError) as raised,
        ):
            verify_token("bad-token")
        self.assertTrue(raised.exception.fatal)
        self.assertEqual(raised.exception.code, 1000)

    def test_verify_token_inactive(self):
        body = {"success": True, "errors": [], "result": {"status": "expired"}}
        with (
            self.mock_cloudflare([(200, body)]),
            self.assertRaises(CloudflareEmailError) as raised,
        ):
            verify_token("old-token")
        self.assertFalse(raised.exception.fatal)
        self.assertIn("expired", str(raised.exception))

    def test_verify_token_unreachable(self):
        with (
            self.mock_cloudflare([requests.ConnectionError("dns")]),
            self.assertRaises(CloudflareEmailError),
        ):
            verify_token("token")

    # -- harness ---------------------------------------------------------------

    def test_harness_signature_vector(self):
        # the vector the worker's unit tests pin; keep both sides in sync
        self.assertEqual(
            self._cf_sign("key", 1700000000, "hello"),
            "v1=4d583a269f4f276a3fa80ff31b5a01879a848096983222a17893d198418939aa",
        )
        self.assertEqual(
            self._cf_sign(b"key", "1700000000", b"hello"),
            self._cf_sign("key", 1700000000, "hello"),
        )
