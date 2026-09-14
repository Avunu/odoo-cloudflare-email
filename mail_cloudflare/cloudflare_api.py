# Copyright 2026 Avunu LLC (avu.nu)
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl.html).
"""Pure client for the Cloudflare Email Sending REST API.

No ORM in here on purpose: everything the ``ir.mail_server`` extension needs to
turn the ``email.message.EmailMessage`` core already built into a Cloudflare
send request lives in this module so it can be unit-tested without a registry
and reused outside a model. The only Odoo import is ``email_split_tuples`` so
that addresses are parsed exactly the way the rest of the mail stack parses
them.

Facts about the API this module encodes (verified against the docs on
2026-09-14):

- ``POST /accounts/{account_id}/email/sending/send`` with a Bearer token that
  carries the "Email Sending: Edit" permission. Body: ``from``, ``to``/``cc``/
  ``bcc`` (string | ``{address, name}`` | array, at most 50 addresses in
  total), ``reply_to``, ``subject``, ``text``/``html`` (at least one),
  ``headers`` (allow-listed names only, see ``HEADER_ALLOWLIST``) and
  ``attachments[] {content (base64), filename, type, disposition, content_id}``.
- Response: ``{success, errors[{code, message}], messages, result: {delivered,
  queued, permanent_bounces, suppressed_recipients, message_id}}``.
- Cloudflare owns ``Date``, ``Message-ID``, ``MIME-Version``, ``Content-*``,
  ``Return-Path``, ``Received``, ``DKIM-Signature``, ... and rewrites the
  ``Message-ID``. Odoo's own id therefore only survives inside ``References``,
  which is why ``_passthrough_headers`` appends it there.
- Limits: 5 MiB per message, 16 KB of custom headers in total, 20 custom
  headers, 2 048 bytes per header value, 50 recipients.
"""

import base64
import json
import logging
import mimetypes
import re
import time

import requests

from odoo.tools.mail import email_split_tuples

_logger = logging.getLogger(__name__)

API_BASE = "https://api.cloudflare.com/client/v4"
SEND_URL = API_BASE + "/accounts/{account_id}/email/sending/send"
VERIFY_URL = API_BASE + "/user/tokens/verify"

MAX_RECIPIENTS = 50
MAX_BODY_BYTES = 5 * 1024 * 1024
MAX_HEADER_BYTES = 16 * 1024
MAX_HEADER_COUNT = 20
MAX_HEADER_VALUE_BYTES = 2048
# What ``ir.mail_server.max_email_size`` defaults to for Cloudflare servers:
# ``mail.mail`` links record-owned attachments out instead of embedding them
# once an email would grow past it.
MAX_EMAIL_SIZE_MB = 5.0
REQUEST_TIMEOUT = 30
MAX_ATTEMPTS = 3
# Upper bound on how long one 429/5xx back-off may block the sending cron.
MAX_RETRY_AFTER = 10

# Cloudflare's allow-list (developers.cloudflare.com/email-service/reference/
# headers/), lower-cased. Anything else in the ``headers`` object makes the
# whole request fail with E_HEADER_NOT_ALLOWED, so unknown headers are dropped
# here rather than sent. ``From``/``To``/``Cc``/``Bcc``/``Subject``/``Reply-To``
# are first-class request fields and must not appear in ``headers`` either.
_ALLOWED_HEADERS = (
    # threading
    "In-Reply-To",
    "References",
    "Thread-Index",
    "Thread-Topic",
    # list management
    "List-Unsubscribe",
    "List-Unsubscribe-Post",
    "List-Id",
    "List-Archive",
    "List-Help",
    "List-Owner",
    "List-Post",
    "List-Subscribe",
    "Precedence",
    # automated messages
    "Auto-Submitted",
    # content and display
    "Content-Language",
    "Keywords",
    "Comments",
    "Importance",
    "Priority",
    "Sensitivity",
    "Organization",
    # delivery and notification
    "Require-Recipient-Valid-Since",
    "Expires",
    "Reply-By",
    # modern standards
    "Archived-At",
)
HEADER_ALLOWLIST = frozenset(name.lower() for name in _ALLOWED_HEADERS)
# Sent under the spelling above whatever the message used (core writes
# ``msg["references"]`` in lower case, for instance).
_CANONICAL_HEADER_NAMES = {name.lower(): name for name in _ALLOWED_HEADERS}
# "Any header starting with X- is allowed." The documented List-* headers are
# enumerated above instead of matched by prefix: the allow-list is closed, so a
# List-Foo that Cloudflare does not know would still be rejected.
HEADER_ALLOWED_PREFIXES = ("x-",)

