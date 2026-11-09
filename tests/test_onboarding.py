import pytest
from sqlalchemy import select

from app.db.models import Business, Faq, MenuItem, PhoneNumber, StaffUser
from app.onboarding.service import OnboardingError, create_restaurant, parse_form, setup_checklist
from app.security import verify_password


def valid_form(**overrides) -> dict:
    form = {
        "name": "Chennai Spice House", "address": "45 Elm St", "timezone": "America/Chicago",
        "language": "en-US", "voice": "marin", "tax_rate": "8.875", "large_order": "300",
        "offers_pickup": "on", "offers_delivery": "on", "is_active": "on",
        "phone_number": "(312) 555-0142", "handoff_phone": "312-555-0199", "notify_phone": "",
        "owner_name": "Lakshmi", "owner_email": "Owner@ChennaiSpice.com", "owner_password": "",
        "menu_text": "# Dosa\nMasala Dosa | 11.99 | vegetarian | Chutney*: Coconut, Tomato",
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
    assert data.handoff_phone == "+13125550199"
    assert data.notify_phone is None
    assert data.owner_email == "owner@chennaispice.com"
    assert data.tax_rate_bps == 888  # 8.875% rounds to 8.88%
    assert data.large_order_cents == 30000
    assert "mon" not in data.hours and data.hours["tue"] == [["11:00", "22:00"]]


def test_parse_form_collects_all_errors():
    _, errors = parse_form(valid_form(
        name="", phone_number="12", owner_email="nope", tax_rate="40", owner_password="short",
        open_tue="", offers_pickup="", offers_delivery="", menu_text="Dosa | cheap",
    ))
    text = " ".join(errors)
    for expected in ("name is required", "VanakkamIQ phone number", "Owner email", "Tax rate",
                     "at least 10 characters", "Hours for Tue", "pickup, delivery", "not a price"):
        assert expected in text


async def test_create_restaurant_and_checklist(services):
    data, _ = parse_form(valid_form())
    async with services.session_factory() as db:
        created = await create_restaurant(db, data)
        assert created.item_count == 1 and created.temporary_password
        business = await db.get(Business, created.business.id)
        assert business.slug == "chennai-spice-house" and business.sms_from == "+13125550142"
        assert (await db.scalar(select(PhoneNumber).where(PhoneNumber.business_id == business.id))).e164 == "+13125550142"
        owner = await db.scalar(select(StaffUser).where(StaffUser.business_id == business.id))
        assert verify_password(owner.password_hash, created.temporary_password)
        item = await db.scalar(select(MenuItem).where(MenuItem.business_id == business.id))
        assert item.modifier_groups[0].name == "Chutney" and item.modifier_groups[0].min_select == 1
        assert await db.scalar(select(Faq).where(Faq.business_id == business.id)) is not None
        checklist = await setup_checklist(db, business)
        assert all(c.done for c in checklist if c.required)


async def test_duplicate_number_and_email_are_rejected(services):
    first, _ = parse_form(valid_form())
    async with services.session_factory() as db:
        await create_restaurant(db, first)
    again, _ = parse_form(valid_form(name="Another Place"))
    async with services.session_factory() as db:
        with pytest.raises(OnboardingError) as exc:
            await create_restaurant(db, again)
    assert any("already assigned to Chennai Spice House" in e for e in exc.value.errors)
    assert any("already has a VanakkamIQ login" in e for e in exc.value.errors)


async def test_same_name_gets_unique_slug(services):
    first, _ = parse_form(valid_form())
    second, _ = parse_form(valid_form(phone_number="+13125550143", owner_email="two@example.com"))
    async with services.session_factory() as db:
        await create_restaurant(db, first)
        created = await create_restaurant(db, second)
    assert created.business.slug == "chennai-spice-house-2"
