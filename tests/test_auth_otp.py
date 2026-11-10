"""Passwordless sign-in: ACS email signing and the one-time code rules."""

import base64
import hashlib
import hmac
import logging
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx
from sqlalchemy import select

from app.auth.email import EmailError, EmailService, build_headers, build_signature, content_hash, parse_connection_string
from app.auth.otp import OtpError, OtpService, hash_code
from app.db.models import OtpChallenge
from tests.conftest import make_settings

KEY = base64.b64encode(b"super-secret-key").decode()
CONN = f"endpoint=https://viq.communication.azure.com/;accesskey={KEY}"


# --- email signing (pinned vectors: a mistake here silently 401s in production) ----------
def test_parses_a_connection_string_in_any_order():
    creds = parse_connection_string(f"  accesskey={KEY} ; endpoint=https://viq.communication.azure.com  ")
    assert creds.endpoint == "https://viq.communication.azure.com/" and creds.access_key == b"super-secret-key"
    assert creds.host == "viq.communication.azure.com"
    padded = base64.b64encode(b"x" * 32).decode()
    assert padded.endswith("=") and parse_connection_string(f"endpoint=https://x/;accesskey={padded}").access_key == b"x" * 32
    with pytest.raises(EmailError, match="endpoint"):
        parse_connection_string(f"accesskey={KEY}")


def test_signature_matches_the_documented_string_to_sign():
    creds = parse_connection_string(CONN)
    date, body_hash, path = "Mon, 09 Nov 2026 12:00:00 GMT", content_hash(b"{}"), "/emails:send?api-version=2023-03-31"
    expected = base64.b64encode(hmac.new(
        b"super-secret-key", f"POST\n{path}\n{date};{creds.host};{body_hash}".encode(), hashlib.sha256).digest()).decode()
    assert build_signature(creds, method="POST", path_and_query=path, date_header=date, body_hash=body_hash) == expected
    headers = build_headers(creds, path_and_query=path, body=b"{}", date_header=date)
    assert headers["Authorization"].endswith(f"&Signature={expected}") and headers["x-ms-content-sha256"] == body_hash


@respx.mock
async def test_sends_through_acs_when_configured():
    route = respx.post("https://viq.communication.azure.com/emails:send?api-version=2023-03-31").mock(
        return_value=httpx.Response(202))
    async with httpx.AsyncClient() as http:
        service = EmailService(http, CONN, "DoNotReply@nativemindsai.com")
        await service.send("owner@example.com", "Subject", "Body")
    assert route.called and not service.outbox
    sent = route.calls.last.request
    assert b'"senderAddress": "DoNotReply@nativemindsai.com"' in sent.content
    assert sent.headers["Authorization"].startswith("HMAC-SHA256 SignedHeaders=x-ms-date;host;x-ms-content-sha256")


@respx.mock
async def test_acs_rejection_is_an_error():
    respx.post(url__regex=r"https://viq\.communication\.azure\.com/.*").mock(return_value=httpx.Response(401, text="no"))
    async with httpx.AsyncClient() as http:
        with pytest.raises(EmailError, match="401"):
            await EmailService(http, CONN, "x@y.com").send("a@b.com", "s", "t")


# --- code rules --------------------------------------------------------------------------
def last_code(service: OtpService) -> str:
    to, subject, text = service.email.outbox[-1]
    return subject.split()[0]


async def test_code_is_emailed_hashed_and_single_use(services):
    otp = services.otp
    async with services.session_factory() as db:
        await otp.request_code(db, " Owner@Example.com ", "staff", has_account=True)
        code = last_code(otp)
        assert len(code) == 6 and code.isdigit()
        stored = await db.scalar(select(OtpChallenge))
        assert stored.email == "owner@example.com" and stored.code_sha256 == hash_code(code)
        assert code not in stored.code_sha256
        await otp.verify_code(db, "owner@example.com", "staff", code)
        with pytest.raises(OtpError):
            await otp.verify_code(db, "owner@example.com", "staff", code)  # already used


async def test_unknown_address_gets_nothing_but_same_answer(services):
    async with services.session_factory() as db:
        await services.otp.request_code(db, "nobody@example.com", "staff", has_account=False)
        assert not services.otp.email.outbox and await db.scalar(select(OtpChallenge)) is None
        with pytest.raises(OtpError, match="isn't right or has expired"):
            await services.otp.verify_code(db, "nobody@example.com", "staff", "123456")


async def test_codes_are_per_audience(services):
    async with services.session_factory() as db:
        await services.otp.request_code(db, "a@example.com", "staff", has_account=True)
        with pytest.raises(OtpError):
            await services.otp.verify_code(db, "a@example.com", "platform", last_code(services.otp))


async def test_resend_cooldown(services):
    async with services.session_factory() as db:
        await services.otp.request_code(db, "a@example.com", "staff", has_account=True)
        with pytest.raises(OtpError, match="Please wait"):
            await services.otp.request_code(db, "a@example.com", "staff", has_account=True)
        challenge = await db.scalar(select(OtpChallenge))
        challenge.sent_at = datetime.now(timezone.utc) - timedelta(seconds=61)
        await db.commit()
        await services.otp.request_code(db, "a@example.com", "staff", has_account=True)  # allowed again


async def test_five_wrong_attempts_burn_the_code(services):
    async with services.session_factory() as db:
        await services.otp.request_code(db, "a@example.com", "staff", has_account=True)
        code = last_code(services.otp)
        wrong = "000000" if code != "000000" else "111111"
        for left in (4, 3, 2, 1):
            with pytest.raises(OtpError, match=f"{left} attempt"):
                await services.otp.verify_code(db, "a@example.com", "staff", wrong)
        with pytest.raises(OtpError, match="Too many attempts"):
            await services.otp.verify_code(db, "a@example.com", "staff", wrong)
        with pytest.raises(OtpError):
            await services.otp.verify_code(db, "a@example.com", "staff", code)  # even the right one


async def test_expired_code_is_rejected(services):
    async with services.session_factory() as db:
        await services.otp.request_code(db, "a@example.com", "staff", has_account=True)
        code = last_code(services.otp)
        challenge = await db.scalar(select(OtpChallenge))
        challenge.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db.commit()
        with pytest.raises(OtpError, match="expired"):
            await services.otp.verify_code(db, "a@example.com", "staff", code)


async def test_production_without_email_refuses_and_never_pins(services, tmp_path, caplog):
    settings = make_settings(tmp_path)
    settings.app_env, settings.mock_otp_code = "production", "123456"
    otp = OtpService(settings, EmailService(services.http, "", ""))
    assert not otp.mock
    async with services.session_factory() as db:
        with pytest.raises(OtpError, match="isn't set up"):
            await otp.request_code(db, "a@example.com", "platform", has_account=True)


async def test_local_dev_pins_and_logs_the_code(services, tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="app.auth.otp")
    settings = make_settings(tmp_path)
    settings.app_env, settings.mock_otp_code = "dev", "424242"
    otp = OtpService(settings, EmailService(services.http, "", ""))
    async with services.session_factory() as db:
        await otp.request_code(db, "dev@example.com", "staff", has_account=True)
        assert "424242" in caplog.text
        await otp.verify_code(db, "dev@example.com", "staff", "424 242")  # spaces are fine
