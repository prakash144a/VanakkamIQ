"""Session login and CSRF protection for the admin screens."""

import hmac
import secrets

from fastapi import HTTPException, Request
from sqlalchemy import func, select

from app.db.models import PlatformAdmin
from app.security import hash_password, verify_password

_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))  # equalizes timing for unknown emails


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


async def authenticate_platform_admin(db, email: str, password: str) -> PlatformAdmin | None:
    admin = await db.scalar(select(PlatformAdmin).where(func.lower(PlatformAdmin.email) == email.strip().lower()))
    if admin is None:
        verify_password(_DUMMY_HASH, password)
        return None
    return admin if verify_password(admin.password_hash, password) else None


def log_in(request: Request, admin: PlatformAdmin) -> None:
    request.session.clear()  # new session on login
    request.session["platform_admin_id"] = admin.id
    request.session["csrf"] = secrets.token_urlsafe(32)


async def current_platform_admin(request: Request) -> PlatformAdmin:
    admin_id = request.session.get("platform_admin_id")
    admin = None
    if admin_id is not None:
        async with request.app.state.services.session_factory() as db:
            admin = await db.get(PlatformAdmin, admin_id)
    if admin is None:
        raise HTTPException(status_code=303, headers={"Location": "/platform/login"})
    return admin
