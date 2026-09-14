# Copyright 2026 Avunu LLC (avu.nu)
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl.html).
"""Inbound webhook: ``POST /mail_cloudflare/inbound/<key>``.

The Email Worker posts the raw RFC 5322 message (``Content-Type:
message/rfc822``) with ``X-Mail-Cloudflare-Timestamp`` / ``-Signature``
(``v1=<hex HMAC-SHA256 over "<timestamp>.<body>">``) and the envelope
addresses; the controller looks the ``fetchmail.server`` up by key, verifies
the signature and routes the message through ``message_process``.
"""
