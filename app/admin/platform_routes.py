"""Platform admin (/platform): VanakkamIQ operators add and manage every business."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select

from app.admin.auth import check_csrf, current_platform_admin, find_platform_admin, log_in
from app.admin.common import conversation_view, db_session, recent_calls, render
from app.admin.login_flow import LoginScreen, check_code, screen, send_code
from app.business_types import DEFAULT_TYPE, TYPES, get_type
from app.db.models import Business, CatalogCategory, CatalogItem, Conversation, Faq, PhoneNumber, PlatformAdmin, StaffUser
from app.onboarding.service import (
    OnboardingError, business_form_values, create_business, form_options, new_form_defaults, parse_business_fields,
    parse_form, parse_phone, setup_checklist, today_for, update_business,
)
from app.usage import usage_since

router = APIRouter(prefix="/platform")


def go(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


# --- sign in / out (emailed one-time code) ------------------------------------------------
async def _start_admin_session(request: Request, db, admin: PlatformAdmin) -> None:
    log_in(request, admin)


LOGIN = LoginScreen(audience="platform", action="/platform/login", title="Platform admin", home="/platform",
                    find=find_platform_admin, start=_start_admin_session,
                    other_link=("Business owner?", "/app/login"))


@router.get("/login")
async def login_page(request: Request):
    return screen(request, LOGIN)


@router.post("/login", dependencies=[Depends(check_csrf)])
async def login_send_code(request: Request):
    return await send_code(request, LOGIN)


@router.post("/login/verify", dependencies=[Depends(check_csrf)])
async def login_verify(request: Request):
    return await check_code(request, LOGIN)


@router.post("/logout", dependencies=[Depends(check_csrf)])
async def logout(request: Request):
    request.session.clear()
    return go("/platform/login")


# --- businesses ----------------------------------------------------------------------------
@router.get("")
async def business_list(request: Request, admin: PlatformAdmin = Depends(current_platform_admin)):
    async with db_session(request) as db:
        businesses = list(await db.scalars(select(Business).order_by(Business.created_at.desc())))
        numbers: dict[int, list[str]] = {}
        for business_id, e164 in await db.execute(select(PhoneNumber.business_id, PhoneNumber.e164)):
            numbers.setdefault(business_id, []).append(e164)
        item_counts = dict((await db.execute(
            select(CatalogItem.business_id, func.count()).group_by(CatalogItem.business_id))).all())
        faq_counts = dict((await db.execute(select(Faq.business_id, func.count()).group_by(Faq.business_id))).all())
    rows = [{"business": b, "type": get_type(b.business_type), "numbers": numbers.get(b.id, []),
             "items": item_counts.get(b.id, 0), "faqs": faq_counts.get(b.id, 0)} for b in businesses]
    return render(request, "platform/businesses.html", admin=admin, rows=rows)


def _new_form(request: Request, admin: PlatformAdmin, v: dict, errors: list[str], status_code: int = 200):
    return render(request, "platform/business_new.html", status_code=status_code, admin=admin, v=v, errors=errors,
                  **form_options())


@router.get("/businesses/new")
async def new_business(request: Request, admin: PlatformAdmin = Depends(current_platform_admin), type: str = DEFAULT_TYPE):
    return _new_form(request, admin, new_form_defaults(type if type in TYPES else DEFAULT_TYPE), [])


@router.post("/businesses", dependencies=[Depends(check_csrf)])
async def create(request: Request, admin: PlatformAdmin = Depends(current_platform_admin)):
    form = await request.form()
    values = {k: str(v) for k, v in form.items() if k != "csrf"}
    data, errors = parse_form(values)
    created = None
    if data is not None:
        async with db_session(request) as db:
            try:
                created = await create_business(db, data)
            except OnboardingError as exc:
                errors = exc.errors
    if created is None:
        return _new_form(request, admin, values, errors, status_code=422)
    return await _detail(request, admin, created.business.id, created=created)


@router.get("/businesses/{business_id}")
async def business_detail(request: Request, business_id: int, admin: PlatformAdmin = Depends(current_platform_admin)):
    return await _detail(request, admin, business_id)


async def _detail(request: Request, admin: PlatformAdmin, business_id: int, created=None):
    async with db_session(request) as db:
        business = await db.get(Business, business_id)
        if business is None:
            return render(request, "platform/not_found.html", status_code=404, admin=admin)
        checklist = await setup_checklist(db, business)
        numbers = list(await db.scalars(select(PhoneNumber.e164).where(PhoneNumber.business_id == business.id)))
        owners = list(await db.scalars(select(StaffUser).where(StaffUser.business_id == business.id)))
        categories = list(await db.scalars(
            select(CatalogCategory).where(CatalogCategory.business_id == business.id).order_by(CatalogCategory.sort_order)))
        items = list(await db.scalars(
            select(CatalogItem).where(CatalogItem.business_id == business.id).order_by(CatalogItem.sort_order)))
        faqs = list(await db.scalars(select(Faq).where(Faq.business_id == business.id)))
    catalog = [(c, [i for i in items if i.category_id == c.id]) for c in categories]
    ready = all(c.done for c in checklist if c.required)
    return render(request, "platform/business_detail.html", admin=admin, b=business, btype=get_type(business.business_type),
                  checklist=checklist, ready=ready, numbers=numbers, owners=owners, catalog=catalog, faqs=faqs,
                  created=created, openai_project_id=request.app.state.services.settings.openai_project_id)


@router.get("/businesses/{business_id}/edit")
async def edit_business(request: Request, business_id: int, admin: PlatformAdmin = Depends(current_platform_admin)):
    async with db_session(request) as db:
        business = await db.get(Business, business_id)
        if business is None:
            return render(request, "platform/not_found.html", status_code=404, admin=admin)
        number = await db.scalar(select(PhoneNumber.e164).where(PhoneNumber.business_id == business.id))
    return render(request, "platform/business_edit.html", admin=admin, b=business,
                  v=business_form_values(business, number), errors=[], **form_options())


@router.post("/businesses/{business_id}/edit", dependencies=[Depends(check_csrf)])
async def save_business(request: Request, business_id: int, admin: PlatformAdmin = Depends(current_platform_admin)):
    form = await request.form()
    values = {k: str(v) for k, v in form.items() if k != "csrf"}
    async with db_session(request) as db:
        business = await db.get(Business, business_id)
        if business is None:
            return render(request, "platform/not_found.html", status_code=404, admin=admin)
        errors: list[str] = []
        fields = parse_business_fields(values, errors, platform=True, today=today_for(business))
        number = parse_phone(values.get("phone_number", ""), "VanakkamIQ phone number", errors, required=True)
        if not errors:
            errors = await update_business(db, business, fields, phone_number=number)
    if errors:
        return render(request, "platform/business_edit.html", status_code=422, admin=admin, b=business,
                      v=values, errors=errors, **form_options())
    return go(f"/platform/businesses/{business_id}")


@router.post("/businesses/{business_id}/status", dependencies=[Depends(check_csrf)])
async def set_status(request: Request, business_id: int, admin: PlatformAdmin = Depends(current_platform_admin)):
    form = await request.form()
    async with db_session(request) as db:
        business = await db.get(Business, business_id)
        if business is not None:
            business.is_active = form.get("active") == "1"
            await db.commit()
    return go(f"/platform/businesses/{business_id}")


@router.get("/businesses/{business_id}/calls")
async def business_calls(request: Request, business_id: int, admin: PlatformAdmin = Depends(current_platform_admin)):
    async with db_session(request) as db:
        business = await db.get(Business, business_id)
        if business is None:
            return render(request, "platform/not_found.html", status_code=404, admin=admin)
        rows = await recent_calls(db, business)
    return render(request, "platform/calls.html", admin=admin, b=business, rows=rows)


@router.get("/conversations/{conversation_id}")
async def conversation_detail(request: Request, conversation_id: int, admin: PlatformAdmin = Depends(current_platform_admin)):
    async with db_session(request) as db:
        convo = await db.get(Conversation, conversation_id)
        if convo is None:
            return render(request, "platform/not_found.html", status_code=404, admin=admin)
        business = await db.get(Business, convo.business_id)
        view = await conversation_view(db, convo, business, back_href=f"/platform/businesses/{business.id}/calls",
                                       back_label="Recent calls")
    return render(request, "platform/conversation.html", admin=admin, **view)


@router.get("/restaurants/{rest:path}")
async def old_restaurant_links(rest: str):
    """Links from before businesses of every kind were supported."""
    return go(f"/platform/businesses/{rest}")


# --- platform admins -----------------------------------------------------------------------
async def _admins_page(request: Request, admin: PlatformAdmin, status_code: int = 200, **context):
    async with db_session(request) as db:
        admins = list(await db.scalars(select(PlatformAdmin).order_by(PlatformAdmin.created_at)))
    context.setdefault("error", None)
    context.setdefault("added", None)
    context.setdefault("v", {})
    return render(request, "platform/admins.html", status_code=status_code, admin=admin, admins=admins, **context)


@router.get("/admins")
async def admins_page(request: Request, admin: PlatformAdmin = Depends(current_platform_admin)):
    return await _admins_page(request, admin)


@router.post("/admins", dependencies=[Depends(check_csrf)])
async def add_admin(request: Request, admin: PlatformAdmin = Depends(current_platform_admin)):
    form = await request.form()
    email = str(form.get("email") or "").strip().lower()
    name = str(form.get("name") or "").strip() or None
    error = None
    if "@" not in email or "." not in email.split("@")[-1]:
        error = "Enter a valid email address."
    async with db_session(request) as db:
        if error is None and await db.scalar(select(PlatformAdmin).where(func.lower(PlatformAdmin.email) == email)):
            error = f"{email} is already a platform admin."
        if error is None:
            db.add(PlatformAdmin(email=email, name=name))
            await db.commit()
    if error:
        return await _admins_page(request, admin, status_code=422, error=error, v={"email": email, "name": name or ""})
    return await _admins_page(request, admin, added=email)


@router.post("/admins/{admin_id}/delete", dependencies=[Depends(check_csrf)])
async def remove_admin(request: Request, admin_id: int, admin: PlatformAdmin = Depends(current_platform_admin)):
    if admin_id == admin.id:
        return await _admins_page(request, admin, status_code=422, error="You can't remove your own login.")
    async with db_session(request) as db:
        target = await db.get(PlatformAdmin, admin_id)
        if target is not None:
            await db.delete(target)
            await db.commit()
    return go("/platform/admins")


# --- usage ---------------------------------------------------------------------------------
@router.get("/usage")
async def usage_page(request: Request, admin: PlatformAdmin = Depends(current_platform_admin), days: int = 30):
    days = days if days in (7, 30, 90) else 30
    async with db_session(request) as db:
        rows = await usage_since(db, days)
    rate = request.app.state.services.settings.ai_cost_cents_per_minute
    totals = {
        "calls": sum(r.calls for r in rows), "minutes": sum(r.minutes for r in rows),
        "orders": sum(r.orders for r in rows), "order_cents": sum(r.order_cents for r in rows),
        "messages": sum(r.messages for r in rows), "cost_cents": sum(r.ai_cost_cents(rate) for r in rows),
    }
    return render(request, "platform/usage.html", admin=admin, rows=rows, totals=totals, days=days, rate=rate)
