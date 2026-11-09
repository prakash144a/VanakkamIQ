"""Platform admin: VanakkamIQ operators sign in, list restaurants and add new ones."""

from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import lazyload

from app.admin.auth import authenticate_platform_admin, check_csrf, csrf_token, current_platform_admin, log_in
from app.business_hours import DAY_NAMES, DAYS
from app.db.models import (
    Business, Conversation, Faq, Handoff, MenuCategory, MenuItem, Order, PhoneNumber, PlatformAdmin, StaffUser,
)
from app.money import format_money
from app.onboarding.service import (
    LANGUAGES, TIMEZONES, VOICES, OnboardingError, create_restaurant, parse_form, setup_checklist,
)

router = APIRouter(prefix="/platform")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.globals.update(DAYS=DAYS, DAY_NAMES=DAY_NAMES, format_money=format_money)

MENU_EXAMPLE = """# Biryani
Chicken Biryani | 15.99 | spicy | aka: chicken biriyani | Spice level*: Mild, Medium, Hot | Add-ons (up to 2): Extra raita +1.50, Boiled egg +2.00
Vegetable Biryani | 13.99 | vegetarian | Spice level*: Mild, Medium, Hot

# Drinks
Mango Lassi | 4.99 | vegetarian"""

FAQ_EXAMPLE = """Is there parking? | Yes, free parking behind the restaurant.
Do you deliver? | Yes, within 5 miles. Delivery takes 40 to 50 minutes."""


def new_form_defaults() -> dict:
    values = {
        "timezone": "America/New_York", "language": "en-US", "voice": "marin",
        "offers_pickup": "on", "is_active": "on",
    }
    for day in DAYS:
        values[f"open_{day}"], values[f"close_{day}"] = "11:00", "22:00"
    return values


def render(request: Request, template: str, status_code: int = 200, **context):
    return templates.TemplateResponse(
        request, template, {"csrf": csrf_token(request), **context}, status_code=status_code
    )


def _db(request: Request):
    return request.app.state.services.session_factory()


# --- sign in / out -----------------------------------------------------------------------
@router.get("/login")
async def login_page(request: Request):
    return render(request, "platform/login.html", error=None, email="")


@router.post("/login", dependencies=[Depends(check_csrf)])
async def login(request: Request):
    form = await request.form()
    email, password = str(form.get("email") or ""), str(form.get("password") or "")
    async with _db(request) as db:
        admin = await authenticate_platform_admin(db, email, password)
    if admin is None:
        return render(request, "platform/login.html", status_code=401, error="Email or password is incorrect.", email=email)
    log_in(request, admin)
    return RedirectResponse("/platform", status_code=303)


@router.post("/logout", dependencies=[Depends(check_csrf)])
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/platform/login", status_code=303)


# --- restaurants -------------------------------------------------------------------------
@router.get("")
async def restaurant_list(request: Request, admin: PlatformAdmin = Depends(current_platform_admin)):
    async with _db(request) as db:
        businesses = list(await db.scalars(select(Business).order_by(Business.created_at.desc())))
        numbers: dict[int, list[str]] = {}
        for business_id, e164 in await db.execute(select(PhoneNumber.business_id, PhoneNumber.e164)):
            numbers.setdefault(business_id, []).append(e164)
        item_counts = dict((await db.execute(
            select(MenuItem.business_id, func.count()).group_by(MenuItem.business_id)
        )).all())
    rows = [
        {"business": b, "numbers": numbers.get(b.id, []), "items": item_counts.get(b.id, 0)}
        for b in businesses
    ]
    return render(request, "platform/restaurants.html", admin=admin, rows=rows)


@router.get("/restaurants/new")
async def new_restaurant(request: Request, admin: PlatformAdmin = Depends(current_platform_admin)):
    return render(request, "platform/restaurant_new.html", admin=admin, v=new_form_defaults(), errors=[],
                  timezones=TIMEZONES, voices=VOICES, languages=LANGUAGES,
                  menu_example=MENU_EXAMPLE, faq_example=FAQ_EXAMPLE)


@router.post("/restaurants", dependencies=[Depends(check_csrf)])
async def create(request: Request, admin: PlatformAdmin = Depends(current_platform_admin)):
    form = await request.form()
    values = {k: str(v) for k, v in form.items() if k not in ("csrf", "owner_password")}
    data, errors = parse_form(values | {"owner_password": str(form.get("owner_password") or "")})
    created = None
    if data is not None:
        async with _db(request) as db:
            try:
                created = await create_restaurant(db, data)
            except OnboardingError as exc:
                errors = exc.errors
    if created is None:
        return render(request, "platform/restaurant_new.html", status_code=422, admin=admin, v=values,
                      errors=errors, timezones=TIMEZONES, voices=VOICES, languages=LANGUAGES,
                      menu_example=MENU_EXAMPLE, faq_example=FAQ_EXAMPLE)
    # Rendered directly (not redirected) so a generated owner password is shown exactly once.
    return await _detail(request, admin, created.business.id, created=created)


