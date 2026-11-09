"""Add a restaurant: check the admin form, then create everything in one transaction."""

import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.business_hours import DAYS
from app.db.models import (
    Business, Faq, MenuCategory, MenuItem, Modifier, ModifierGroup, PhoneNumber, StaffUser,
)
from app.onboarding.menu_import import ParsedCategory, parse_faq_text, parse_menu_text, parse_money
from app.security import hash_password
from app.voice.sip_headers import normalize_e164

TIMEZONES = [
    ("America/New_York", "Eastern"),
    ("America/Chicago", "Central"),
    ("America/Denver", "Mountain"),
    ("America/Phoenix", "Arizona"),
    ("America/Los_Angeles", "Pacific"),
    ("America/Anchorage", "Alaska"),
    ("Pacific/Honolulu", "Hawaii"),
    ("America/Toronto", "Toronto"),
    ("America/Vancouver", "Vancouver"),
    ("America/Halifax", "Atlantic"),
]
VOICES = ["marin", "cedar", "coral", "sage", "shimmer", "verse", "ballad", "alloy", "ash", "echo"]
LANGUAGES = [("en-US", "English (US)"), ("en-CA", "English (Canada)"), ("fr-CA", "French (Canada)"), ("es-US", "Spanish (US)")]
MIN_PASSWORD = 10

_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE = re.compile(r"^\+?[\d\s\-().]{10,20}$")


class OnboardingError(Exception):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass
class RestaurantInput:
    name: str
    address: str | None
    timezone: str
    language: str
    voice: str
    persona: str | None
    hours: dict
    offers_pickup: bool
    offers_delivery: bool
    tax_rate_bps: int
    large_order_cents: int | None
    phone_number: str
    handoff_phone: str | None
    notify_phone: str | None
    owner_name: str | None
    owner_email: str
    owner_password: str | None  # None = generate a temporary one
    menu: list[ParsedCategory] = field(default_factory=list)
    faqs: list[tuple[str, str]] = field(default_factory=list)
    is_active: bool = True


@dataclass
class CreatedRestaurant:
    business: Business
    owner_email: str
    temporary_password: str | None
    item_count: int


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:80] or "restaurant"


def _phone(value: str, label: str, errors: list[str], required: bool = False) -> str | None:
    value = (value or "").strip()
    if not value:
        if required:
            errors.append(f"{label} is required.")
        return None
    if not _PHONE.match(value):
        errors.append(f"{label} \"{value}\" doesn't look like a phone number.")
        return None
    number = normalize_e164(value)
    if not 11 <= len(number) <= 16:
        errors.append(f"{label} \"{value}\" doesn't look like a full phone number with area code.")
        return None
    return number


def parse_form(form: Mapping[str, str]) -> tuple[RestaurantInput | None, list[str]]:
    """Turn the submitted form into a RestaurantInput, collecting every problem at once."""
    errors: list[str] = []
    get = lambda key: (form.get(key) or "").strip()  # noqa: E731

    name = get("name")
    if not name:
        errors.append("Restaurant name is required.")

    timezone = get("timezone") or "America/New_York"
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        errors.append(f"Unknown timezone \"{timezone}\".")

    voice = get("voice") or "marin"
    if voice not in VOICES:
        errors.append(f"Unknown voice \"{voice}\".")
    language = get("language") or "en-US"

    hours: dict = {}
    for day in DAYS:
        if form.get(f"closed_{day}"):
            continue
        start, end = get(f"open_{day}"), get(f"close_{day}")
        if not (_TIME.match(start) and _TIME.match(end)):
            errors.append(f"Hours for {day.capitalize()}: enter opening and closing times, or tick Closed.")
            continue
        if start == end:
            errors.append(f"Hours for {day.capitalize()}: opening and closing times are the same.")
            continue
        hours[day] = [[start, end]]
    if not hours:
        errors.append("The restaurant needs to be open at least one day.")

    offers_pickup = bool(form.get("offers_pickup"))
    offers_delivery = bool(form.get("offers_delivery"))
    if not (offers_pickup or offers_delivery):
        errors.append("Choose pickup, delivery, or both.")

    tax_rate_bps = 0
    if get("tax_rate"):
        try:
            tax = float(get("tax_rate").rstrip("%"))
            if not 0 <= tax <= 25:
                raise ValueError
            tax_rate_bps = round(tax * 100)
        except ValueError:
            errors.append("Tax rate should be a percentage between 0 and 25, like 8.875.")

    large_order_cents = None
    if get("large_order"):
        large_order_cents = parse_money(get("large_order"))
        if large_order_cents is None:
            errors.append("Large-order amount should be a dollar amount, like 250.")

    phone_number = _phone(get("phone_number"), "VanakkamIQ phone number", errors, required=True)
    handoff_phone = _phone(get("handoff_phone"), "Staff transfer number", errors)
    notify_phone = _phone(get("notify_phone"), "Staff alert number", errors)

    owner_email = get("owner_email").lower()
    if not _EMAIL.match(owner_email):
        errors.append("Owner email is required and must be a valid address.")
    owner_password = get("owner_password") or None
    if owner_password and len(owner_password) < MIN_PASSWORD:
        errors.append(f"Owner password must be at least {MIN_PASSWORD} characters (or leave it blank to generate one).")

    menu, menu_errors = parse_menu_text(form.get("menu_text") or "")
    errors += menu_errors
    faqs, faq_errors = parse_faq_text(form.get("faq_text") or "")
    errors += faq_errors

    if errors:
        return None, errors
    return RestaurantInput(
        name=name, address=get("address") or None, timezone=timezone, language=language, voice=voice,
        persona=get("persona") or None, hours=hours, offers_pickup=offers_pickup,
        offers_delivery=offers_delivery, tax_rate_bps=tax_rate_bps, large_order_cents=large_order_cents,
        phone_number=phone_number, handoff_phone=handoff_phone, notify_phone=notify_phone,
        owner_name=get("owner_name") or None, owner_email=owner_email, owner_password=owner_password,
        menu=menu, faqs=faqs, is_active=bool(form.get("is_active")),
    ), []


