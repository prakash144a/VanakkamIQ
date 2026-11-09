"""Verify OpenAI webhooks (Standard Webhooks scheme: webhook-id / webhook-timestamp / webhook-signature)."""

import base64
import hashlib
import hmac
import time
from collections.abc import Mapping


class InvalidSignature(Exception):
    pass


def _key(secret: str) -> bytes:
    if secret.startswith("whsec_"):
        return base64.b64decode(secret.removeprefix("whsec_"))
    return secret.encode()


def sign(secret: str, msg_id: str, timestamp: str, body: bytes) -> str:
    mac = hmac.new(_key(secret), f"{msg_id}.{timestamp}.".encode() + body, hashlib.sha256)
    return "v1," + base64.b64encode(mac.digest()).decode()


def diagnose(secret: str, headers: Mapping[str, str], body: bytes) -> dict:
    """Explain a signature mismatch without revealing the secret or the payload."""
    import gzip
    import zlib

    msg_id = headers.get("webhook-id") or ""
    timestamp = headers.get("webhook-timestamp") or ""
    sent = [p.partition(",")[2] or p for p in (headers.get("webhook-signature") or "").split()]
    info: dict = {
        "content_encoding": headers.get("content-encoding"),
        "content_type": headers.get("content-type"),
        "content_length_header": headers.get("content-length"),
        "body_bytes": len(body),
        "body_starts_with_brace": body[:1] == b"{",
        "signature_versions": [p.partition(",")[0] for p in (headers.get("webhook-signature") or "").split()],
        "timestamp_skew_s": (int(time.time()) - int(timestamp)) if timestamp.isdigit() else None,
        "secret_len": len(secret),
        "secret_whitespace": secret != secret.strip(),
    }

    def matches(key: bytes, payload: bytes) -> bool:
        mac = hmac.new(key, f"{msg_id}.{timestamp}.".encode() + payload, hashlib.sha256)
        expected = base64.b64encode(mac.digest()).decode()
        return any(hmac.compare_digest(expected, s) for s in sent)

    keys = {"decoded": _key(secret.strip()), "raw": secret.strip().encode()}
    bodies = {"as_received": body, "stripped": body.strip()}
    for name, fn in (("gunzip", gzip.decompress), ("inflate", zlib.decompress)):
        try:
            bodies[name] = fn(body)
        except Exception:
            pass
    info["matching_variants"] = [f"{k}+{b}" for k, kv in keys.items() for b, bv in bodies.items() if matches(kv, bv)]
    return info


def verify(secret: str, headers: Mapping[str, str], body: bytes, tolerance_seconds: int = 300) -> None:
    msg_id = headers.get("webhook-id")
    timestamp = headers.get("webhook-timestamp")
    signatures = headers.get("webhook-signature")
    if not (msg_id and timestamp and signatures):
        raise InvalidSignature("missing webhook signature headers")
    try:
        ts = int(timestamp)
    except ValueError as exc:
        raise InvalidSignature("bad timestamp") from exc
    if abs(time.time() - ts) > tolerance_seconds:
        raise InvalidSignature("timestamp outside tolerance")
    expected = sign(secret, msg_id, timestamp, body).split(",", 1)[1]
    for candidate in signatures.split():
        version, _, sig = candidate.partition(",")
        if version == "v1" and hmac.compare_digest(sig, expected):
            return
    raise InvalidSignature("signature mismatch")
