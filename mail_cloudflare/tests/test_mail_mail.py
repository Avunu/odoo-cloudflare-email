# Copyright 2026 Avunu LLC (avu.nu)
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl.html).
"""``mail.mail.send()`` end to end through a Cloudflare outgoing server.

Nothing in ``mail.mail`` is overridden, so these tests pin the integration:
batching opens one session per configuration, a fatal API error fails the
rest of the batch fast, ``message_id`` survives the send and oversized record
attachments become download links because of ``_get_max_email_size``.
"""

from unittest.mock import PropertyMock, patch

from odoo.tests import tagged
from odoo.tools import mute_logger

from odoo.addons.mail_cloudflare.tests.common import CloudflareCommon


@tagged("post_install", "-at_install")
class TestMailMail(CloudflareCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # res.partner is the one business mail.thread model ``mail`` alone
        # guarantees; its attachments count as record-owned
        cls.partner = cls.env["res.partner"].create(
            {"name": "Cloudflare Customer", "email": "customer@example.com"}
        )

    def _create_mail(self, **values):
        return self.env["mail.mail"].create(
            {
                "body_html": "<p>Hello</p>",
                "email_from": "Sender <sender@cf.example.com>",
                "email_to": "alice@example.com",
                "subject": "Subject",
                **values,
            }
        )

    def _create_record_attachments(self, count):
        return self.env["ir.attachment"].create(
            [
                {
                    "name": f"attachment{index}.bin",
                    "res_model": self.partner._name,
                    "res_id": self.partner.id,
                    # real content stays tiny, file_size is mocked
                    "datas": "IA==",
                }
                for index in range(count)
            ]
        )

    def test_send(self):
        mail = self._create_mail()
        message_id = mail.message_id
        self.assertTrue(message_id)
        with self.mock_cloudflare():
            mail.send()
        self.assertEqual(mail.state, "sent")
        self.assertFalse(mail.failure_reason)
        # core returns Odoo's Message-Id from send_email and mail.mail writes
        # it back: Cloudflare's own id never reaches mail.message
        self.assertEqual(mail.message_id, message_id)
        self.assertEqual(len(self.cf_requests), 1)
        payload = self.cf_requests[0]["json"]
        self.assertEqual(
            payload["from"], {"address": "sender@cf.example.com", "name": "Sender"}
        )
        self.assertEqual(payload["to"], [{"address": "alice@example.com"}])
        self.assertEqual(payload["subject"], "Subject")
        self.assertIn("<p>Hello</p>", payload["html"])
        self.assertIn(message_id, payload["headers"]["References"])
        self.assertEqual(self.cf_sleeps, [])

    def test_send_forced_mail_server(self):
        # test.mycompany.com is served by MailCommon's SMTP server, the forced
        # server wins anyway and the From header is sent as is
        mail = self._create_mail(
            email_from="someone@test.mycompany.com",
            mail_server_id=self.mail_server_cloudflare.id,
        )
        with self.mock_cloudflare():
            mail.send()
        self.assertEqual(mail.state, "sent")
        self.assertEqual(len(self.cf_requests), 1)
        payload = self.cf_requests[0]["json"]
        self.assertEqual(payload["from"], {"address": "someone@test.mycompany.com"})

    def test_send_recipients_one_request_each(self):
        # one mail.mail, two notified partners: core builds one message per
        # recipient and the session sends each on its own
        partners = self.env["res.partner"].create(
            [
                {"name": "Alice", "email": "alice@example.com"},
                {"name": "Bob", "email": "bob@example.com"},
            ]
        )
        mail = self._create_mail(email_to=False, recipient_ids=partners.ids)
        with self.mock_cloudflare():
            mail.send()
        self.assertEqual(mail.state, "sent")
        self.assertEqual(len(self.cf_requests), 2)
        self.assertEqual(
            sorted(request["json"]["to"][0]["address"] for request in self.cf_requests),
            ["alice@example.com", "bob@example.com"],
        )

    @mute_logger("odoo.addons.mail.models.mail_mail")
    def test_send_401_marks_exception(self):
        mail = self._create_mail()
        responses = [self._cf_error(401, 10101, "authentication.unauthorized")]
        with self.mock_cloudflare(responses):
            mail.send()
        self.assertEqual(mail.state, "exception")
        self.assertEqual(mail.failure_type, "unknown")
        self.assertIn("CloudflareEmailError", mail.failure_reason)
        self.assertIn("HTTP 401", mail.failure_reason)
        self.assertIn("authentication.unauthorized", mail.failure_reason)
        self.assertEqual(len(self.cf_requests), 1)
        self.assertEqual(self.cf_sleeps, [])

    @mute_logger("odoo.addons.mail.models.mail_mail")
    def test_send_batch_fails_fast(self):
        first = self._create_mail()
        second = self._create_mail(email_to="bob@example.com")
        mails = first | second
        responses = [self._cf_error(403, 10102, "authentication.forbidden")]
        with self.mock_cloudflare(responses):
            mails.send()
        self.assertEqual(mails.mapped("state"), ["exception", "exception"])
        self.assertIn("HTTP 403", first.failure_reason)
        # same batch (same server, same sender), same session: the second
        # mail is failed without another request
        self.assertIn("Not retried", second.failure_reason)
        self.assertEqual(len(self.cf_requests), 1)

    @mute_logger("odoo.addons.mail.models.mail_mail")
    def test_send_400_no_retry(self):
        mail = self._create_mail()
        responses = [self._cf_error(400, 10001, "invalid_request_schema")]
        with self.mock_cloudflare(responses):
            mail.send()
        self.assertEqual(mail.state, "exception")
        self.assertIn("invalid_request_schema", mail.failure_reason)
        self.assertEqual(len(self.cf_requests), 1)
        self.assertEqual(self.cf_sleeps, [])

    @patch(
        "odoo.addons.base.models.ir_attachment.IrAttachment.file_size",
        new_callable=PropertyMock,
    )
    def test_send_large_record_attachment_becomes_link(self, mock_file_size):
        """Above the server's max_email_size (5 MiB unless set) record-owned
        attachments are replaced by download links, so the message stays
        under Cloudflare's limit; composer-owned ones are always embedded."""
        mock_file_size.return_value = 6 * 1024 * 1024
        attachments = self._create_record_attachments(1)
        mail = self._create_mail(attachment_ids=[(6, 0, attachments.ids)])
        with self.mock_cloudflare():
            mail.send()
        self.assertEqual(mail.state, "sent")
        payload = self.cf_requests[0]["json"]
        self.assertNotIn("attachments", payload)
        self.assertIn(f"/web/content/{attachments.id}", payload["html"])
        self.assertIn("access_token=", payload["html"])

        # an explicit max_email_size on the server is what counts
        self.mail_server_cloudflare.max_email_size = 10
        mail = self._create_mail(attachment_ids=[(6, 0, attachments.ids)])
        with self.mock_cloudflare():
            mail.send()
        self.assertEqual(mail.state, "sent")
        payload = self.cf_requests[0]["json"]
        self.assertEqual(len(payload["attachments"]), 1)
        self.assertEqual(payload["attachments"][0]["filename"], "attachment0.bin")
        self.assertNotIn("/web/content/", payload["html"])
