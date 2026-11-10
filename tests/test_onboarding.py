import pytest
from sqlalchemy import select

from app.db.models import Business, CatalogItem, Faq, PhoneNumber, StaffUser
from app.onboarding.service import (
    OnboardingError, business_form_values, create_business, parse_business_fields, parse_closures, parse_form,
    setup_checklist,
)


def valid_form(**overrides) -> dict:
    form = {
        "name": "Chennai Spice House", "address": "45 Elm St", "timezone": "America/Chicago",
        "business_type": "restaurant", "takes_orders": "on",
        "language": "en-US", "voice": "marin", "tax_rate": "8.875", "large_order": "300",
        "offers_pickup": "on", "offers_delivery": "on", "is_active": "on",
        "phone_number": "(312) 555-0142", "handoff_phone": "312-555-0199", "notify_phone": "",
        "owner_name": "Lakshmi", "owner_email": "Owner@ChennaiSpice.com",
        "catalog_text": "# Dosa\nMasala Dosa | 11.99 | vegetarian | Chutney*: Coconut, Tomato",
        "faq_text": "Parking? | Street parking only.",
    }
    for day in ("mon", "tue", "wed", "thu", "fri", "sat", "sun"):
        form[f"open_{day}"], form[f"close_{day}"] = "11:00", "22:00"
    form["closed_mon"] = "on"
    form.update(overrides)
    return form


def test_parse_form_normalizes_values():
    data, errors = parse_form(valid_form())
    assert errors == []
    assert data.phone_number == "+13125550142"
    assert data.owner_email == "owner@chennaispice.com"
    fields = data.fields
    assert fields["handoff_phone"] == "+13125550199" and fields["notify_phone"] is None
    assert fields["tax_rate_bps"] == 888  # 8.875% rounds to 8.88%
    assert fields["large_order_cents"] == 30000
    assert fields["business_type"] == "restaurant" and fields["takes_orders"] is True
    assert "mon" not in fields["hours"] and fields["hours"]["tue"] == [["11:00", "22:00"]]


def test_parse_form_collects_all_errors():
    _, errors = parse_form(valid_form(
        name="", phone_number="12", owner_email="nope", tax_rate="40",
        open_tue="", offers_pickup="", offers_delivery="", catalog_text="Dosa | cheap",
    ))
    text = " ".join(errors)
    for expected in ("name is required", "VanakkamIQ phone number", "Owner email", "Tax rate",
                     "Hours for Tue", "pickup, delivery", "not a price"):
        assert expected in text


async def test_create_business_and_checklist(services):
    data, _ = parse_form(valid_form())
    async with services.session_factory() as db:
        created = await create_business(db, data)
        assert created.item_count == 1
        business = await db.get(Business, created.business.id)
        assert business.slug == "chennai-spice-house" and business.sms_from == "+13125550142"
        assert (await db.scalar(select(PhoneNumber).where(PhoneNumber.business_id == business.id))).e164 == "+13125550142"
        owner = await db.scalar(select(StaffUser).where(StaffUser.business_id == business.id))
        assert owner.email == "owner@chennaispice.com" and owner.role == "owner"
        item = await db.scalar(select(CatalogItem).where(CatalogItem.business_id == business.id))
        assert item.modifier_groups[0].name == "Chutney" and item.modifier_groups[0].min_select == 1
        assert await db.scalar(select(Faq).where(Faq.business_id == business.id)) is not None
        checklist = await setup_checklist(db, business)
        assert all(c.done for c in checklist if c.required)


async def test_duplicate_number_and_email_are_rejected(services):
    first, _ = parse_form(valid_form())
    async with services.session_factory() as db:
        await create_business(db, first)
    again, _ = parse_form(valid_form(name="Another Place"))
    async with services.session_factory() as db:
        with pytest.raises(OnboardingError) as exc:
            await create_business(db, again)
    assert any("already assigned to Chennai Spice House" in e for e in exc.value.errors)
    assert any("already has a VanakkamIQ login" in e for e in exc.value.errors)


async def test_same_name_gets_unique_slug(services):
    first, _ = parse_form(valid_form())
    second, _ = parse_form(valid_form(phone_number="+13125550143", owner_email="two@example.com"))
    async with services.session_factory() as db:
        await create_business(db, first)
        created = await create_business(db, second)
    assert created.business.slug == "chennai-spice-house-2"


def test_faq_only_business_needs_no_catalog_or_pickup():
    data, errors = parse_form(valid_form(
        name="Bright Smile Dental", business_type="services", takes_orders="", offers_pickup="", offers_delivery="",
        catalog_text="this would not parse | nope", faq_text="Do you take walk-ins? | Yes, until 4 PM.",
    ))
    assert errors == []
    assert data.fields["business_type"] == "services" and data.fields["takes_orders"] is False
    assert data.catalog == [] and data.faqs == [("Do you take walk-ins?", "Yes, until 4 PM.")]


async def test_faq_only_business_checklist_wants_answers_not_a_catalog(services):
    data, _ = parse_form(valid_form(business_type="services", takes_orders="", faq_text=""))
    async with services.session_factory() as db:
        created = await create_business(db, data)
        checks = {c.label: c for c in await setup_checklist(db, created.business)}
    assert "Menu" not in checks
    assert checks["Saved answers (FAQs)"].required and not checks["Saved answers (FAQs)"].done


def test_unknown_business_type_is_rejected():
    _, errors = parse_form(valid_form(business_type="spaceship"))
    assert any("Unknown business type" in e for e in errors)


def test_closures_parse_sort_and_drop_past_dates():
    from datetime import date
    errors: list[str] = []
    text = "\n".join(["2026-12-25 Christmas Day", "2026-01-01 Old", "not a date", "2026-11-27"])
    closures = parse_closures(text, errors, today=date(2026, 11, 9))
    assert closures == [{"date": "2026-11-27", "note": ""}, {"date": "2026-12-25", "note": "Christmas Day"}]
    assert len(errors) == 1 and "line 3" in errors[0]


async def test_settings_round_trip_through_the_form(services):
    data, _ = parse_form(valid_form(closures_text="2030-12-25 Christmas", retention_days="90", greeting="Hello there!"))
    async with services.session_factory() as db:
        created = await create_business(db, data)
    values = business_form_values(created.business, "+13125550142")
    errors: list[str] = []
    fields = parse_business_fields(values, errors, platform=True)
    assert errors == []
    for key in ("name", "hours", "closures", "tax_rate_bps", "large_order_cents", "takes_orders", "offers_delivery",
                "transcript_retention_days", "greeting", "handoff_phone"):
        assert fields[key] == getattr(created.business, key), key