# Message-ids inside References/In-Reply-To: ``<`` ... ``>`` tokens, whatever
# the separator (space, comma, folded whitespace) between them.
_MSGID_RE = re.compile(r"<[^<>]+>")

# Module-level so tests can patch it out of the retry back-off.
_sleep = time.sleep


class CloudflareEmailError(Exception):
    """A send or token check that Cloudflare refused, or that never reached it.

    Core interpolates the class name into ``MailDeliveryException``
    (``ir_mail_server.py`` ``send_email``), so a failed ``mail.mail`` shows
    ``CloudflareEmailError: <message>`` as its failure reason; keep messages
    operator-readable. ``fatal`` marks credential problems (401/403): every
    later send through the same session is pointless and fails fast.
    """

    def __init__(self, message, status=None, code=None, fatal=False):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.fatal = fatal


# ---------------------------------------------------------------------------
# MIME -> JSON mapping
# ---------------------------------------------------------------------------


def _address(pair):
    """``(name, email)`` -> Cloudflare address object.

    Objects rather than ``"Name <email>"`` strings so display names with commas
    or quotes never need RFC 5322 quoting on our side.
    """
    name, address = pair
    return {"address": address, "name": name} if name else {"address": address}


def _split_recipients(message, smtp_to_list):
    """Return ``(to, cc, bcc)`` address-object lists for the request.

    The envelope (``smtp_to_list``, computed by ``_prepare_email_message`` from
    To/Cc/Bcc and optionally narrowed by the ``send_validated_to`` context) is
    the truth about who receives the mail; the headers only add display names
    and the to/cc split. Core deleted the ``Bcc`` header before handing the
    message over, so everything in the envelope that is neither To nor Cc is a
    Bcc. The headers are parsed with ``email_split_tuples`` on ``str(...)``
    rather than ``header.addresses``: ``build_email`` assigns a *list of raw
    strings* to ``msg["To"]``, which the header registry wraps as opaque
    ``Group`` objects instead of parsing.
    """
    envelope = {}
    for address in smtp_to_list:
        # ``extract_rfc2822_addresses`` may yield the same address twice (one
        # per header it appeared in); keep the first spelling.
        envelope.setdefault(address.lower(), address)
    seen = set()

    def take(header):
        pairs = []
        for name, address in email_split_tuples(str(message[header] or "")):
            key = address.lower()
            if key in envelope and key not in seen:
                seen.add(key)
                pairs.append(_address((name, address)))
        return pairs

    to = take("To")
    cc = take("Cc")
    # No display name is available for a Bcc (or for a To/Cc whose header
    # spelling differs from the envelope, e.g. an IDNA-encoded domain).
    bcc = [
        _address(("", address)) for key, address in envelope.items() if key not in seen
    ]
    if not to and len(bcc) == 1:
        # ``to`` is mandatory. The header can stop matching the envelope when
        # core forged it (``X-Forge-To``, discuss channels), and Odoo sends one
        # message per recipient anyway, so a lone hidden recipient becomes the
        # visible one; several stay hidden rather than exposed to each other.
        to, bcc = bcc, []
    total = len(to) + len(cc) + len(bcc)
    if not total:
        raise CloudflareEmailError("No recipient left after envelope validation.")
    if total > MAX_RECIPIENTS:
        raise CloudflareEmailError(
            f"{total} recipients; Cloudflare accepts at most {MAX_RECIPIENTS} "
            "(To, Cc and Bcc combined) per message."
        )
    return to, cc, bcc


