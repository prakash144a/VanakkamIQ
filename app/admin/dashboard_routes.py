"""Business dashboard (/app): owners and staff see their own business only.

Every query here is scoped to the signed-in user's business_id. Pages that change
settings, the catalog or saved answers are owner-only; everyone can work the orders board,
mark items unavailable, handle callback messages and keep customer notes.
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import lazyload

from app.admin.auth import StaffSession, check_csrf, current_owner, current_staff, find_staff, log_in_staff
from app.admin.common import as_utc, conversation_view, db_session, local, recent_calls, render
from app.admin.login_flow import LoginScreen, check_code, screen, send_code
from app.business_types import get_type
from app.capabilities.ordering.orders import OPEN_STATUSES, StatusChangeError, change_status
from app.db.models import (
    Business, CatalogCategory, CatalogItem, Conversation, ConversationEvent, Customer, Faq, Handoff, Modifier, ModifierGroup,
    Order, PhoneNumber, StaffUser,
)
from app.onboarding.catalog_json import MAX_BYTES, catalog_to_json, parse_catalog_json, preview_changes
from app.onboarding.menu_import import (
    catalog_to_text, group_to_text, parse_menu_text, parse_money, parse_option_groups,
)
from app.onboarding.service import (
    business_form_values, form_options, parse_business_fields, replace_catalog, today_for, update_business,
)

router = APIRouter(prefix="/app")


def waiting_text(minutes: int) -> str:
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} min"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} h {minutes % 60} min" if minutes % 60 else f"{hours} h"
    return f"{hours // 24} day{'s' if hours >= 48 else ''}"


def go(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


async def nav_counts(db, business_id: int) -> dict:
    callbacks = await db.scalar(select(func.count()).select_from(Handoff).where(
        Handoff.business_id == business_id, Handoff.resolved_at.is_(None),
        Handoff.outcome.in_(("callback_requested", "failed"))))
    new_orders = await db.scalar(select(func.count()).select_from(Order).where(
        Order.business_id == business_id, Order.status == "confirmed"))
    live = await db.scalar(select(func.count()).select_from(Conversation).where(
        Conversation.business_id == business_id, Conversation.status == "active"))
    return {"callbacks": callbacks or 0, "new_orders": new_orders or 0, "live": live or 0}


async def page(request: Request, staff: StaffSession, template: str, active: str, status_code: int = 200, **context):
    async with db_session(request) as db:
        counts = await nav_counts(db, staff.business.id)
    return render(request, template, status_code=status_code, staff=staff, b=staff.business,
                  btype=get_type(staff.business.business_type), active=active, counts=counts, **context)


# --- sign in (emailed one-time code) --------------------------------------------------------
LOGIN = LoginScreen(audience="staff", action="/app/login", title="Your virtual front desk", home="/app",
                    find=find_staff, start=log_in_staff)


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
    return go("/app/login")


@router.get("")
async def home(staff: StaffSession = Depends(current_staff)):
    return go("/app/orders" if staff.business.takes_orders else "/app/calls")


# --- orders board --------------------------------------------------------------------------
async def _board(request: Request, staff: StaffSession) -> dict:
    business = staff.business
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    async with db_session(request) as db:
        open_orders = list(await db.scalars(
            select(Order).where(Order.business_id == business.id, Order.status.in_(OPEN_STATUSES))
            .order_by(Order.confirmed_at)))
        closed = list(await db.scalars(
            select(Order).where(Order.business_id == business.id, Order.status.in_(("completed", "cancelled")),
                                Order.status_updated_at >= since)
            .order_by(Order.status_updated_at.desc()).limit(20)))
    columns = [(s, [o for o in open_orders if o.status == s]) for s in OPEN_STATUSES]
    return {"columns": columns, "closed": closed,
            "times": {o.id: local(o.confirmed_at, business) for o in open_orders + closed}}


@router.get("/orders")
async def orders_page(request: Request, staff: StaffSession = Depends(current_staff)):
    if not staff.business.takes_orders:
        return await page(request, staff, "dashboard/module_off.html", "orders")
    return await page(request, staff, "dashboard/orders.html", "orders", **await _board(request, staff))


@router.get("/orders/board")
async def orders_board(request: Request, staff: StaffSession = Depends(current_staff)):
    """Polled every few seconds by the orders page (works across app replicas)."""
    return render(request, "dashboard/_board.html", b=staff.business, btype=get_type(staff.business.business_type),
                  **await _board(request, staff))


@router.post("/orders/{order_id}/status", dependencies=[Depends(check_csrf)])
async def order_status(request: Request, order_id: int, staff: StaffSession = Depends(current_staff)):
    form = await request.form()
    new_status = str(form.get("status") or "")
    notifier = request.app.state.services.notifier
    async with db_session(request) as db:
        order = await db.get(Order, order_id)
        if order is None or order.business_id != staff.business.id:
            return go("/app/orders")
        try:
            previous = change_status(order, new_status)
        except StatusChangeError:
            return go("/app/orders")
        await db.commit()
    # Text the customer only on real progress, not when a mistaken tap is undone.
    if new_status == "ready" and previous in ("confirmed", "in_progress"):
        await notifier.order_ready(staff.business, order)
    elif new_status == "cancelled" and previous in OPEN_STATUSES:
        await notifier.order_cancelled(staff.business, order)
    return go("/app/orders")


# --- calls ---------------------------------------------------------------------------------
@router.get("/calls")
async def calls_page(request: Request, staff: StaffSession = Depends(current_staff), show: str = ""):
    status = show if show in ("escalated", "abandoned", "completed") else None
    async with db_session(request) as db:
        rows = await recent_calls(db, staff.business, status=status)
        live = await recent_calls(db, staff.business, status="active", limit=10)
    return await page(request, staff, "dashboard/calls.html", "calls", rows=rows, live=live, show=status or "")


@router.get("/calls/live")
async def live_calls(request: Request, staff: StaffSession = Depends(current_staff)):
    async with db_session(request) as db:
        live = await recent_calls(db, staff.business, status="active", limit=10)
    return render(request, "dashboard/_live.html", live=live, b=staff.business, base="/app/calls")


async def _own_conversation(db, conversation_id: int, business_id: int) -> Conversation | None:
    convo = await db.get(Conversation, conversation_id)
    return convo if convo is not None and convo.business_id == business_id else None


@router.get("/calls/{conversation_id}")
async def call_detail(request: Request, conversation_id: int, staff: StaffSession = Depends(current_staff)):
    async with db_session(request) as db:
        convo = await _own_conversation(db, conversation_id, staff.business.id)
        if convo is None:
            return await page(request, staff, "dashboard/not_found.html", "calls", status_code=404)
        view = await conversation_view(db, convo, staff.business, back_href="/app/calls", back_label="Calls",
                                       events_url=f"/app/calls/{convo.id}/events")
    view.pop("b")  # page() supplies the business
    return await page(request, staff, "dashboard/conversation.html", "calls", **view)


@router.get("/calls/{conversation_id}/events")
async def call_events(request: Request, conversation_id: int, after: int = 0, staff: StaffSession = Depends(current_staff)):
    """New transcript lines for a live call (polled by the call page)."""
    async with db_session(request) as db:
        convo = await db.scalar(select(Conversation).options(lazyload(Conversation.events))
                                .where(Conversation.id == conversation_id, Conversation.business_id == staff.business.id))
        if convo is None:
            return HTMLResponse("", status_code=404)
        events = list(await db.scalars(select(ConversationEvent).where(
            ConversationEvent.conversation_id == conversation_id, ConversationEvent.id > after)
            .order_by(ConversationEvent.id)))
    response = render(request, "shared/_events.html", events=[{"e": e} for e in events])
    response.headers["X-Call-Status"] = convo.status
    return response


# --- callback messages ---------------------------------------------------------------------
@router.get("/callbacks")
async def callbacks_page(request: Request, staff: StaffSession = Depends(current_staff), show: str = "open"):
    done = show == "done"
    async with db_session(request) as db:
        query = select(Handoff).where(Handoff.business_id == staff.business.id,
                                      Handoff.outcome.in_(("callback_requested", "failed")))
        query = query.where(Handoff.resolved_at.is_not(None) if done else Handoff.resolved_at.is_(None))
        handoffs = list(await db.scalars(query.order_by(Handoff.created_at.desc()).limit(100)))
        callers = dict((await db.execute(select(Conversation.id, Conversation.caller_phone).where(
            Conversation.id.in_([h.conversation_id for h in handoffs] or [0])))).all())
    now = datetime.now(timezone.utc)
    rows = []
    for h in handoffs:
        minutes = max(0, int((now - as_utc(h.created_at)).total_seconds() // 60))
        rows.append({"h": h, "when": local(h.created_at, staff.business),
                     "phone": h.callback_phone or callers.get(h.conversation_id),
                     "done_when": local(h.resolved_at, staff.business), "waiting_minutes": minutes,
                     "waiting": waiting_text(minutes)})
    return await page(request, staff, "dashboard/callbacks.html", "callbacks", rows=rows, done=done)


@router.post("/callbacks/{handoff_id}/done", dependencies=[Depends(check_csrf)])
async def callback_done(request: Request, handoff_id: int, staff: StaffSession = Depends(current_staff)):
    form = await request.form()
    async with db_session(request) as db:
        handoff = await db.get(Handoff, handoff_id)
        if handoff is not None and handoff.business_id == staff.business.id:
            if form.get("undo"):
                handoff.resolved_at, handoff.resolved_by = None, None
            else:
                handoff.resolved_at, handoff.resolved_by = datetime.now(timezone.utc), staff.user.email
            await db.commit()
    return go("/app/callbacks" + ("?show=done" if form.get("undo") else ""))


# --- customers -----------------------------------------------------------------------------
@router.get("/customers")
async def customers_page(request: Request, staff: StaffSession = Depends(current_staff), q: str = ""):
    query = select(Customer).where(Customer.business_id == staff.business.id)
    if q.strip():
        term = f"%{q.strip().lower()}%"
        digits = "".join(ch for ch in q if ch.isdigit())
        conditions = [func.lower(Customer.name).like(term)]
        if digits:
            conditions.append(Customer.phone.like(f"%{digits}%"))
        query = query.where(or_(*conditions))
    async with db_session(request) as db:
        customers = list(await db.scalars(query.order_by(Customer.last_call_at.desc().nulls_last()).limit(100)))
    rows = [{"c": c, "last": local(c.last_call_at, staff.business)} for c in customers]
    return await page(request, staff, "dashboard/customers.html", "customers", rows=rows, q=q)


@router.get("/customers/{customer_id}")
async def customer_detail(request: Request, customer_id: int, staff: StaffSession = Depends(current_staff), saved: int = 0):
    async with db_session(request) as db:
        customer = await db.get(Customer, customer_id)
        if customer is None or customer.business_id != staff.business.id:
            return await page(request, staff, "dashboard/not_found.html", "customers", status_code=404)
        calls = list(await db.scalars(select(Conversation).options(lazyload(Conversation.events)).where(
            Conversation.business_id == staff.business.id, Conversation.caller_phone == customer.phone)
            .order_by(Conversation.started_at.desc()).limit(20)))
        orders = list(await db.scalars(select(Order).where(
            Order.business_id == staff.business.id, Order.customer_id == customer.id,
            Order.status.not_in(("draft",))).order_by(Order.confirmed_at.desc()).limit(20)))
    return await page(request, staff, "dashboard/customer.html", "customers", c=customer, saved=bool(saved),
                      calls=[{"c": c, "when": local(c.started_at, staff.business)} for c in calls],
                      orders=[{"o": o, "when": local(o.confirmed_at, staff.business)} for o in orders])


@router.post("/customers/{customer_id}", dependencies=[Depends(check_csrf)])
async def customer_save(request: Request, customer_id: int, staff: StaffSession = Depends(current_staff)):
    form = await request.form()
    async with db_session(request) as db:
        customer = await db.get(Customer, customer_id)
        if customer is not None and customer.business_id == staff.business.id:
            customer.name = str(form.get("name") or "").strip()[:120] or None
            customer.notes = str(form.get("notes") or "").strip()[:2000] or None
            await db.commit()
    return go(f"/app/customers/{customer_id}?saved=1")


# --- catalog -------------------------------------------------------------------------------
async def _catalog(db, business_id: int):
    categories = list(await db.scalars(select(CatalogCategory).where(CatalogCategory.business_id == business_id)
                                       .order_by(CatalogCategory.sort_order, CatalogCategory.id)))
    items = list(await db.scalars(select(CatalogItem).where(CatalogItem.business_id == business_id)
                                  .order_by(CatalogItem.sort_order, CatalogItem.id)))
    return categories, items


@router.get("/catalog")
async def catalog_page(request: Request, staff: StaffSession = Depends(current_staff), saved: str = ""):
    if not staff.business.takes_orders:
        return await page(request, staff, "dashboard/module_off.html", "catalog")
    async with db_session(request) as db:
        categories, items = await _catalog(db, staff.business.id)
    sections = [(c, [i for i in items if i.category_id == c.id]) for c in categories]
    loose = [i for i in items if i.category_id is None]
    if loose:
        sections.insert(0, (None, loose))
    return await page(request, staff, "dashboard/catalog.html", "catalog", sections=sections,
                      item_count=len(items), saved=saved)


@router.post("/catalog/items/{item_id}/availability", dependencies=[Depends(check_csrf)])
async def item_availability(request: Request, item_id: int, staff: StaffSession = Depends(current_staff)):
    form = await request.form()
    async with db_session(request) as db:
        item = await db.get(CatalogItem, item_id)
        if item is not None and item.business_id == staff.business.id:
            item.is_available = form.get("available") == "1"
            await db.commit()
    return go(f"/app/catalog#item-{item_id}")


def _item_form_values(item: CatalogItem | None) -> dict:
    if item is None:
        return {"available": "on"}
    return {
        "name": item.name, "price": f"{item.price_cents / 100:.2f}", "category_id": str(item.category_id or ""),
        "description": item.description or "", "tags": ", ".join(item.tags or []),
        "aliases": ", ".join(item.aliases or []), "available": "on" if item.is_available else "",
        "external_id": item.external_id or "",
        "options": "\n".join(group_to_text(g) for g in item.modifier_groups),
    }


async def _item_form(request: Request, staff: StaffSession, item: CatalogItem | None, v: dict, errors: list[str], status_code=200):
    async with db_session(request) as db:
        categories, _ = await _catalog(db, staff.business.id)
    return await page(request, staff, "dashboard/item_form.html", "catalog", status_code=status_code,
                      item=item, v=v, errors=errors, categories=categories)


@router.get("/catalog/items/new")
async def new_item(request: Request, staff: StaffSession = Depends(current_owner)):
    return await _item_form(request, staff, None, _item_form_values(None), [])


@router.get("/catalog/items/{item_id}")
async def edit_item(request: Request, item_id: int, staff: StaffSession = Depends(current_owner)):
    async with db_session(request) as db:
        item = await db.get(CatalogItem, item_id)
    if item is None or item.business_id != staff.business.id:
        return await page(request, staff, "dashboard/not_found.html", "catalog", status_code=404)
    return await _item_form(request, staff, item, _item_form_values(item), [])


@router.post("/catalog/items", dependencies=[Depends(check_csrf)])
async def create_item(request: Request, staff: StaffSession = Depends(current_owner)):
    return await _save_item(request, staff, None)


@router.post("/catalog/items/{item_id}", dependencies=[Depends(check_csrf)])
async def update_item(request: Request, item_id: int, staff: StaffSession = Depends(current_owner)):
    return await _save_item(request, staff, item_id)


def _csv(text: str, lower: bool = False) -> list[str]:
    values = [v.strip() for v in text.split(",") if v.strip()]
    return [v.lower() for v in values] if lower else values


async def _save_item(request: Request, staff: StaffSession, item_id: int | None):
    form = await request.form()
    v = {k: str(form.get(k) or "") for k in ("name", "price", "category_id", "new_category", "description",
                                             "tags", "aliases", "options", "available", "external_id")}
    errors: list[str] = []
    name = v["name"].strip()
    if not name:
        errors.append("Name is required.")
    price = parse_money(v["price"]) if v["price"].strip() else None
    if price is None:
        errors.append("Price should be an amount like 12.99.")
    groups, group_errors = parse_option_groups(v["options"])
    errors += group_errors

    async with db_session(request) as db:
        business_id = staff.business.id
        item = None
        if item_id is not None:
            item = await db.get(CatalogItem, item_id)
            if item is None or item.business_id != business_id:
                return go("/app/catalog")
        duplicate = await db.scalar(select(CatalogItem.id).where(
            CatalogItem.business_id == business_id, func.lower(CatalogItem.name) == name.lower(),
            CatalogItem.id != (item_id or 0)))
        if name and duplicate:
            errors.append(f"\"{name}\" is already in the list.")
        category_id = None
        if v["new_category"].strip():
            category_id = "new"
        elif v["category_id"]:
            category = await db.get(CatalogCategory, int(v["category_id"])) if v["category_id"].isdigit() else None
            if category is None or category.business_id != business_id:
                errors.append("Choose a section from the list.")
            else:
                category_id = category.id
        if errors:
            return await _item_form(request, staff, item, v, errors, status_code=422)

        if category_id == "new":
            count = await db.scalar(select(func.count()).select_from(CatalogCategory).where(
                CatalogCategory.business_id == business_id))
            category = CatalogCategory(business_id=business_id, name=v["new_category"].strip()[:120], sort_order=count)
            db.add(category)
            await db.flush()
            category_id = category.id
        if item is None:
            last = await db.scalar(select(func.max(CatalogItem.sort_order)).where(CatalogItem.business_id == business_id))
            item = CatalogItem(business_id=business_id, sort_order=(last or 0) + 1, modifier_groups=[])
            db.add(item)
        item.name, item.price_cents, item.category_id = name[:160], price, category_id
        item.description = v["description"].strip() or None
        item.external_id = v["external_id"].strip()[:80] or None
        item.tags, item.aliases = _csv(v["tags"], lower=True), _csv(v["aliases"])
        item.is_available = bool(v["available"])
        item.modifier_groups = [
            ModifierGroup(name=g.name, min_select=g.min_select, max_select=g.max_select,
                          modifiers=[Modifier(name=o, price_delta_cents=cents) for o, cents, _ in g.options])
            for g in groups
        ]
        await db.commit()
        saved_id = item.id
    return go(f"/app/catalog?saved={saved_id}#item-{saved_id}")


@router.post("/catalog/items/{item_id}/delete", dependencies=[Depends(check_csrf)])
async def delete_item(request: Request, item_id: int, staff: StaffSession = Depends(current_owner)):
    async with db_session(request) as db:
        item = await db.get(CatalogItem, item_id)
        if item is not None and item.business_id == staff.business.id:
            await db.delete(item)
            await db.commit()
    return go("/app/catalog")


@router.get("/catalog/text")
async def catalog_text_page(request: Request, staff: StaffSession = Depends(current_owner)):
    async with db_session(request) as db:
        categories, items = await _catalog(db, staff.business.id)
    return await page(request, staff, "dashboard/catalog_text.html", "catalog",
                      text=catalog_to_text(categories, items), errors=[])


@router.post("/catalog/text", dependencies=[Depends(check_csrf)])
async def catalog_text_save(request: Request, staff: StaffSession = Depends(current_owner)):
    form = await request.form()
    text = str(form.get("catalog_text") or "")
    catalog, errors = parse_menu_text(text)
    if not errors and not catalog:
        errors = ["The list is empty. To remove every item, delete them one by one instead."]
    if errors:
        return await page(request, staff, "dashboard/catalog_text.html", "catalog", status_code=422, text=text, errors=errors)
    async with db_session(request) as db:
        count = await replace_catalog(db, staff.business.id, catalog)
        await db.commit()
    return go(f"/app/catalog?saved=all{count}")


@router.get("/catalog/export.json")
async def catalog_export(request: Request, staff: StaffSession = Depends(current_staff)):
    async with db_session(request) as db:
        categories, items = await _catalog(db, staff.business.id)
    filename = f"{staff.business.slug}-{get_type(staff.business.business_type).catalog.lower()}.json"
    return JSONResponse(catalog_to_json(categories, items),
                        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"})


@router.get("/catalog/upload")
async def catalog_upload_page(request: Request, staff: StaffSession = Depends(current_owner)):
    return await page(request, staff, "dashboard/catalog_upload.html", "catalog", errors=[], preview=None)


async def _upload_text(form) -> tuple[str | None, list[str]]:
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read") or not upload.filename:
        return None, ["Choose a .json file to upload."]
    raw = await upload.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        return None, [f"The file is too large (over {MAX_BYTES // 1_000_000} MB)."]
    try:
        return raw.decode("utf-8-sig"), []
    except UnicodeDecodeError:
        return None, ["The file isn't UTF-8 text. Save it as UTF-8 JSON and try again."]


@router.post("/catalog/upload", dependencies=[Depends(check_csrf)])
async def catalog_upload_preview(request: Request, staff: StaffSession = Depends(current_owner)):
    """Step 1: check the file and show what would change. Nothing is saved yet."""
    text, errors = await _upload_text(await request.form())
    catalog = []
    if text is not None:
        catalog, errors = parse_catalog_json(text)
    if errors:
        return await page(request, staff, "dashboard/catalog_upload.html", "catalog", status_code=422,
                          errors=errors, preview=None)
    async with db_session(request) as db:
        _, current = await _catalog(db, staff.business.id)
    return await page(request, staff, "dashboard/catalog_upload.html", "catalog", errors=[],
                      preview=preview_changes(current, catalog), catalog_json=text)


@router.post("/catalog/upload/confirm", dependencies=[Depends(check_csrf)])
async def catalog_upload_confirm(request: Request, staff: StaffSession = Depends(current_owner)):
    """Step 2: the owner confirmed the preview; the file becomes the whole catalog."""
    form = await request.form()
    catalog, errors = parse_catalog_json(str(form.get("catalog_json") or ""))
    if errors:
        return await page(request, staff, "dashboard/catalog_upload.html", "catalog", status_code=422,
                          errors=errors, preview=None)
    async with db_session(request) as db:
        count = await replace_catalog(db, staff.business.id, catalog)
        await db.commit()
    return go(f"/app/catalog?saved=all{count}")


# --- saved answers -------------------------------------------------------------------------
@router.get("/faqs")
async def faqs_page(request: Request, staff: StaffSession = Depends(current_staff), edit: int = 0):
    async with db_session(request) as db:
        faqs = list(await db.scalars(select(Faq).where(Faq.business_id == staff.business.id).order_by(Faq.id)))
    return await page(request, staff, "dashboard/faqs.html", "faqs", faqs=faqs, edit=edit, error=None, v={})


def _faq_values(form) -> tuple[str, str, str | None]:
    question, answer = str(form.get("question") or "").strip(), str(form.get("answer") or "").strip()
    error = None if question and answer else "Enter both the question and the answer."
    return question[:500], answer[:2000], error


@router.post("/faqs", dependencies=[Depends(check_csrf)])
async def add_faq(request: Request, staff: StaffSession = Depends(current_owner)):
    question, answer, error = _faq_values(await request.form())
    async with db_session(request) as db:
        if error is None:
            db.add(Faq(business_id=staff.business.id, question=question, answer=answer))
            await db.commit()
            return go("/app/faqs#faq-new")
        faqs = list(await db.scalars(select(Faq).where(Faq.business_id == staff.business.id).order_by(Faq.id)))
    return await page(request, staff, "dashboard/faqs.html", "faqs", status_code=422, faqs=faqs, edit=0,
                      error=error, v={"question": question, "answer": answer})


@router.post("/faqs/{faq_id}", dependencies=[Depends(check_csrf)])
async def edit_faq(request: Request, faq_id: int, staff: StaffSession = Depends(current_owner)):
    question, answer, error = _faq_values(await request.form())
    async with db_session(request) as db:
        faq = await db.get(Faq, faq_id)
        if faq is not None and faq.business_id == staff.business.id and error is None:
            faq.question, faq.answer = question, answer
            await db.commit()
    return go(f"/app/faqs#faq-{faq_id}")


@router.post("/faqs/{faq_id}/delete", dependencies=[Depends(check_csrf)])
async def delete_faq(request: Request, faq_id: int, staff: StaffSession = Depends(current_owner)):
    async with db_session(request) as db:
        faq = await db.get(Faq, faq_id)
        if faq is not None and faq.business_id == staff.business.id:
            await db.delete(faq)
            await db.commit()
    return go("/app/faqs")


# --- team (the owner manages who can sign in) ----------------------------------------------
ROLES = {"owner": "Owner", "staff": "Staff"}


async def _team_page(request: Request, staff: StaffSession, status_code: int = 200, **context):
    async with db_session(request) as db:
        members = list(await db.scalars(select(StaffUser).where(StaffUser.business_id == staff.business.id)
                                        .order_by(StaffUser.role, StaffUser.email)))
    rows = [{"u": u, "last": local(u.last_login_at, staff.business)} for u in members]
    context.setdefault("error", None)
    context.setdefault("added", None)
    context.setdefault("v", {})
    return await page(request, staff, "dashboard/team.html", "team", status_code=status_code, rows=rows,
                      roles=ROLES, **context)


@router.get("/team")
async def team_page(request: Request, staff: StaffSession = Depends(current_owner)):
    return await _team_page(request, staff)


@router.post("/team", dependencies=[Depends(check_csrf)])
async def team_add(request: Request, staff: StaffSession = Depends(current_owner)):
    form = await request.form()
    email = str(form.get("email") or "").strip().lower()
    name = str(form.get("name") or "").strip()[:120] or None
    role = str(form.get("role") or "staff")
    error = None
    if "@" not in email or "." not in email.split("@")[-1] or len(email) > 254:
        error = "Enter a valid email address."
    elif role not in ROLES:
        error = "Choose a role from the list."
    async with db_session(request) as db:
        if error is None and await find_staff(db, email) is not None:
            error = f"{email} already has a VanakkamIQ login."
        if error is None:
            db.add(StaffUser(business_id=staff.business.id, email=email, name=name, role=role))
            await db.commit()
    if error:
        return await _team_page(request, staff, status_code=422, error=error,
                                v={"email": email, "name": name or "", "role": role})
    return await _team_page(request, staff, added=email)


async def _owner_count(db, business_id: int) -> int:
    return await db.scalar(select(func.count()).select_from(StaffUser).where(
        StaffUser.business_id == business_id, StaffUser.role == "owner")) or 0


@router.post("/team/{user_id}/role", dependencies=[Depends(check_csrf)])
async def team_role(request: Request, user_id: int, staff: StaffSession = Depends(current_owner)):
    form = await request.form()
    role = str(form.get("role") or "")
    async with db_session(request) as db:
        user = await db.get(StaffUser, user_id)
        if user is None or user.business_id != staff.business.id or role not in ROLES:
            return go("/app/team")
        if user.role == "owner" and role != "owner" and await _owner_count(db, staff.business.id) <= 1:
            return await _team_page(request, staff, status_code=422,
                                    error="The business needs at least one owner. Make someone else an owner first.")
        user.role = role
        await db.commit()
    # Owners who make themselves staff lose access to this page.
    return go("/app" if user_id == staff.user.id and role != "owner" else "/app/team")


@router.post("/team/{user_id}/delete", dependencies=[Depends(check_csrf)])
async def team_remove(request: Request, user_id: int, staff: StaffSession = Depends(current_owner)):
    if user_id == staff.user.id:
        return await _team_page(request, staff, status_code=422, error="You can't remove your own login.")
    async with db_session(request) as db:
        user = await db.get(StaffUser, user_id)
        if user is not None and user.business_id == staff.business.id:
            await db.delete(user)
            await db.commit()
    return go("/app/team")


# --- settings ------------------------------------------------------------------------------
async def _number(db, business_id: int) -> str | None:
    return await db.scalar(select(PhoneNumber.e164).where(PhoneNumber.business_id == business_id))


@router.get("/settings")
async def settings_page(request: Request, staff: StaffSession = Depends(current_owner), saved: int = 0):
    async with db_session(request) as db:
        number = await _number(db, staff.business.id)
    return await page(request, staff, "dashboard/settings.html", "settings", v=business_form_values(staff.business, number),
                      errors=[], saved=bool(saved), number=number, **form_options())


@router.post("/settings", dependencies=[Depends(check_csrf)])
async def settings_save(request: Request, staff: StaffSession = Depends(current_owner)):
    form = await request.form()
    values = {k: str(v) for k, v in form.items() if k != "csrf"}
    errors: list[str] = []
    fields = parse_business_fields(values, errors, platform=False, takes_orders=staff.business.takes_orders,
                                   today=today_for(staff.business))
    async with db_session(request) as db:
        number = await _number(db, staff.business.id)
        if not errors:
            business = await db.get(Business, staff.business.id)
            errors = await update_business(db, business, fields)
    if errors:
        return await page(request, staff, "dashboard/settings.html", "settings", status_code=422, v=values,
                          errors=errors, saved=False, number=number, **form_options())
    return go("/app/settings?saved=1")
