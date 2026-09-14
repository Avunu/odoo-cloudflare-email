# Copyright 2026 Avunu LLC (avu.nu)
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl.html).
"""Shared harness for the mail_cloudflare test suites.

``MockCloudflareCase`` is to the Cloudflare REST API what core's
``MockSmtplibCase`` (``odoo/addons/base/tests/common.py``) is to smtplib: a
context manager that keeps every request away from the network, records what
would have been sent and serves scripted responses. ``CloudflareCommon`` is
the ``MailCommon`` flavour with a Cloudflare ``ir.mail_server`` next to the
SMTP servers ``MailCommon`` creates. Later suites build on this module and do
not edit it, so its public surface is the contract:

- ``mock_cloudflare(responses=None)``, ``cf_requests``, ``cf_sleeps``;
- response builders ``_cf_success`` / ``_cf_error``, ``_cf_recipients``;
- inbound helpers ``_cf_sign`` / ``_cf_post`` and the ``CF_HEADER_*`` names;
- ``MIME_TEMPLATE`` for ``MockEmail.format()`` / ``format_and_process()``;
- ``CloudflareCommon.mail_server_cloudflare`` and
  ``_create_cloudflare_mail_server()``.
"""

import hashlib
import hmac
import json
import time
from contextlib import contextmanager
from unittest.mock import patch

import requests
from requests.structures import CaseInsensitiveDict

from odoo import modules

from odoo.addons.mail.tests.common import MailCommon
from odoo.addons.mail_cloudflare import cloudflare_api

# Wire contract between the Email Worker and the inbound controller. The
# signature covers ``"<timestamp>." + body`` (HMAC-SHA256, hex) and is sent as
# ``v1=<hex>``; the same vector is pinned in the worker's unit tests.
CF_INBOUND_ROUTE = "/mail_cloudflare/inbound/{key}"
CF_HEADER_ID = "X-Mail-Cloudflare-Id"
CF_HEADER_TIMESTAMP = "X-Mail-Cloudflare-Timestamp"
CF_HEADER_SIGNATURE = "X-Mail-Cloudflare-Signature"
CF_HEADER_ENVELOPE_FROM = "X-Mail-Cloudflare-Envelope-From"
CF_HEADER_ENVELOPE_TO = "X-Mail-Cloudflare-Envelope-To"
CF_HEADER_ATTEMPT = "X-Mail-Cloudflare-Attempt"

# A raw inbound message for ``MockEmail.format()`` (same placeholders as
# test_mail's MAIL_TEMPLATE, inlined so the module does not depend on
# test_mail). Keep the body free of braces: it goes through ``str.format``.
MIME_TEMPLATE = """Return-Path: {return_path}
To: {to}
cc: {cc}
Received: by mx.cloudflare.net (Cloudflare Email Routing)
    id 5DF9ABFB2A; Fri, 10 Aug 2012 16:16:39 +0200 (CEST)
From: {email_from}
Subject: {subject}
MIME-Version: 1.0
Content-Type: multipart/alternative;
    boundary="----=_Part_4200734_24778174.1344608186754"
Date: Fri, 10 Aug 2012 14:16:26 +0000
Message-ID: {msg_id}
{extra}
------=_Part_4200734_24778174.1344608186754
Content-Type: text/plain; charset=utf-8
Content-Transfer-Encoding: quoted-printable

Please call me as soon as possible this afternoon!

--
Sylvie
------=_Part_4200734_24778174.1344608186754
Content-Type: text/html; charset=utf-8
Content-Transfer-Encoding: quoted-printable

<!DOCTYPE html PUBLIC "-//W3C//DTD HTML 4.01//EN" "http://www.w3.org/TR/html4/strict.dtd">
<html>
 <head>=20
  <meta http-equiv=3D"Content-Type" content=3D"text/html; charset=3Dutf-8" />
 </head>=20
 <body>=20

  <p>Please call me as soon as possible this afternoon!</p>

  <p>--<br/>
     Sylvie
  <p>
 </body>
</html>
------=_Part_4200734_24778174.1344608186754--
"""


