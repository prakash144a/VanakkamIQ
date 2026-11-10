"""Transactional email via Azure Communication Services (same approach as EverMindIQ).

The ACS REST API is called directly with httpx (already a dependency) instead of the SDK.

Signing (ACS "HMAC-SHA256" scheme):

    stringToSign = "POST\\n{path?query}\\n{x-ms-date};{host};{content-sha256}"
    signature    = base64(HMAC_SHA256(base64decode(accesskey), stringToSign))

Without a connection string nothing is sent: the message is kept in a short in-memory
outbox (tests read sign-in codes from it) and logged without its body.
"""

import base64
import hashlib
import hmac
import json
import logging
from collections import deque
from dataclasses import dataclass
from email.utils import formatdate
from urllib.parse import urlsplit

import httpx

log = logging.getLogger(__name__)

API_VERSION = "2023-03-31"
OUTBOX_KEPT = 50


class EmailError(RuntimeError):
    """The provider rejected the message, or is not configured."""


@dataclass(frozen=True)
class AcsCredentials:
    endpoint: str  # "https://x.communication.azure.com/"
    access_key: bytes  # already base64-decoded

    @property
    def host(self) -> str:
        return urlsplit(self.endpoint).netloc


def parse_connection_string(value: str) -> AcsCredentials:
    """Split 'endpoint=...;accesskey=...' (any order, tolerant of whitespace)."""
    parts: dict[str, str] = {}
    for segment in value.split(";"):
        key, sep, val = segment.strip().partition("=")
        if sep:
            parts[key.strip().lower()] = val.strip()  # the key is base64: split once only
    endpoint, access_key = parts.get("endpoint", ""), parts.get("accesskey", "")
    if not endpoint or not access_key:
        raise EmailError("ACS connection string must contain endpoint= and accesskey=")
    if not endpoint.endswith("/"):
        endpoint += "/"
    try:
        decoded = base64.b64decode(access_key)
    except Exception as exc:  # noqa: BLE001 - surfaced as a config error
        raise EmailError("ACS accesskey is not valid base64") from exc
    return AcsCredentials(endpoint=endpoint, access_key=decoded)


def content_hash(body: bytes) -> str:
    return base64.b64encode(hashlib.sha256(body).digest()).decode()


def build_signature(creds: AcsCredentials, *, method: str, path_and_query: str, date_header: str, body_hash: str) -> str:
    string_to_sign = f"{method}\n{path_and_query}\n{date_header};{creds.host};{body_hash}"
    digest = hmac.new(creds.access_key, string_to_sign.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def build_headers(creds: AcsCredentials, *, path_and_query: str, body: bytes, date_header: str | None = None) -> dict:
    date_header = date_header or formatdate(timeval=None, localtime=False, usegmt=True)  # RFC 1123, GMT
    body_hash = content_hash(body)
    signature = build_signature(creds, method="POST", path_and_query=path_and_query,
                                date_header=date_header, body_hash=body_hash)
    return {
        "Content-Type": "application/json",
        "x-ms-date": date_header,
        "x-ms-content-sha256": body_hash,
        "Authorization": f"HMAC-SHA256 SignedHeaders=x-ms-date;host;x-ms-content-sha256&Signature={signature}",
    }


def build_payload(sender: str, to: str, subject: str, text: str, html: str = "",
                  reply_to: tuple[str, str] | None = None) -> dict:
    payload = {
        "senderAddress": sender,
        "recipients": {"to": [{"address": to}]},
        "content": {"subject": subject, "plainText": text, **({"html": html} if html else {})},
    }
    if reply_to:  # (address, display name): "Reply" goes to the visitor, not the no-reply sender
        payload["replyTo"] = [{"address": reply_to[0], "displayName": reply_to[1]}]
    return payload


class EmailService:
    def __init__(self, http: httpx.AsyncClient, connection_string: str, sender: str) -> None:
        self._http = http
        self._connection_string = connection_string
        self._sender = sender
        self.outbox: deque[tuple] = deque(maxlen=OUTBOX_KEPT)  # (to, subject, text[, reply_to]) when not sending

    @property
    def will_send(self) -> bool:
        return bool(self._connection_string and self._sender)

    async def send(self, to: str, subject: str, text: str, html: str = "",
                   reply_to: tuple[str, str] | None = None) -> None:
        if not self.will_send:
            self.outbox.append((to, subject, text, reply_to) if reply_to else (to, subject, text))
            log.info("Email not sent (ACS not configured): %d characters", len(text))
            return
        creds = parse_connection_string(self._connection_string)
        path_and_query = f"/emails:send?api-version={API_VERSION}"
        body = json.dumps(build_payload(self._sender, to, subject, text, html, reply_to)).encode()
        headers = build_headers(creds, path_and_query=path_and_query, body=body)
        try:
            resp = await self._http.post(f"{creds.endpoint.rstrip('/')}{path_and_query}", content=body,
                                         headers=headers, timeout=15.0)
        except httpx.HTTPError as exc:
            raise EmailError(f"Could not reach the email service: {exc}") from exc
        # 202 Accepted: ACS delivers asynchronously.
        if resp.status_code != 202:
            raise EmailError(f"The email service rejected the message ({resp.status_code}): {resp.text[:300]}")
