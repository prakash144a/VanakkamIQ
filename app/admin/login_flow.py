"""The two-step email sign-in screen shared by /platform and /app.

Step 1: the person enters their email; a code is sent if it has a login.
Step 2: they enter the code; on success the session starts.
The screens say the same thing whether or not an address has a login.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fastapi import Request
from fastapi.responses import RedirectResponse

from app.admin.common import db_session, render
from app.auth.otp import OtpError, normalize_email


@dataclass(frozen=True)
class LoginScreen:
    audience: str  # "platform" | "staff"
    action: str  # "/platform/login"
    title: str  # brand subtitle
    home: str  # where to go after signing in
    find: Callable  # async (db, email) -> account | None
    start: Callable[..., Awaitable[None]]  # async (request, db, account)
    other_link: tuple[str, str] | None = None  # (label, href)


def screen(request: Request, ls: LoginScreen, *, step: str = "email", email: str = "", error: str | None = None,
           notice: str | None = None, status_code: int = 200):
    otp = request.app.state.services.otp
    return render(request, "shared/login.html", status_code=status_code, ls=ls, step=step, email=email,
                  error=error, notice=notice, code_length=otp.settings.otp_code_length,
                  ttl_minutes=otp.settings.otp_ttl_seconds // 60, dev_hint=otp.mock)


async def send_code(request: Request, ls: LoginScreen):
    form = await request.form()
    email = normalize_email(str(form.get("email") or ""))
    if "@" not in email or "." not in email.split("@")[-1]:
        return screen(request, ls, email=email, error="Enter the email address you use for VanakkamIQ.", status_code=422)
    otp = request.app.state.services.otp
    async with db_session(request) as db:
        account = await ls.find(db, email)
        try:
            await otp.request_code(db, email, ls.audience, has_account=account is not None)
        except OtpError as exc:
            # A cooldown means a code was sent moments ago: let them type that one.
            return screen(request, ls, step="code", email=email, error=str(exc), status_code=429)
    return screen(request, ls, step="code", email=email,
                  notice=f"If {email} has a VanakkamIQ login, we've emailed it a sign-in code.")


async def check_code(request: Request, ls: LoginScreen):
    form = await request.form()
    email = normalize_email(str(form.get("email") or ""))
    code = str(form.get("code") or "")
    otp = request.app.state.services.otp
    async with db_session(request) as db:
        try:
            await otp.verify_code(db, email, ls.audience, code)
        except OtpError as exc:
            return screen(request, ls, step="code", email=email, error=str(exc), status_code=401)
        account = await ls.find(db, email)
        if account is None:  # removed between sending the code and signing in
            return screen(request, ls, email=email, error="That login no longer exists.", status_code=401)
        await ls.start(request, db, account)
    return RedirectResponse(ls.home, status_code=303)