def _iter_leaves(part):
    """Yield the leaf parts of a MIME tree, treating message/rfc822 as a leaf.

    ``EmailMessage.walk()`` descends *into* an attached message (its payload is
    a list holding the inner message), which would turn the inner text parts
    into attachments of the outer mail; stopping at the rfc822 boundary keeps
    the attached message whole. Walking the tree rather than using
    ``iter_attachments()`` finds inline images that other modules nest inside
    ``multipart/related``.
    """
    if part.get_content_type() == "message/rfc822" or not part.is_multipart():
        yield part
        return
    for subpart in part.get_payload():
        yield from _iter_leaves(subpart)


def _attachment(part, index):
    """Map one non-body leaf to a Cloudflare attachment object."""
    content_type = part.get_content_type()
    if content_type == "message/rfc822":
        # ``build_email`` attaches forwarded mails as parsed messages
        # (``add_attachment(BytesParser().parsebytes(...))``); serialise the
        # inner message back to its bytes.
        content = part.get_payload()[0].as_bytes()
    else:
        content = part.get_payload(decode=True) or b""
    filename = part.get_filename()
    if not filename:
        extension = mimetypes.guess_extension(content_type) or ""
        filename = f"attachment-{index}{extension}"
    content_id = (part["Content-ID"] or "").strip()
    attachment = {
        "content": base64.b64encode(content).decode("ascii"),
        "filename": filename,
        "type": content_type,
        # Anything carrying a Content-ID is meant to be referenced from the
        # HTML (``cid:``), whatever its Content-Disposition says.
        "disposition": (
            "inline"
            if content_id or part.get_content_disposition() == "inline"
            else "attachment"
        ),
    }
    if content_id:
        # Cloudflare's own example references ``cid:company-logo`` with
        # ``contentId: "company-logo"``: the bare id, no angle brackets.
        attachment["content_id"] = content_id.strip("<>")
    return attachment


def _extract_content(message):
    """Return ``(text, html, attachments)`` for the request body.

    ``get_body`` picks the same parts a mail client would render (it skips
    parts with an attachment disposition); every other leaf becomes an
    attachment. Empty bodies are reported as ``None`` because Cloudflare
    requires at least one non-empty ``text``/``html``.
    """
    html_part = message.get_body(("html",))
    text_part = message.get_body(("plain",))
    body_parts = {id(part) for part in (html_part, text_part) if part is not None}
    text = text_part.get_content() if text_part is not None else None
    html = html_part.get_content() if html_part is not None else None
    # ``set_content("")`` still yields a newline: whitespace-only is empty
    text = text if text and text.strip() else None
    html = html if html and html.strip() else None
    attachments = []
    for part in _iter_leaves(message):
        if id(part) not in body_parts:
            attachments.append(_attachment(part, len(attachments) + 1))
    return text, html, attachments


def _header_size(headers):
    """Bytes the headers would occupy on the wire (``Name: value\\r\\n``)."""
    return sum(
        len(name.encode()) + len(value.encode()) + 4 for name, value in headers.items()
    )


