"""One-time sign-in codes, emailed (same rules as EverMindIQ).

- 6 digits, valid 10 minutes, 5 attempts, 60 seconds before another can be sent.
- Only the SHA-256 hash is stored; the code itself exists only in the email.
- Single use: a correct code is deleted. Every guard is enforced here, server-side.
- Codes are only sent to addresses that have a login, but the screens never say whether an
  address has one (the same message either way).
- Stored in the database, so any app replica can check a code another one sent.
"""

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.email import EmailError, EmailService
from app.config import Settings
from app.db.models import OtpChallenge

log = logging.getLogger(__name__)

AUDIENCES = ("platform", "staff")


class OtpError(Exception):
    """Something the person can fix; the message is safe to show them."""


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def hash_code(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def generate_code(length: int = 6) -> str:
    """Uniformly random digits, leading zeros kept."""
    return "".join(secrets.choice("0123456789") for _ in range(length))


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt  # SQLite returns naive UTC


def _message(code: str, ttl_minutes: int, where: str) -> tuple[str, str, str]:
    subject = f"{code} is your VanakkamIQ sign-in code"
    text = (f"Your VanakkamIQ sign-in code for {where} is {code}.\n\n"
            f"It expires in {ttl_minutes} minutes. If you didn't ask for it, you can ignore this email.\n")
    html = (f"<p>Your VanakkamIQ sign-in code for {where} is</p>"
            f'<p style="font-size:28px;font-weight:700;letter-spacing:4px">{code}</p>'
            f"<p>It expires in {ttl_minutes} minutes. If you didn't ask for it, you can ignore this email.</p>")
    return subject, text, html


class OtpService:
    def __init__(self, settings: Settings, email: EmailService) -> None:
        self.settings = settings
        self.email = email

    @property
    def mock(self) -> bool:
        """Local development without email: codes are logged instead of sent."""
        return self.settings.app_env == "dev" and not self.settings.email_configured

    async def request_code(self, db: AsyncSession, email: str, audience: str, *, has_account: bool) -> None:
        """Send a code if the address has a login. Raises OtpError for cooldown or setup problems."""
        assert audience in AUDIENCES
        email = normalize_email(email)
        existing = await db.scalar(select(OtpChallenge).where(OtpChallenge.email == email, OtpChallenge.audience == audience))
        now = datetime.now(timezone.utc)
        if existing is not None:
            remaining = self.settings.otp_resend_cooldown_seconds - (now - _utc(existing.sent_at)).total_seconds()
            if remaining > 0:
                raise OtpError(f"Please wait {int(remaining) + 1} seconds before asking for another code.")
        if not has_account:
            return  # same response as a real address; nothing sent, nothing stored
        if not self.email.will_send and not self.mock and self.settings.app_env == "production":
            log.error("Sign-in code requested but ACS email is not configured")
            raise OtpError("Email sign-in isn't set up yet. Contact VanakkamIQ support.")

        pinned = self.settings.mock_otp_code.strip() if self.mock else ""
        code = pinned or generate_code(self.settings.otp_code_length)
        if existing is None:
            existing = OtpChallenge(email=email, audience=audience)
            db.add(existing)
        existing.code_sha256 = hash_code(code)
        existing.attempts = 0
        existing.sent_at = now
        existing.expires_at = now + timedelta(seconds=self.settings.otp_ttl_seconds)
        await db.commit()

        if self.mock:
            log.warning("Local dev (no email configured): the sign-in code for %s is %s", email, code)
        where = "the VanakkamIQ platform" if audience == "platform" else "your business dashboard"
        subject, text, html = _message(code, self.settings.otp_ttl_seconds // 60, where)
        try:
            await self.email.send(email, subject, text, html)
        except EmailError:
            log.exception("Sign-in code email failed")
            raise OtpError("We couldn't send the email just now. Please try again in a minute.") from None

    async def verify_code(self, db: AsyncSession, email: str, audience: str, code: str) -> None:
        """Consume a code. Raises OtpError unless it is correct and still valid."""
        email = normalize_email(email)
        challenge = await db.scalar(select(OtpChallenge).where(OtpChallenge.email == email, OtpChallenge.audience == audience))
        if challenge is None:
            raise OtpError("That code isn't right or has expired. Ask for a new one.")
        if _utc(challenge.expires_at) <= datetime.now(timezone.utc):
            await db.delete(challenge)
            await db.commit()
            raise OtpError("That code has expired. Ask for a new one.")
        if challenge.attempts >= self.settings.otp_max_attempts:
            await db.delete(challenge)
            await db.commit()
            raise OtpError("Too many attempts. Ask for a new code.")
        # compare_digest so a wrong code can't be found by timing the response.
        if not secrets.compare_digest(challenge.code_sha256, hash_code("".join((code or "").split()))):
            challenge.attempts += 1
            left = self.settings.otp_max_attempts - challenge.attempts
            if left <= 0:
                await db.delete(challenge)
                await db.commit()
                raise OtpError("Too many attempts. Ask for a new code.")
            await db.commit()
            raise OtpError(f"That code isn't right. {left} attempt{'s' if left != 1 else ''} left.")
        await db.delete(challenge)  # single use
        await db.commit()