class MockCloudflareCase:
    """Mock the Cloudflare REST API underneath ``cloudflare_api``.

    Usable with any ``TransactionCase``; for HTTP tests see ``_cf_post``.
    """

    CF_ACCOUNT_ID = "0123456789abcdef0123456789abcdef"
    CF_API_TOKEN = "cf-test-token"

    @contextmanager
    def mock_cloudflare(self, responses=None):
        """Serve ``responses`` to whatever ``cloudflare_api`` sends.

        ``responses`` is consumed in order; each entry is a ``(status, body)``
        or ``(status, body, headers)`` tuple (``body``: dict -> JSON, str or
        bytes -> raw), an exception instance (raised in place of a response,
        e.g. ``requests.ConnectionError``) or a callable taking the recorded
        request and returning one of those. Once the script is exhausted, a
        success answering *every* recipient of the request (or an active
        token for the verify endpoint) is served, so tests that do not care
        about the reply need no script.

        Every request lands in ``self.cf_requests`` as ``{method, url,
        headers, body, json, timeout, params}``; back-off sleeps are recorded
        in ``self.cf_sleeps`` instead of sleeping. Like
        ``mock_smtplib_connection`` this patches ``current_test`` to False
        (``connect()`` and ``send_email`` short-circuit in test mode), which
        is also why an ``HttpCase.url_open`` must not run inside it: the test
        cursor check compares ``current_test`` to the running test.
        """
        self.cf_requests = []
        self.cf_sleeps = []
        script = list(responses or [])
        origin = self

        def _request(session, method, url, **kwargs):
            record = origin._cf_record(session, method, url, kwargs)
            origin.cf_requests.append(record)
            item = script.pop(0) if script else origin._cf_default_response(record)
            if callable(item):
                item = item(record)
            if isinstance(item, BaseException):
                raise item
            return origin._cf_build_response(item, url)

        with (
            patch.object(requests.Session, "request", _request),
            patch.object(modules.module, "current_test", False),
            patch.object(cloudflare_api, "_sleep", self.cf_sleeps.append),
        ):
            yield

    # -- request / response plumbing -----------------------------------------

    @staticmethod
    def _cf_record(session, method, url, kwargs):
        headers = CaseInsensitiveDict(session.headers)
        headers.update(kwargs.get("headers") or {})
        body = kwargs.get("data")
        if body is None and kwargs.get("json") is not None:
            body = json.dumps(kwargs["json"]).encode()
        if isinstance(body, str):
            body = body.encode()
        try:
            payload = json.loads(body) if body else None
        except ValueError:
            payload = None
        return {
            "method": method.upper(),
            "url": url,
            "headers": headers,
            "body": body,
            "json": payload,
            "timeout": kwargs.get("timeout"),
            "params": kwargs.get("params"),
        }

    @classmethod
    def _cf_default_response(cls, record):
        if record["url"] == cloudflare_api.VERIFY_URL:
            return (
                200,
                {
                    "success": True,
                    "errors": [],
                    "messages": [],
                    "result": {"id": "token-id", "status": "active"},
                },
            )
        return cls._cf_success(delivered=cls._cf_recipients(record))

    @staticmethod
    def _cf_build_response(item, url):
        status, body, *rest = item
        headers = CaseInsensitiveDict(rest[0] if rest else {})
        if body is None:
            content = b""
        elif isinstance(body, bytes | bytearray):
            content = bytes(body)
        elif isinstance(body, str):
            content = body.encode()
        else:
            content = json.dumps(body).encode()
            headers.setdefault("Content-Type", "application/json")
        response = requests.Response()
        response.status_code = status
        response.url = url
        response.headers = headers
        response.encoding = "utf-8"
        response._content = content
        return response

    @staticmethod
    def _cf_recipients(record):
        """Every address a recorded send request targets (to + cc + bcc)."""
        payload = record["json"] or {}
        return [
            entry["address"] if isinstance(entry, dict) else entry
            for field in ("to", "cc", "bcc")
            for entry in payload.get(field) or []
        ]

    @staticmethod
    def _cf_success(
        delivered=None,
        queued=None,
        permanent_bounces=None,
        suppressed_recipients=None,
        message_id="cf-message-id",
    ):
        """A 200 send response in Cloudflare's envelope."""
        return (
            200,
            {
                "success": True,
                "errors": [],
                "messages": [],
                "result": {
                    "delivered": delivered or [],
                    "queued": queued or [],
                    "permanent_bounces": permanent_bounces or [],
                    "suppressed_recipients": suppressed_recipients or [],
                    "message_id": message_id,
                },
            },
        )

    @staticmethod
    def _cf_error(status, code, message, headers=None):
        """A failed response in Cloudflare's envelope (e.g. ``429, 10004``)."""
        body = {
            "success": False,
            "errors": [{"code": code, "message": message}],
            "messages": [],
            "result": None,
        }
        return (status, body, headers) if headers else (status, body)

    # -- inbound webhook helpers (HttpCase) ----------------------------------

    @staticmethod
    def _cf_sign(secret, timestamp, body):
        """``v1=<hex HMAC-SHA256(secret, "<timestamp>." + body)>``."""
        if isinstance(secret, str):
            secret = secret.encode()
        if isinstance(body, str):
            body = body.encode()
        digest = hmac.new(
            secret, f"{timestamp}.".encode() + body, hashlib.sha256
        ).hexdigest()
        return f"v1={digest}"

    def _cf_post(
        self,
        server,
        body,
        *,
        key=None,
        timestamp=None,
        signature=True,
        envelope_from="alice@example.com",
        envelope_to=None,
        worker_id="01JTESTWORKERID0000000000",
        attempt=1,
        headers=None,
    ):
        """POST ``body`` to the inbound controller the way the Worker does.

        Needs ``HttpCase`` (``url_open``) and a confirmed Cloudflare
        ``fetchmail.server``; ``key`` defaults to the server's webhook key.
        ``signature`` True computes a valid one from the server's secret, a
        string is sent verbatim (a wrong one), False/None omits the header.
        ``headers`` adds or overrides headers; a ``None`` value removes one.
        """
        if isinstance(body, str):
            body = body.encode()
        if key is None:
            key = server.cloudflare_webhook_key
        if timestamp is None:
            timestamp = int(time.time())
        if envelope_to is None:
            envelope_to = "{}@{}".format(
                getattr(self, "alias_catchall", "catchall"),
                getattr(self, "alias_domain", "example.com"),
            )
        request_headers = {
            "Content-Type": "message/rfc822",
            CF_HEADER_ID: worker_id,
            CF_HEADER_TIMESTAMP: str(timestamp),
            CF_HEADER_ENVELOPE_FROM: envelope_from,
            CF_HEADER_ENVELOPE_TO: envelope_to,
            CF_HEADER_ATTEMPT: str(attempt),
        }
        if signature is True:
            signature = self._cf_sign(
                server.sudo().cloudflare_webhook_secret, timestamp, body
            )
        if signature:
            request_headers[CF_HEADER_SIGNATURE] = signature
        for name, value in (headers or {}).items():
            if value is None:
                request_headers.pop(name, None)
            else:
                request_headers[name] = value
        return self.url_open(
            CF_INBOUND_ROUTE.format(key=key), data=body, headers=request_headers
        )