def _passthrough_headers(message):
    """Return the ``headers`` object: allow-listed headers plus the References trick.

    Cloudflare replaces the ``Message-ID``, so a reply's ``In-Reply-To`` points
    at an id Odoo never saw. Mail clients copy the parent's ``References``
    into a reply, though, and ``mail.thread.message_route`` searches
    ``mail.message.message_id`` over the last 32 entries of that header. Odoo's
    own id is therefore appended to ``References`` when it is not already
    there (notification mails already end with it, see
    ``_notify_by_email_get_base_mail_values``).

    Repeated headers are joined with ``", "``. When the total would exceed
    Cloudflare's 16 KB (or References its 2 048 bytes), the oldest References
    entries are dropped first - never the own id - and an error is raised only
    if that is not enough.
    """
    headers = {}
    for name, value in message.items():
        lname = name.lower()
        if lname not in HEADER_ALLOWLIST and not lname.startswith(
            HEADER_ALLOWED_PREFIXES
        ):
            continue
        value = str(value).strip()
        key = _CANONICAL_HEADER_NAMES.get(lname) or next(
            (known for known in headers if known.lower() == lname), name
        )
        if key not in headers:
            headers[key] = value
        elif value:
            headers[key] = f"{headers[key]}, {value}"

    references = _MSGID_RE.findall(headers.get("References", ""))
    own_id = str(message["Message-Id"] or "").strip()
    if own_id and own_id not in references:
        references.append(own_id)
    if references:
        headers["References"] = " ".join(references)

    def references_oversized():
        if len(headers.get("References", "").encode()) > MAX_HEADER_VALUE_BYTES:
            return True
        return _header_size(headers) > MAX_HEADER_BYTES

    while references_oversized() and len(references) > 1:
        references.pop(0)
        headers["References"] = " ".join(references)
    too_long = [
        name
        for name, value in headers.items()
        if len(value.encode()) > MAX_HEADER_VALUE_BYTES
    ]
    if too_long or _header_size(headers) > MAX_HEADER_BYTES:
        raise CloudflareEmailError(
            f"Headers exceed Cloudflare's limits ({MAX_HEADER_BYTES} bytes in "
            f"total, {MAX_HEADER_VALUE_BYTES} bytes per value) even after "
            f"trimming References; too long: {', '.join(too_long) or 'none'}."
        )
    if len(headers) > MAX_HEADER_COUNT:
        raise CloudflareEmailError(
            f"{len(headers)} custom headers; Cloudflare accepts at most "
            f"{MAX_HEADER_COUNT}."
        )
    return headers


def build_payload(message, smtp_to_list):
    """Map the message core prepared into the JSON body of a send request.

    ``message`` is what ``_prepare_email_message`` returns: ``From`` already
    encapsulated when the server's ``from_filter`` did not match, ``Bcc``
    removed, recipients validated into ``smtp_to_list``.
    """
    senders = email_split_tuples(str(message["From"] or ""))
    if not senders:
        raise CloudflareEmailError("The message has no valid From address.")
    to, cc, bcc = _split_recipients(message, smtp_to_list)
    text, html, attachments = _extract_content(message)
    if text is None and html is None:
        raise CloudflareEmailError(
            "The message has neither a text nor an html body; Cloudflare requires "
            "at least one."
        )

    payload = {
        "from": _address(senders[0]),
        "to": to,
        "subject": str(message["Subject"] or ""),
    }
    if cc:
        payload["cc"] = cc
    if bcc:
        payload["bcc"] = bcc
    # ``reply_to`` is a single address in the API; core sets Reply-To to one
    # formatted address (``build_email``), keep the first if there are more.
    reply_to = email_split_tuples(str(message["Reply-To"] or ""))
    if reply_to:
        payload["reply_to"] = _address(reply_to[0])
    if text is not None:
        payload["text"] = text
    if html is not None:
        payload["html"] = html
    headers = _passthrough_headers(message)
    if headers:
        payload["headers"] = headers
    if attachments:
        payload["attachments"] = attachments
    return payload


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _parse_json(response):
    """The decoded JSON body, or ``None`` when the body is not a JSON object."""
    try:
        payload = response.json()
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _describe(status, payload):
    """One line for logs and failure reasons: HTTP status + Cloudflare errors."""
    errors = (payload or {}).get("errors") or []
    details = "; ".join(
        f"{error.get('code')}: {error.get('message')}"
        for error in errors
        if isinstance(error, dict)
    )
    return f"HTTP {status}" + (f" ({details})" if details else "")


