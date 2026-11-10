"""Sessions and CSRF protection for the admin screens.

Sign-in is passwordless: an emailed one-time code (app/auth/otp.py). Two kinds of login
share one session cookie, which lasts SESSION_DAYS and is extended on every visit:
- platform admins (VanakkamIQ operators): /platform
- business staff (owner or staff of one business): /app
"""

import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import HTTPException, Request
from sqlalchemy import func, select

from app.db.models import Business, PlatformAdmin, StaffUser


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


async def check_csrf(request: Request) -> None:
    form = await request.form()
    sent = str(form.get("csrf") or "")
    expected = request.session.get("csrf") or ""
    if not expected or not hmac.compare_digest(sent, expected):
        raise HTTPException(status_code=403, detail="Form expired. Go back, reload the page and try again.")


def _new_session(request: Request) -> None:
    request.session.clear()  # new session on every login
    request.session["csrf"] = secrets.token_urlsafe(32)


# --- platform admins -----------------------------------------------------------------------
async def find_platform_admin(db, email: str) -> PlatformAdmin | None:
    return await db.scalar(select(PlatformAdmin).where(func.lower(PlatformAdmin.email) == email.strip().lower()))


def log_in(request: Request, admin: PlatformAdmin) -> None:
    _new_session(request)
    request.session["platform_admin_id"] = admin.id


async def current_platform_admin(request: Request) -> PlatformAdmin:
    admin_id = request.session.get("platform_admin_id")
    admin = None
    if admin_id is not None:
        async with request.app.state.services.session_factory() as db:
            admin = await db.get(PlatformAdmin, admin_id)
    if admin is None:
        raise HTTPException(status_code=303, headers={"Location": "/platform/login"})
    return admin


# --- business staff ------------------------------------------------------------------------
@dataclass
class StaffSession:
    user: StaffUser
    business: Business

    @property
    def is_owner(self) -> bool:
        return self.user.role == "owner"


async def find_staff(db, email: str) -> StaffUser | None:
    return await db.scalar(select(StaffUser).where(func.lower(StaffUser.email) == email.strip().lower()))


async def log_in_staff(request: Request, db, user: StaffUser) -> None:
    user.last_login_at = datetime.now(timezone.utc)
    await db.commit()
    _new_session(request)
    request.session["staff_user_id"] = user.id


async def current_staff(request: Request) -> StaffSession:
    """Any signed-in staff member of a business."""
    user_id = request.session.get("staff_user_id")
    if user_id is not None:
        async with request.app.state.services.session_factory() as db:
            user = await db.get(StaffUser, user_id)
            business = await db.get(Business, user.business_id) if user else None
        if user is not None and business is not None:
            return StaffSession(user, business)
    raise HTTPException(status_code=303, headers={"Location": "/app/login"})


async def current_owner(request: Request) -> StaffSession:
    """Owners only: settings, catalog and saved-answer changes."""
    staff = await current_staff(request)
    if not staff.is_owner:
        raise HTTPException(status_code=403, detail="Only the business owner can change this.")
    return staff
