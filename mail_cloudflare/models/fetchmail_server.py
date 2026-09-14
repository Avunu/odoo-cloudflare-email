# Copyright 2026 Avunu LLC (avu.nu)
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl.html).
"""``fetchmail.server`` extension: the "Cloudflare Email Worker" server type.

Nothing is polled: the Email Worker pushes every inbound message to the
``/mail_cloudflare/inbound/<key>`` controller, signed with the server's
webhook secret, and the record only carries the key, the secret and the usual
``object_id``/``attach``/``original`` routing options for ``message_process``.
"""