def _first_code(payload):
    errors = (payload or {}).get("errors") or []
    return errors[0].get("code") if errors and isinstance(errors[0], dict) else None


def _retry_after(response):
    """``Retry-After`` in seconds, or ``None`` when absent or not numeric."""
    try:
        return int(response.headers.get("Retry-After", ""))
    except ValueError:
        return None


def verify_token(api_token, http=None):
    """Check that ``api_token`` is active via ``GET /user/tokens/verify``.

    Returns Cloudflare's ``result`` object (``{id, status, ...}``) and raises
    ``CloudflareEmailError`` otherwise; 401/403 are ``fatal``. ``http`` may be
    any ``requests.Session``-like object (tests inject one).
    """
    owns_http = http is None
    http = http or requests.Session()
    try:
        try:
            response = http.get(
                VERIFY_URL,
                headers={"Authorization": f"Bearer {api_token}"},
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise CloudflareEmailError(f"Could not reach Cloudflare: {exc}") from exc
    finally:
        if owns_http:
            http.close()
    payload = _parse_json(response)
    status = response.status_code
    if status in (401, 403):
        raise CloudflareEmailError(
            f"Cloudflare rejected the API token: {_describe(status, payload)}",
            status=status,
            code=_first_code(payload),
            fatal=True,
        )
    if status != 200 or not payload or not payload.get("success"):
        raise CloudflareEmailError(
            f"Token verification failed: {_describe(status, payload)}",
            status=status,
            code=_first_code(payload),
        )
    result = payload.get("result") or {}
    if result.get("status") != "active":
        raise CloudflareEmailError(
            f"The API token is not active (status: {result.get('status')!r})."
        )
    return result


class CloudflareSendingSession:
    """What ``ir.mail_server.connect()`` returns for a Cloudflare server.

    Duck-types the parts of ``SMTPConnection`` core touches after ``connect``:
    ``from_filter``/``smtp_from`` (read by ``_prepare_email_message`` to decide
    whether to encapsulate the From header), ``mail_server_name`` (in the
    delivery-failure message), ``send_message`` and ``quit``/``close``.

    Construction has no side effect - ``mail.mail.send()`` opens one session
    per batch even when nothing ends up being sent - and a session may serve
    many ``send_message`` calls (one per recipient of a ``mail.mail``).
    """

    def __init__(
        self,
        account_id,
        api_token,
        *,
        from_filter,
        smtp_from,
        mail_server_name,
        http=None,
    ):
        self.account_id = account_id
        self.from_filter = from_filter
        self.smtp_from = smtp_from
        self.mail_server_name = mail_server_name
        self._send_url = SEND_URL.format(account_id=account_id)
        # Sent per request instead of stored on the session object so an
        # injected ``http`` is left untouched and the token never sits on a
        # shared object.
        self._headers = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        self._http = http or requests.Session()
        self._fatal_error = None

    # -- SMTPConnection duck-typing ------------------------------------------

    def send_message(self, message, smtp_from, smtp_to_list):
        """Send one prepared message; return Cloudflare's message id.

        ``smtp_from`` is deliberately unused: the envelope sender
        (``Return-Path``) is reserved by Cloudflare, and core already rewrote
        the ``From`` header to the address it wants shown. The returned id is
        informational - core keeps returning Odoo's ``Message-Id`` from
        ``send_email`` and that is what ``mail.message.message_id`` stores.
        """
        if self._fatal_error is not None:
            raise CloudflareEmailError(
                "Not retried: an earlier send in this batch failed with "
                f"{self._fatal_error}",
                status=self._fatal_error.status,
                code=self._fatal_error.code,
                fatal=True,
            )
        payload = build_payload(message, smtp_to_list)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if len(body) > MAX_BODY_BYTES:
            raise CloudflareEmailError(
                f"Message is {len(body) / 1024 / 1024:.1f} MiB once encoded; "
                f"Cloudflare accepts at most {MAX_BODY_BYTES // 1024 // 1024} MiB."
            )
        result = self._post(body)
        self._check_recipients(result, payload)
        _logger.info(
            "Cloudflare accepted %s as %s",
            message["Message-Id"],
            result.get("message_id"),
        )
        return result.get("message_id") or ""

    def quit(self):
        self.close()

    def close(self):
        """Release the HTTP connection pool; never raises (called in cleanup)."""
        try:
            self._http.close()
        except Exception:  # noqa: BLE001 - cleanup, nothing a caller could do
            _logger.debug("Ignoring error while closing the Cloudflare session")

    # -- internals ------------------------------------------------------------

    def _post(self, body):
        """POST ``body`` with the retry matrix; return Cloudflare's ``result``.

        - network error, 5xx: retry with a 1 s, 2 s back-off;
        - 429: retry after ``Retry-After`` (else 2^attempt), capped at 10 s;
        - 401/403: remembered as fatal so the rest of the batch fails fast;
        - 200 without ``success: true``, or any other 4xx: raised, no retry
          (the request is malformed or too big, retrying cannot help).
        """
        last_error = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = self._http.post(
                    self._send_url,
                    data=body,
                    headers=self._headers,
                    timeout=REQUEST_TIMEOUT,
                )
            except requests.RequestException as exc:
                last_error = CloudflareEmailError(f"Could not reach Cloudflare: {exc}")
                delay = 2 ** (attempt - 1)
            else:
                status = response.status_code
                payload = _parse_json(response)
                if status == 200:
                    if payload and payload.get("success"):
                        return payload.get("result") or {}
                    raise CloudflareEmailError(
                        f"Cloudflare answered {_describe(status, payload)} "
                        "without success: true",
                        status=status,
                        code=_first_code(payload),
                    )
                last_error = CloudflareEmailError(
                    f"Cloudflare refused the message: {_describe(status, payload)}",
                    status=status,
                    code=_first_code(payload),
                    fatal=status in (401, 403),
                )
                if last_error.fatal:
                    self._fatal_error = last_error
                    raise last_error
                if status == 429:
                    delay = _retry_after(response) or 2**attempt
                elif status >= 500:
                    delay = 2 ** (attempt - 1)
                else:
                    raise last_error
            if attempt < MAX_ATTEMPTS:
                delay = min(delay, MAX_RETRY_AFTER)
                _logger.info(
                    "Cloudflare send attempt %s/%s failed (%s), retrying in %ss",
                    attempt,
                    MAX_ATTEMPTS,
                    last_error,
                    delay,
                )
                _sleep(delay)
        raise CloudflareEmailError(
            f"Giving up after {MAX_ATTEMPTS} attempts: {last_error}",
            status=last_error.status,
            code=last_error.code,
        )

    def _check_recipients(self, result, payload):
        """Turn per-recipient failures in a 200 into an error or a warning.

        Cloudflare's asynchronous bounces go to its own ``cf-bounce``
        subdomain, so the synchronous ``permanent_bounces`` and
        ``suppressed_recipients`` arrays are the only bounce signal Odoo ever
        sees. Nobody reached -> raise (the ``mail.mail`` ends in ``exception``
        with the reason); some reached -> warn and report success.
        """
        recipients = {
            entry["address"].lower()
            for field in ("to", "cc", "bcc")
            for entry in payload.get(field) or []
        }
        bounced = {address.lower() for address in result.get("permanent_bounces") or []}
        suppressed = {
            address.lower() for address in result.get("suppressed_recipients") or []
        }
        failed = bounced | suppressed
        if not failed:
            return
        reached = (result.get("delivered") or []) + (result.get("queued") or [])
        detail = (
            f"permanently bounced: {sorted(bounced)}; suppressed: {sorted(suppressed)}"
        )
        if failed >= recipients or not reached:
            raise CloudflareEmailError(f"No recipient could be reached ({detail}).")
        _logger.warning(
            "Cloudflare could not reach every recipient of %s (%s)",
            result.get("message_id"),
            detail,
        )
