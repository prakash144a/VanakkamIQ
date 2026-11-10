"""Business forms: add a business (platform admin) and edit its settings (platform admin or owner).

Both share one set of fields and checks, so a value accepted on one screen is accepted on the other.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.business_hours import DAYS, local_now
from app.business_types import DEFAULT_TYPE, TYPES, get_type
from app.db.models import (
    Business, CatalogCategory, CatalogItem, Faq, Modifier, ModifierGroup, PhoneNumber, StaffUser,
)
from app.onboarding.menu_import import ParsedCategory, parse_faq_text, parse_menu_text, parse_money
from app.voice.sip_headers import normalize_e164

# Shown first in the timezone picker; every IANA zone is listed after these.
COMMON_TIMEZONES = [
    ("America/New_York", "US Eastern"),
    ("America/Chicago", "US Central"),
    ("America/Denver", "US Mountain"),
    ("America/Phoenix", "Arizona"),
    ("America/Los_Angeles", "US Pacific"),
    ("America/Anchorage", "Alaska"),
    ("Pacific/Honolulu", "Hawaii"),
    ("America/Toronto", "Toronto"),
    ("America/Vancouver", "Vancouver"),
    ("America/Halifax", "Atlantic (Canada)"),
    ("America/St_Johns", "Newfoundland"),
    ("Europe/London", "UK"),
    ("Asia/Kolkata", "India"),
    ("Asia/Singapore", "Singapore"),
    ("Australia/Sydney", "Sydney"),
]


def _all_timezones() -> list[str]:
    """Every region/city zone (no Etc/ aliases or legacy names), sorted."""
    return sorted(z for z in available_timezones()
                  if "/" in z and not z.startswith(("Etc/", "SystemV/", "US/", "posix/", "right/")))


ALL_TIMEZONES = _all_timezones()


def utc_offset(tz: str) -> str:
    offset = datetime.now(ZoneInfo(tz)).utcoffset() or timedelta(0)
    minutes = int(offset.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "−"
    return f"UTC{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"


def timezone_choices() -> dict:
    return {
        "common": [(tz, f"{label} ({utc_offset(tz)})") for tz, label in COMMON_TIMEZONES],
        "all": [(tz, f"{tz.replace('_', ' ')} ({utc_offset(tz)})") for tz in ALL_TIMEZONES],
    }
VOICES = ["marin", "cedar", "coral", "sage", "shimmer", "verse", "ballad", "alloy", "ash", "echo"]
LANGUAGES = [("en-US", "English (US)"), ("en-CA", "English (Canada)"), ("fr-CA", "French (Canada)"), ("es-US", "Spanish (US)")]
RETENTION_CHOICES = [("", "Always (never delete)"), ("30", "30 days"), ("90", "90 days"), ("180", "6 months"), ("365", "1 year")]

_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE = re.compile(r"^\+?[\d\s\-().]{10,20}$")


class OnboardingError(Exception):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


def form_options() -> dict:
    """Choices every business form template needs."""
    return {"timezones": timezone_choices(), "voices": VOICES, "languages": LANGUAGES,
            "retention_choices": RETENTION_CHOICES, "types": list(TYPES.values())}


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:80] or "business"


def parse_phone(value: str, label: str, errors: list[str], required: bool = False) -> str | None:
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


def parse_closures(text: str, errors: list[str], today: date | None = None) -> list[dict]:
    """One per line: '2026-12-25 Christmas Day'. Past dates are dropped."""
    closures: dict[str, dict] = {}
    for line_no, raw in enumerate((text or "").splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        day_text, _, note = line.partition(" ")
        try:
            day = date.fromisoformat(day_text)
        except ValueError:
            errors.append(f"Closed dates, line {line_no}: start with a date like 2026-12-25.")
            continue
        if today is None or day >= today:
            closures[day.isoformat()] = {"date": day.isoformat(), "note": note.strip()[:80]}
    return [closures[k] for k in sorted(closures)]


def closures_to_text(closures: list[dict] | None) -> str:
    return "\n".join(f"{c['date']} {c.get('note', '')}".strip() for c in closures or [])


def parse_business_fields(
    form: Mapping[str, str], errors: list[str], *, platform: bool, takes_orders: bool = False, today: date | None = None,
) -> dict:
    """Settings shared by every business form. Platform-only fields (type, ordering module)
    are read only when `platform` is true; otherwise `takes_orders` says whether ordering is on."""
    get = lambda key: (form.get(key) or "").strip()  # noqa: E731
    values: dict = {}

    values["name"] = get("name")
    if not values["name"]:
        errors.append("Business name is required.")
    values["description"] = get("description") or None
    values["address"] = get("address") or None

    tz = get("timezone") or "America/New_York"
    try:
        ZoneInfo(tz)
        if "/" not in tz:
            raise ValueError
    except (ZoneInfoNotFoundError, ValueError):
        errors.append(f"Unknown timezone \"{tz}\". Pick one from the list.")
    values["timezone"] = tz

    if platform:
        business_type = get("business_type") or DEFAULT_TYPE
        if business_type not in TYPES:
            errors.append(f"Unknown business type \"{business_type}\".")
        values["business_type"] = business_type
        values["takes_orders"] = bool(form.get("takes_orders"))

    voice = get("voice") or "marin"
    if voice not in VOICES:
        errors.append(f"Unknown voice \"{voice}\".")
    values["voice"] = voice
    language = get("language") or "en-US"
    if language not in dict(LANGUAGES):
        errors.append(f"Unknown language \"{language}\".")
    values["language"] = language
    values["persona"] = get("persona") or None
    values["greeting"] = get("greeting")[:300] or None

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
        errors.append("The business needs to be open at least one day.")
    values["hours"] = hours
    values["closures"] = parse_closures(form.get("closures_text") or "", errors, today)

    # Ordering details: checked only when the ordering module is (or will be) on.
    values["offers_pickup"] = bool(form.get("offers_pickup"))
    values["offers_delivery"] = bool(form.get("offers_delivery"))
    ordering_on = values["takes_orders"] if platform else takes_orders
    if ordering_on and not (values["offers_pickup"] or values["offers_delivery"]):
        errors.append("Choose pickup, delivery, or both.")
    if not ordering_on and not (values["offers_pickup"] or values["offers_delivery"]):
        values["offers_pickup"] = True

    values["tax_rate_bps"] = 0
    if get("tax_rate"):
        try:
            tax = float(get("tax_rate").rstrip("%"))
            if not 0 <= tax <= 25:
                raise ValueError
            values["tax_rate_bps"] = round(tax * 100)
        except ValueError:
            errors.append("Tax rate should be a percentage between 0 and 25, like 8.875.")

    values["large_order_cents"] = None
    if get("large_order"):
        values["large_order_cents"] = parse_money(get("large_order"))
        if values["large_order_cents"] is None:
            errors.append("Large-order amount should be a dollar amount, like 250.")

    values["transfers_enabled"] = bool(form.get("transfers_enabled"))
    values["handoff_phone"] = parse_phone(get("handoff_phone"), "Staff transfer number", errors)
    values["notify_phone"] = parse_phone(get("notify_phone"), "Staff alert number", errors)

    retention = get("retention_days")
    if retention and retention not in dict(RETENTION_CHOICES):
        errors.append("Choose how long to keep call transcripts from the list.")
        retention = ""
    values["transcript_retention_days"] = int(retention) if retention else None
    if not platform and not takes_orders:
        # The owner's settings page has no ordering fields then; keep what's saved.
        for key in ("offers_pickup", "offers_delivery", "tax_rate_bps", "large_order_cents"):
            values.pop(key)
    return values


def business_form_values(business: Business, phone_number: str | None = None) -> dict:
    """The current settings as form values (strings), for edit screens."""
    v: dict = {
        "name": business.name, "description": business.description or "", "address": business.address or "",
        "timezone": business.timezone, "business_type": business.business_type, "voice": business.voice,
        "language": business.language, "persona": business.persona or "", "greeting": business.greeting or "",
        "tax_rate": f"{business.tax_rate_bps / 100:g}" if business.tax_rate_bps else "",
        "large_order": f"{business.large_order_cents / 100:g}" if business.large_order_cents else "",
        "handoff_phone": business.handoff_phone or "", "notify_phone": business.notify_phone or "",
        "closures_text": closures_to_text(business.closures),
        "retention_days": str(business.transcript_retention_days or ""),
        "phone_number": phone_number or "",
    }
    for key in ("takes_orders", "offers_pickup", "offers_delivery", "is_active", "transfers_enabled"):
        if getattr(business, key):
            v[key] = "on"
    for day in DAYS:
        ranges = (business.hours or {}).get(day)
        if ranges:
            v[f"open_{day}"], v[f"close_{day}"] = ranges[0]
        else:
            v[f"closed_{day}"] = "on"
            v[f"open_{day}"], v[f"close_{day}"] = "09:00", "17:00"
    return v


def new_form_defaults(business_type: str = DEFAULT_TYPE) -> dict:
    btype = get_type(business_type)
    values = {
        "business_type": btype.key, "timezone": "America/New_York", "language": "en-US", "voice": "marin",
        "offers_pickup": "on", "is_active": "on", "transfers_enabled": "on",
    }
    if btype.takes_orders:
        values["takes_orders"] = "on"
    for day in DAYS:
        values[f"open_{day}"], values[f"close_{day}"] = "11:00", "22:00"
    return values


# --- add a business ------------------------------------------------------------------------
@dataclass
class BusinessInput:
    fields: dict
    phone_number: str
    owner_name: str | None
    owner_email: str
    catalog: list[ParsedCategory] = field(default_factory=list)
    faqs: list[tuple[str, str]] = field(default_factory=list)
    is_active: bool = True


@dataclass
class CreatedBusiness:
    business: Business
    owner_email: str
    item_count: int


def parse_form(form: Mapping[str, str]) -> tuple[BusinessInput | None, list[str]]:
    """Turn the add-business form into a BusinessInput, collecting every problem at once."""
    errors: list[str] = []
    get = lambda key: (form.get(key) or "").strip()  # noqa: E731
    fields = parse_business_fields(form, errors, platform=True)
    phone_number = parse_phone(get("phone_number"), "VanakkamIQ phone number", errors, required=True)

    owner_email = get("owner_email").lower()
    if not _EMAIL.match(owner_email):
        errors.append("Owner email is required and must be a valid address.")

    catalog: list[ParsedCategory] = []
    if fields.get("takes_orders"):
        catalog, catalog_errors = parse_menu_text(form.get("catalog_text") or "")
        errors += catalog_errors
    faqs, faq_errors = parse_faq_text(form.get("faq_text") or "")
    errors += faq_errors

    if errors:
        return None, errors
    return BusinessInput(
        fields=fields, phone_number=phone_number, owner_name=get("owner_name") or None,
        owner_email=owner_email, catalog=catalog, faqs=faqs,
        is_active=bool(form.get("is_active")),
    ), []


async def _unique_slug(db: AsyncSession, name: str) -> str:
    base = slugify(name)
    slug, n = base, 2
    while await db.scalar(select(func.count()).select_from(Business).where(Business.slug == slug)):
        slug, n = f"{base}-{n}", n + 1
    return slug


async def phone_number_problem(db: AsyncSession, number: str, business_id: int | None = None) -> str | None:
    taken = await db.scalar(select(PhoneNumber).where(PhoneNumber.e164 == number))
    if taken is not None and taken.business_id != business_id:
        owner = await db.get(Business, taken.business_id)
        return f"{number} is already assigned to {owner.name if owner else 'another business'}."
    return None


async def add_catalog(db: AsyncSession, business_id: int, catalog: list[ParsedCategory], start_order: int = 0) -> int:
    count = 0
    for c_index, parsed in enumerate(catalog, start=start_order):
        category = CatalogCategory(business_id=business_id, name=parsed.name, sort_order=c_index)
        db.add(category)
        await db.flush()
        for i_index, p in enumerate(parsed.items):
            db.add(CatalogItem(
                business_id=business_id, category_id=category.id, name=p.name, price_cents=p.price_cents,
                aliases=p.aliases, tags=p.tags, sort_order=i_index, description=p.description,
                is_available=p.available, external_id=p.external_id,
                modifier_groups=[
                    ModifierGroup(
                        name=g.name, min_select=g.min_select, max_select=g.max_select,
                        modifiers=[Modifier(name=o, price_delta_cents=cents, is_available=on) for o, cents, on in g.options],
                    )
                    for g in p.groups
                ],
            ))
            count += 1
    return count


async def replace_catalog(db: AsyncSession, business_id: int, catalog: list[ParsedCategory]) -> int:
    """Swap the whole catalog for a new one (items in past orders keep their snapshot names).

    Options are deleted explicitly rather than left to ON DELETE CASCADE, so no database
    (SQLite doesn't enforce foreign keys by default) can leave them attached to a reused id."""
    items = select(CatalogItem.id).where(CatalogItem.business_id == business_id)
    groups = select(ModifierGroup.id).where(ModifierGroup.menu_item_id.in_(items))
    await db.execute(delete(Modifier).where(Modifier.group_id.in_(groups)))
    await db.execute(delete(ModifierGroup).where(ModifierGroup.menu_item_id.in_(items)))
    await db.execute(delete(CatalogItem).where(CatalogItem.business_id == business_id))
    await db.execute(delete(CatalogCategory).where(CatalogCategory.business_id == business_id))
    return await add_catalog(db, business_id, catalog)


async def create_business(db: AsyncSession, data: BusinessInput) -> CreatedBusiness:
    errors: list[str] = []
    if problem := await phone_number_problem(db, data.phone_number):
        errors.append(problem)
    if await db.scalar(select(StaffUser).where(func.lower(StaffUser.email) == data.owner_email)):
        errors.append(f"{data.owner_email} already has a VanakkamIQ login.")
    if errors:
        raise OnboardingError(errors)

    business = Business(slug=await _unique_slug(db, data.fields["name"]), sms_from=data.phone_number,
                        is_active=data.is_active, **data.fields)
    db.add(business)
    await db.flush()
    db.add(PhoneNumber(business_id=business.id, e164=data.phone_number, carrier="telnyx"))

    db.add(StaffUser(business_id=business.id, email=data.owner_email, name=data.owner_name, role="owner"))
    item_count = await add_catalog(db, business.id, data.catalog)
    for question, answer in data.faqs:
        db.add(Faq(business_id=business.id, question=question, answer=answer))
    await db.commit()
    return CreatedBusiness(business, data.owner_email, item_count)


async def update_business(db: AsyncSession, business: Business, fields: dict, phone_number: str | None = None) -> list[str]:
    """Apply edited settings. Changing the VanakkamIQ number is platform-only (pass phone_number)."""
    if phone_number:
        if problem := await phone_number_problem(db, phone_number, business.id):
            return [problem]
        current = await db.scalar(select(PhoneNumber).where(PhoneNumber.business_id == business.id))
        old = current.e164 if current else None
        if current is None:
            db.add(PhoneNumber(business_id=business.id, e164=phone_number, carrier="telnyx"))
        else:
            current.e164 = phone_number
        if not business.sms_from or business.sms_from == old:
            business.sms_from = phone_number
    for key, value in fields.items():
        setattr(business, key, value)
    await db.commit()
    return []


@dataclass
class SetupCheck:
    label: str
    done: bool
    detail: str
    required: bool = True


async def setup_checklist(db: AsyncSession, business: Business) -> list[SetupCheck]:
    btype = get_type(business.business_type)
    numbers = list(await db.scalars(select(PhoneNumber.e164).where(PhoneNumber.business_id == business.id)))
    items = await db.scalar(select(func.count()).select_from(CatalogItem).where(CatalogItem.business_id == business.id))
    faqs = await db.scalar(select(func.count()).select_from(Faq).where(Faq.business_id == business.id))
    owners = await db.scalar(select(func.count()).select_from(StaffUser).where(StaffUser.business_id == business.id))
    checks = [SetupCheck("Phone number", bool(numbers), ", ".join(numbers) or "No number assigned")]
    if business.takes_orders:
        checks.append(SetupCheck(btype.catalog, bool(items),
                                 f"{items} item(s)" if items else "Nothing yet; the AI can't take orders"))
    checks += [
        SetupCheck("Opening hours", bool(business.hours), "Set" if business.hours else "Not set"),
        SetupCheck("Owner login", bool(owners), f"{owners} account(s)"),
        SetupCheck("Saved answers (FAQs)", bool(faqs), f"{faqs} saved answer(s)" if faqs else
                   "None yet: the AI can only share hours and address", required=not business.takes_orders),
        SetupCheck("Transfers to staff", bool(business.handoff_phone and business.transfers_enabled),
                   f"On: {business.handoff_phone}" if business.handoff_phone and business.transfers_enabled
                   else "Off: callers who ask for a person are promised a call back", required=False),
        SetupCheck("Staff alert number", bool(business.notify_phone),
                   business.notify_phone or "Not set: no text alerts for new orders and messages", required=False),
    ]
    return checks


def today_for(business: Business) -> date:
    return local_now(business).date()

