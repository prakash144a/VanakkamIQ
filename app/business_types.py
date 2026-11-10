"""Business types change wording and defaults only. What the AI can do comes from modules
switched on per business: questions and messages always; ordering when `takes_orders` is on."""

from dataclasses import dataclass


@dataclass(frozen=True)
class BusinessType:
    key: str
    label: str
    hint: str
    kind: str  # how the AI describes the business: "a restaurant"
    role: str  # the AI's job title: "phone host"
    catalog: str  # what the orderable list is called: "Menu"
    item: str  # one thing on it: "dish"
    takes_orders: bool  # default for new businesses
    persona: str
    closed_note: str  # mentioned in the greeting when closed
    handoff_rules: tuple[str, ...] = ()
    in_progress: str = "Preparing"
    ready_pickup: str = "Ready for pickup"
    ready_delivery: str = "Out for delivery"
    catalog_example: str = ""
    faq_example: str = ""


TYPES: dict[str, BusinessType] = {t.key: t for t in (
    BusinessType(
        key="restaurant", label="Restaurant or café", hint="Takes food orders and answers questions",
        kind="a restaurant", role="phone host", catalog="Menu", item="dish", takes_orders=True,
        persona=("Warm, gracious and upbeat, like a favorite host who remembers regulars. "
                 "Unhurried but efficient. A light, natural sense of humor; never pushy."),
        closed_note="the kitchen is closed right now",
        handoff_rules=(
            "Food allergies or medical diets: never promise anything is allergen-free; offer a team member.",
            "Catering requests.",
        ),
        catalog_example="""# Biryani
Chicken Biryani | 15.99 | spicy | aka: chicken biriyani | Spice level*: Mild, Medium, Hot | Add-ons (up to 2): Extra raita +1.50, Boiled egg +2.00
Vegetable Biryani | 13.99 | vegetarian | Spice level*: Mild, Medium, Hot

# Drinks
Mango Lassi | 4.99 | vegetarian""",
        faq_example="""Is there parking? | Yes, free parking behind the restaurant.
Do you deliver? | Yes, within 5 miles. Delivery takes 40 to 50 minutes.""",
    ),
    BusinessType(
        key="retail", label="Shop or store", hint="Takes orders for pickup or delivery and answers questions",
        kind="a shop", role="phone assistant", catalog="Products", item="product", takes_orders=True,
        persona="Friendly, knowledgeable and patient, like a helpful shop assistant. Clear and concise.",
        closed_note="the shop is closed right now",
        handoff_rules=("Returns, exchanges or warranty claims.", "Bulk or wholesale orders."),
        in_progress="Being packed",
        catalog_example="""# Flowers
Rose bouquet | 39.00 | aka: dozen roses | Color*: Red, Pink, White | Card: Yes +3.00
Seasonal arrangement | 55.00

# Plants
Peace lily | 28.00""",
        faq_example="""Do you deliver? | Yes, same-day delivery within 10 miles for orders before 2 PM.
Can I return an item? | Yes, within 14 days with the receipt.""",
    ),
    BusinessType(
        key="services", label="Service business", hint="Salon, clinic, repair shop, studio: answers questions and takes messages",
        kind="a service business", role="receptionist", catalog="Services", item="service", takes_orders=False,
        persona="Calm, warm and professional, like an experienced front-desk receptionist.",
        closed_note="the office is closed right now",
        handoff_rules=(
            "Anything that needs professional judgment (medical, legal, financial or safety advice).",
            "Requests to book, change or cancel an appointment: take a message for the team.",
            "Price quotes that aren't in the saved answers.",
        ),
        faq_example="""What services do you offer? | Haircuts, coloring and styling for adults and kids.
How much is a haircut? | Adult haircuts start at $35; kids under 12 are $20.""",
    ),
    BusinessType(
        key="other", label="Other business", hint="Answers questions and takes messages",
        kind="a business", role="phone assistant", catalog="Catalog", item="item", takes_orders=False,
        persona="Warm, clear and helpful. Professional but never stiff.",
        closed_note="we're closed right now",
        faq_example="""Where are you located? | 123 Main Street, next to the post office.
How can I reach the owner? | Leave a message and they'll call you back the same day.""",
    ),
)}

DEFAULT_TYPE = "restaurant"


def get_type(key: str | None) -> BusinessType:
    return TYPES.get(key or "", TYPES["other"])


def status_label(btype: BusinessType, status: str, fulfillment: str = "pickup") -> str:
    return {
        "draft": "Not finished",
        "confirmed": "New",
        "in_progress": btype.in_progress,
        "ready": btype.ready_delivery if fulfillment == "delivery" else btype.ready_pickup,
        "completed": "Delivered" if fulfillment == "delivery" else "Picked up",
        "cancelled": "Cancelled",
    }.get(status, status.replace("_", " ").capitalize())
