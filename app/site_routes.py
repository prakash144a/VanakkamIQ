"""The public website (milestone 5b): the home page and "Book a demo".

A demo request is saved (platform admin → Demo requests) and emailed to DEMO_REQUEST_TO
(info@nativemindsai.com) through Azure Communication Services, with Reply-To set to the visitor,
the same way the NativeMinds company site's contact form works. Spam guards: a hidden honeypot
field, a minimum fill time, length limits and a per-address rate limit.
"""

import html
import logging
import re
import time
from collections import defaultdict, deque

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.admin.common import db_session, templates
from app.auth.email import EmailError
from app.db.models import DemoRequest

log = logging.getLogger(__name__)
router = APIRouter()

BUSINESS_TYPES = {"restaurant": "Restaurant or café", "retail": "Shop or store",
                  "services": "Service business", "other": "Other"}
_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_PHONE = re.compile(r"^[+\d][\d\s\-().]{6,24}$")
LIMITS = {"name": 200, "business_name": 200, "email": 320, "phone": 40, "message": 3000}
RATE_LIMIT, RATE_WINDOW = 5, 3600  # requests per address per hour
MIN_FILL_SECONDS = 2


class RateLimiter:
    """In-memory, per replica: enough to stop a form being hammered; not a security boundary."""

    def __init__(self, limit: int, window: float) -> None:
        self.limit, self.window = limit, window
        self.hits: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str, now: float | None = None) -> bool:
        now = now or time.monotonic()
        q = self.hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        if len(self.hits) > 10_000:  # keep memory bounded
            self.hits.clear()
        return True


limiter = RateLimiter(RATE_LIMIT, RATE_WINDOW)


def _sign_in_url(request: Request) -> str:
    owner = request.app.state.services.settings.owner_host
    return f"https://{owner}/app/login" if owner else "/app/login"


@router.get("/", include_in_schema=False)
async def home(request: Request):
    response = templates.TemplateResponse(request, "site/home.html", {
        "sign_in_url": _sign_in_url(request), "business_types": BUSINESS_TYPES, "rendered_at": int(time.time()),
    })
    response.headers["Cache-Control"] = "public, max-age=300"
    return response


def _client_key(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() or (request.client.host if request.client else "unknown")


def _validate(form) -> tuple[dict, str | None]:
    data = {k: str(form.get(k) or "").strip() for k in LIMITS}
    data["business_type"] = str(form.get("business_type") or "").strip()
    if not data["name"] or not data["business_name"]:
        return data, "Please tell us your name and your business's name."
    if not data["email"] and not data["phone"]:
        return data, "Please leave an email or a phone number so we can reach you."
    if data["email"] and not _EMAIL.match(data["email"]):
        return data, "That email address doesn't look right."
    if data["phone"] and not _PHONE.match(data["phone"]):
        return data, "That phone number doesn't look right."
    if any(len(data[k]) > n for k, n in LIMITS.items()):
        return data, "One of the fields is too long."
    if data["business_type"] not in BUSINESS_TYPES:
        data["business_type"] = "other"
    return data, None


def _email_body(data: dict, request_id: int) -> tuple[str, str, str]:
    kind = BUSINESS_TYPES.get(data["business_type"], "Other")
    subject = f"VanakkamIQ demo request: {data['business_name']}"
    rows = [("Name", data["name"]), ("Business", data["business_name"]), ("Type", kind),
            ("Email", data["email"] or "—"), ("Phone", data["phone"] or "—")]
    text = "\n".join(f"{k}: {v}" for k, v in rows) + (f"\n\n{data['message']}" if data["message"] else "") + \
        f"\n\nRequest #{request_id}: see Demo requests in the VanakkamIQ platform admin."
    body = "".join(f"<p><strong>{k}:</strong> {html.escape(v)}</p>" for k, v in rows)
    if data["message"]:
        body += f'<p style="white-space:pre-wrap">{html.escape(data["message"])}</p>'
    body += f"<p>Request #{request_id}: see <em>Demo requests</em> in the VanakkamIQ platform admin.</p>"
    return subject, text, body


def _reply(request: Request, status: int, ok: bool, message: str):
    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse({"ok": ok, "message": message}, status_code=status)
    # No JavaScript: back to the page with the result in the URL.
    return RedirectResponse(f"/?demo={'sent' if ok else 'error'}#demo", status_code=303)


@router.post("/demo", include_in_schema=False)
async def book_demo(request: Request):
    form = await request.form()
    if str(form.get("website") or "").strip():  # honeypot: people never see it; bots fill it
        return _reply(request, 200, True, "Thanks! We'll be in touch soon.")
    try:
        if time.time() - int(str(form.get("t") or "0")) < MIN_FILL_SECONDS:
            return _reply(request, 200, True, "Thanks! We'll be in touch soon.")
    except ValueError:
        pass
    if not limiter.allow(_client_key(request)):
        return _reply(request, 429, False, "Too many requests from this connection. Please email info@nativemindsai.com.")
    data, error = _validate(form)
    if error:
        return _reply(request, 400, False, error)

    services = request.app.state.services
    async with db_session(request) as db:
        demo = DemoRequest(name=data["name"], business_name=data["business_name"], business_type=data["business_type"],
                           email=data["email"] or None, phone=data["phone"] or None, message=data["message"] or None)
        db.add(demo)
        await db.commit()
        subject, text, body = _email_body(data, demo.id)
        try:
            await services.otp.email.send(services.settings.demo_request_to, subject, text, body,
                                          reply_to=(data["email"], data["name"]) if data["email"] else None)
            demo.email_sent = True
            await db.commit()
        except EmailError:
            # Saved anyway: it shows in the platform admin even when email is down.
            log.exception("Demo request #%s email failed", demo.id)
    log.info("Demo request #%s received", demo.id)
    return _reply(request, 200, True, "Thanks! We'll be in touch soon to set up your demo.")
