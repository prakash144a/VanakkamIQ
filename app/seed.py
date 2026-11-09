"""Create a demo restaurant to test calls with:  python -m app.seed

Safe to run more than once. Prices and dishes are sample data; edit them in the dashboard.
"""

import asyncio
import os

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import (
    Base, Business, Faq, MenuCategory, MenuItem, Modifier, ModifierGroup, PhoneNumber, StaffUser,
)
from app.db.session import make_engine, make_session_factory
from app.security import hash_password

DEMO_SLUG = "demo-kitchen"
EVERY_DAY = {d: [["11:00", "22:00"]] for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}

MENU = {
    "Biryani": [
        ("Chicken Biryani", 1599, ["chicken biriyani"], ["spicy"], True),
        ("Vegetable Biryani", 1399, ["veg biryani"], ["vegetarian"], True),
        ("Goat Biryani", 1899, ["mutton biryani"], ["spicy"], True),
    ],
    "Dosa": [
        ("Masala Dosa", 1199, ["masala dosai"], ["vegetarian"], False),
        ("Plain Dosa", 999, ["sada dosa"], ["vegetarian"], False),
    ],
    "Curries": [
        ("Chettinad Chicken", 1699, ["chicken chettinad"], ["spicy"], True),
        ("Paneer Butter Masala", 1499, ["paneer makhani"], ["vegetarian"], True),
    ],
    "Breads & Sides": [
        ("Butter Naan", 349, [], ["vegetarian"], False),
        ("Parotta", 399, ["paratha", "porotta"], ["vegetarian"], False),
        ("Veg Samosa (2 pcs)", 599, ["samosa"], ["vegetarian"], False),
    ],
    "Drinks": [
        ("Mango Lassi", 499, [], ["vegetarian"], False),
        ("Filter Coffee", 349, ["madras coffee"], ["vegetarian"], False),
    ],
}

FAQS = [
    ("Do you deliver?", "Yes, we deliver within 5 miles. Delivery usually takes 40 to 50 minutes."),
    ("Is there parking?", "Yes, there's free parking behind the restaurant."),
    ("Do you have vegan options?", "Yes. Our vegetable biryani and plain dosa can be made vegan; just ask when ordering."),
    ("How long does pickup take?", "Pickup orders are usually ready in 20 to 25 minutes."),
    ("Do you cater events?", "Yes, we cater events. A team member can help with catering orders."),
]


async def seed(db: AsyncSession) -> Business:
    existing = await db.scalar(select(Business).where(Business.slug == DEMO_SLUG))
    if existing is not None:
        return existing

    business = Business(
        name="VanakkamIQ Demo Kitchen", slug=DEMO_SLUG, vertical="restaurant",
        timezone="America/New_York", address="123 Main Street", hours=EVERY_DAY,
        tax_rate_bps=800, large_order_cents=25000, offers_pickup=True, offers_delivery=True,
        handoff_phone=os.getenv("SEED_HANDOFF_PHONE") or None,
        notify_phone=os.getenv("SEED_NOTIFY_PHONE") or None,
        sms_from=os.getenv("SEED_PHONE_NUMBER", "+15555550100"),
    )
    db.add(business)
    await db.flush()
    db.add(PhoneNumber(business_id=business.id, e164=os.getenv("SEED_PHONE_NUMBER", "+15555550100")))
    db.add(StaffUser(
        business_id=business.id, email=os.getenv("SEED_ADMIN_EMAIL", "admin@example.com"), name="Owner",
        password_hash=hash_password(os.getenv("SEED_ADMIN_PASSWORD", "change-me")),
    ))

    for c_index, (category_name, items) in enumerate(MENU.items()):
        category = MenuCategory(business_id=business.id, name=category_name, sort_order=c_index)
        db.add(category)
        await db.flush()
        for i_index, (name, price, aliases, tags, spice) in enumerate(items):
            item = MenuItem(
                business_id=business.id, category_id=category.id, name=name, price_cents=price,
                aliases=aliases, tags=tags, sort_order=i_index, modifier_groups=[],
            )
            if spice:
                item.modifier_groups.append(ModifierGroup(
                    name="Spice level", min_select=1, max_select=1,
                    modifiers=[Modifier(name=n) for n in ("Mild", "Medium", "Hot")],
                ))
            if category_name == "Biryani":
                item.modifier_groups.append(ModifierGroup(
                    name="Add-ons", min_select=0, max_select=2,
                    modifiers=[Modifier(name="Extra raita", price_delta_cents=150),
                               Modifier(name="Boiled egg", price_delta_cents=200)],
                ))
            db.add(item)

    for question, answer in FAQS:
        db.add(Faq(business_id=business.id, question=question, answer=answer))
    await db.commit()
    return business


async def main() -> None:
    settings = get_settings()
    engine = make_engine(settings.database_url, settings.database_schema)
    if settings.database_url.startswith("sqlite"):
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    async with make_session_factory(engine)() as db:
        business = await seed(db)
        print(f"Demo business ready: {business.name} (id {business.id})")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
