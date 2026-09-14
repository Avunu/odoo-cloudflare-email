# Copyright 2026 Avunu LLC (avu.nu)
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl.html).
"""``ir.mail_server`` extension: the "Cloudflare Email Sending" authentication.

``connect()`` hands core a ``CloudflareSendingSession`` (see
``cloudflare_api``) instead of an SMTP connection; everything else in the
outbound path - ``build_email``, ``_prepare_email_message``, ``send_email``,
``mail.mail._send`` - stays untouched.
"""
