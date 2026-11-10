from app.onboarding.menu_import import parse_faq_text, parse_menu_text, parse_money

MENU = """
# Biryani
Chicken Biryani | 15.99 | spicy | aka: chicken biriyani, biriyani | Spice level*: Mild, Medium, Hot | Add-ons (up to 2): Extra raita +1.50, Boiled egg +$2
Vegetable Biryani | $13.99 | vegetarian, vegan option

# Drinks
Mango Lassi | 4.99
Filter Coffee | 3.5
"""


def test_parses_categories_items_and_options():
    categories, errors = parse_menu_text(MENU)
    assert errors == []
    assert [c.name for c in categories] == ["Biryani", "Drinks"]
    chicken = categories[0].items[0]
    assert chicken.name == "Chicken Biryani" and chicken.price_cents == 1599
    assert chicken.tags == ["spicy"]
    assert chicken.aliases == ["chicken biriyani", "biriyani"]
    spice, addons = chicken.groups
    assert (spice.name, spice.min_select, spice.max_select) == ("Spice level", 1, 1)
    assert [o for o, _, _ in spice.options] == ["Mild", "Medium", "Hot"]
    assert (addons.name, addons.min_select, addons.max_select) == ("Add-ons", 0, 2)
    assert addons.options == [("Extra raita", 150, True), ("Boiled egg", 200, True)]
    assert categories[0].items[1].tags == ["vegetarian", "vegan option"]
    assert categories[1].items[1].price_cents == 350


def test_items_without_category_go_under_menu():
    categories, errors = parse_menu_text("Masala Dosa | 11.99")
    assert errors == [] and categories[0].name == "Menu"


def test_reports_problems_with_line_numbers():
    text = "# Mains\nDosa | cheap\nIdli\nDosa | 9\nDosa | 10\nVada | 5 | Size (up to 3): Small, Large\n#\n"
    _, errors = parse_menu_text(text)
    assert any(e.startswith("Line 2:") and "not a price" in e for e in errors)
    assert any(e.startswith("Line 3:") for e in errors)
    assert any(e.startswith("Line 5:") and "more than once" in e for e in errors)
    assert any(e.startswith("Line 6:") and "up to 3" in e for e in errors)
    assert any(e.startswith("Line 7:") for e in errors)


def test_parse_money():
    assert parse_money("12") == 1200
    assert parse_money("$12.5") == 1250
    assert parse_money("12.05") == 1205
    assert parse_money("twelve") is None


def test_parse_faqs():
    faqs, errors = parse_faq_text("Parking? | Free, out back.\nno separator here\n")
    assert faqs == [("Parking?", "Free, out back.")]
    assert errors and "line 2" in errors[0]