@router.get("/restaurants/{business_id}")
async def restaurant_detail(request: Request, business_id: int, admin: PlatformAdmin = Depends(current_platform_admin)):
    return await _detail(request, admin, business_id)


async def _detail(request: Request, admin: PlatformAdmin, business_id: int, created=None):
    async with _db(request) as db:
        business = await db.get(Business, business_id)
        if business is None:
            return render(request, "platform/not_found.html", status_code=404, admin=admin)
        checklist = await setup_checklist(db, business)
        numbers = list(await db.scalars(select(PhoneNumber.e164).where(PhoneNumber.business_id == business.id)))
        owners = list(await db.scalars(select(StaffUser).where(StaffUser.business_id == business.id)))
        categories = list(await db.scalars(
            select(MenuCategory).where(MenuCategory.business_id == business.id).order_by(MenuCategory.sort_order)
        ))
        items = list(await db.scalars(
            select(MenuItem).where(MenuItem.business_id == business.id).order_by(MenuItem.sort_order)
        ))
        faqs = list(await db.scalars(select(Faq).where(Faq.business_id == business.id)))
    menu = [(c, [i for i in items if i.category_id == c.id]) for c in categories]
    ready = all(c.done for c in checklist if c.required)
    return render(request, "platform/restaurant_detail.html", admin=admin, b=business, checklist=checklist,
                  ready=ready, numbers=numbers, owners=owners, menu=menu, faqs=faqs, created=created,
                  openai_project_id=request.app.state.services.settings.openai_project_id)


def _local(dt: datetime | None, business: Business) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:  # SQLite returns naive UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo(business.timezone))


def _duration(convo: Conversation) -> str:
    if not convo.ended_at:
        return "in progress" if convo.status == "active" else "—"
    start, end = (d if d.tzinfo else d.replace(tzinfo=timezone.utc) for d in (convo.started_at, convo.ended_at))
    seconds = int((end - start).total_seconds())
    return f"{seconds // 60}m {seconds % 60:02d}s"


@router.get("/restaurants/{business_id}/calls")
async def recent_calls(request: Request, business_id: int, admin: PlatformAdmin = Depends(current_platform_admin)):
    async with _db(request) as db:
        business = await db.get(Business, business_id)
        if business is None:
            return render(request, "platform/not_found.html", status_code=404, admin=admin)
        convos = list(await db.scalars(
            select(Conversation).options(lazyload(Conversation.events))
            .where(Conversation.business_id == business_id)
            .order_by(Conversation.started_at.desc()).limit(50)
        ))
        orders = {
            o.conversation_id: o for o in await db.scalars(
                select(Order).where(Order.conversation_id.in_([c.id for c in convos] or [0]))
            )
        }
    rows = [{"c": c, "order": orders.get(c.id), "when": _local(c.started_at, business), "duration": _duration(c)} for c in convos]
    return render(request, "platform/calls.html", admin=admin, b=business, rows=rows)


@router.get("/conversations/{conversation_id}")
async def conversation_detail(request: Request, conversation_id: int, admin: PlatformAdmin = Depends(current_platform_admin)):
    async with _db(request) as db:
        convo = await db.get(Conversation, conversation_id)
        if convo is None:
            return render(request, "platform/not_found.html", status_code=404, admin=admin)
        business = await db.get(Business, convo.business_id)
        orders = list(await db.scalars(select(Order).where(Order.conversation_id == convo.id)))
        handoffs = list(await db.scalars(select(Handoff).where(Handoff.conversation_id == convo.id)))
    events = [{"e": e, "when": _local(e.created_at, business)} for e in convo.events]
    return render(request, "platform/conversation.html", admin=admin, b=business, c=convo, events=events,
                  orders=orders, handoffs=handoffs, when=_local(convo.started_at, business), duration=_duration(convo))


@router.post("/restaurants/{business_id}/status", dependencies=[Depends(check_csrf)])
async def set_status(request: Request, business_id: int, admin: PlatformAdmin = Depends(current_platform_admin)):
    form = await request.form()
    async with _db(request) as db:
        business = await db.get(Business, business_id)
        if business is not None:
            business.is_active = form.get("active") == "1"
            await db.commit()
    return RedirectResponse(f"/platform/restaurants/{business_id}", status_code=303)