class CloudflareCommon(MailCommon, MockCloudflareCase):
    """``MailCommon`` plus a Cloudflare outgoing mail server.

    ``mail_server_cloudflare`` matches senders on ``cf.example.com`` and, with
    ``sequence=0``, is found before ``MailCommon``'s SMTP servers by
    ``_find_mail_server``. It is an empty recordset while the
    ``ir.mail_server`` extension is not loaded (the client module is tested
    on its own first), so a suite that needs it should ``assertTrue`` it.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.mail_server_cloudflare = cls._create_cloudflare_mail_server()

    @classmethod
    def _create_cloudflare_mail_server(cls, **values):
        """Create a Cloudflare ``ir.mail_server`` (empty recordset if the
        ``cloudflare`` authentication is not registered in this registry)."""
        IrMailServer = cls.env["ir.mail_server"]
        selection = IrMailServer._fields["smtp_authentication"].get_values(cls.env)
        if "cloudflare" not in selection:
            return IrMailServer
        return IrMailServer.create(
            {
                "name": "Cloudflare Email Sending",
                "smtp_authentication": "cloudflare",
                "smtp_encryption": "none",
                "cloudflare_account_id": cls.CF_ACCOUNT_ID,
                "cloudflare_api_token": cls.CF_API_TOKEN,
                "from_filter": "cf.example.com",
                "sequence": 0,
                **values,
            }
        )
