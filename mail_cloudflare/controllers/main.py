# Copyright 2026 Avunu LLC (avu.nu)
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl.html).
"""Inbound webhook: ``POST /mail_cloudflare/inbound/<key>``.

The Email Worker posts the raw RFC 5322 message (``Content-Type:
message/rfc822``) with ``X-Mail-Cloudflare-Timestamp`` / ``-Signature``
(``v1=<hex HMAC-SHA256 over "<timestamp>.<body>">``) and the envelope
addresses; the controller looks the ``fetchmail.server`` up by key, verifies
the signature and routes the message through ``message_process``.

Status codes are the contract with the Worker's retry queue: 2xx is
delivered, 401/404/422 are permanent (``rejected``, no retry), anything else
is retried with back-off. Error bodies carry a one-line reason at most, never
a traceback.
"""

import logging

from odoo import SUPERUSER_ID
from odoo.http import Controller, request, route
from odoo.service.model import PG_CONCURRENCY_EXCEPTIONS_TO_RETRY

from odoo.addons.web.controllers.utils import ensure_db

from ..models.fetchmail_server import INBOUND_PATH

_logger = logging.getLogger(__name__)

HEADER_ID = "X-Mail-Cloudflare-Id"
HEADER_TIMESTAMP = "X-Mail-Cloudflare-Timestamp"
HEADER_SIGNATURE = "X-Mail-Cloudflare-Signature"
HEADER_ENVELOPE_FROM = "X-Mail-Cloudflare-Envelope-From"
HEADER_ENVELOPE_TO = "X-Mail-Cloudflare-Envelope-To"
HEADER_ATTEMPT = "X-Mail-Cloudflare-Attempt"


class MailCloudflareController(Controller):
    # ``auth="none"``: the Worker has no session, the HMAC is the credential.
    # ``readonly=False`` is mandatory: ``auth="none"`` routes default to a
    # read-only cursor (``http.py``, ``_check_and_complete_route_definition``)
    # and the ``ro->rw`` retry would run the handler twice. ``csrf=False``
    # because there is no form token, ``save_session=False`` so a session is
    # not created on disk for every delivery.
    @route(
        f"{INBOUND_PATH}/<string:key>",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
        readonly=False,
        save_session=False,
    )
    def inbound(self, key, **kw):
        # Multi-database hosts need dbfilter/db_name: a ``?db=`` redirect
        # would lose the POST body.
        ensure_db()
        # An ``auth="none"`` environment has no user; the gateway runs as the
        # superuser exactly like the fetchmail cron does.
        request.update_env(user=SUPERUSER_ID)
        headers = request.httprequest.headers
        worker_id = headers.get(HEADER_ID) or False
        server = request.env["fetchmail.server"]._cloudflare_find_by_key(key)
        if not server:
            _logger.warning(
                "Cloudflare inbound %s: no confirmed server for key %s...",
                worker_id,
                key[:8],
            )
            return self._respond(404, error="unknown webhook key")
        body = request.httprequest.get_data()
        if not server._cloudflare_verify_signature(
            headers.get(HEADER_TIMESTAMP), headers.get(HEADER_SIGNATURE), body
        ):
            # No detail on purpose: an attacker probing the endpoint learns
            # nothing about which check failed.
            _logger.warning(
                "Cloudflare inbound %s: signature verification failed on %s",
                worker_id,
                server.name,
            )
            return self._respond(401, error="invalid signature")
        try:
            thread_id = server._cloudflare_process_inbound(
                body,
                envelope_from=headers.get(HEADER_ENVELOPE_FROM),
                envelope_to=headers.get(HEADER_ENVELOPE_TO),
            )
        except ValueError as error:
            # ``message_route`` found no alias and there is no fallback model
            # (or the model refuses the message): permanent, the Worker must
            # not retry. The sender-safe message is the one core built.
            request.env.cr.rollback()
            _logger.info(
                "Cloudflare inbound %s rejected on %s: %s",
                worker_id,
                server.name,
                error,
            )
            return self._respond(422, error=str(error))
        except PG_CONCURRENCY_EXCEPTIONS_TO_RETRY:
            # Let ``service.model.retrying`` rerun the request on a fresh
            # transaction, as it does for every other controller.
            raise
        except Exception:  # anything else is a 500, the Worker retries
            request.env.cr.rollback()
            _logger.exception(
                "Cloudflare inbound %s failed on %s", worker_id, server.name
            )
            return self._respond(500, error="internal error")
        return self._respond(200, thread_id=thread_id or False, id=worker_id)

    @staticmethod
    def _respond(status, error=None, **values):
        payload = {"ok": status == 200, **values}
        if error:
            payload["error"] = error
        return request.make_json_response(payload, status=status)