async def _unique_slug(db: AsyncSession, name: str) -> str:
    base = slugify(name)
    slug, n = base, 2
    while await db.scalar(select(func.count()).select_from(Business).where(Business.slug == slug)):
        slug, n = f"{base}-{n}", n + 1
    return slug


async def create_restaurant(db: AsyncSession, data: RestaurantInput) -> CreatedRestaurant:
    errors: list[str] = []
    taken = await db.scalar(select(PhoneNumber).where(PhoneNumber.e164 == data.phone_number))
    if taken is not None:
        owner = await db.get(Business, taken.business_id)
        errors.append(f"{data.phone_number} is already assigned to {owner.name if owner else 'another restaurant'}.")
    if await db.scalar(select(StaffUser).where(func.lower(StaffUser.email) == data.owner_email)):
        errors.append(f"{data.owner_email} already has a VanakkamIQ login.")
    if errors:
        raise OnboardingError(errors)

    business = Business(
        name=data.name, slug=await _unique_slug(db, data.name), vertical="restaurant",
        timezone=data.timezone, address=data.address, hours=data.hours, persona=data.persona,
        language=data.language, voice=data.voice, offers_pickup=data.offers_pickup,
        offers_delivery=data.offers_delivery, tax_rate_bps=data.tax_rate_bps,
        large_order_cents=data.large_order_cents, handoff_phone=data.handoff_phone,
        notify_phone=data.notify_phone, sms_from=data.phone_number, is_active=data.is_active,
    )
    db.add(business)
    await db.flush()
    db.add(PhoneNumber(business_id=business.id, e164=data.phone_number, carrier="telnyx"))

    temporary = None if data.owner_password else secrets.token_urlsafe(12)
    db.add(StaffUser(
        business_id=business.id, email=data.owner_email, name=data.owner_name, role="owner",
        password_hash=hash_password(data.owner_password or temporary),
    ))

    item_count = 0
    for c_index, parsed in enumerate(data.menu):
        category = MenuCategory(business_id=business.id, name=parsed.name, sort_order=c_index)
        db.add(category)
        await db.flush()
        for i_index, p in enumerate(parsed.items):
            db.add(MenuItem(
                business_id=business.id, category_id=category.id, name=p.name, price_cents=p.price_cents,
                aliases=p.aliases, tags=p.tags, sort_order=i_index,
                modifier_groups=[
                    ModifierGroup(
                        name=g.name, min_select=g.min_select, max_select=g.max_select,
                        modifiers=[Modifier(name=o, price_delta_cents=cents) for o, cents in g.options],
                    )
                    for g in p.groups
                ],
            ))
            item_count += 1

    for question, answer in data.faqs:
        db.add(Faq(business_id=business.id, question=question, answer=answer))

    await db.commit()
    return CreatedRestaurant(business, data.owner_email, temporary, item_count)


@dataclass
class SetupCheck:
    label: str
    done: bool
    detail: str
    required: bool = True


async def setup_checklist(db: AsyncSession, business: Business) -> list[SetupCheck]:
    numbers = list(await db.scalars(select(PhoneNumber.e164).where(PhoneNumber.business_id == business.id)))
    items = await db.scalar(select(func.count()).select_from(MenuItem).where(MenuItem.business_id == business.id))
    faqs = await db.scalar(select(func.count()).select_from(Faq).where(Faq.business_id == business.id))
    owners = await db.scalar(select(func.count()).select_from(StaffUser).where(StaffUser.business_id == business.id))
    return [
        SetupCheck("Phone number", bool(numbers), ", ".join(numbers) or "No number assigned"),
        SetupCheck("Menu", bool(items), f"{items} item(s)" if items else "No menu items yet; the AI can't take orders"),
        SetupCheck("Opening hours", bool(business.hours), "Set" if business.hours else "Not set"),
        SetupCheck("Owner login", bool(owners), f"{owners} account(s)"),
        SetupCheck("FAQs", bool(faqs), f"{faqs} saved answer(s)" if faqs else "None yet (recommended)", required=False),
        SetupCheck("Staff transfer number", bool(business.handoff_phone),
                   business.handoff_phone or "Not set: callers who need a person can only leave a message",
                   required=False),
        SetupCheck("Staff alert number", bool(business.notify_phone),
                   business.notify_phone or "Not set: no SMS alerts for new orders", required=False),
    ]
