from app.db.models import MenuItem, Modifier, ModifierGroup
from app.money import format_money
from app.verticals.restaurant.menu_service import resolve_modifiers, search_items, text_score


def _biryani() -> MenuItem:
    spice = ModifierGroup(id=1, name="Spice level", min_select=1, max_select=1, modifiers=[
        Modifier(id=1, group_id=1, name="Mild", price_delta_cents=0, is_available=True),
        Modifier(id=2, group_id=1, name="Hot", price_delta_cents=0, is_available=True),
    ])
    addons = ModifierGroup(id=2, name="Add-ons", min_select=0, max_select=2, modifiers=[
        Modifier(id=3, group_id=2, name="Extra raita", price_delta_cents=150, is_available=True),
        Modifier(id=4, group_id=2, name="Boiled egg", price_delta_cents=200, is_available=False),
    ])
    return MenuItem(id=1, name="Chicken Biryani", price_cents=1599, aliases=["chicken biriyani"], tags=[],
                    is_available=True, modifier_groups=[spice, addons])


def _items() -> list[MenuItem]:
    return [
        _biryani(),
        MenuItem(id=2, name="Masala Dosa", price_cents=1199, aliases=[], tags=[], is_available=True, modifier_groups=[]),
        MenuItem(id=3, name="Mango Lassi", price_cents=499, aliases=[], tags=[], is_available=True, modifier_groups=[]),
    ]


def test_search_handles_spoken_variants():
    assert search_items(_items(), "chicken biriyani")[0][0].name == "Chicken Biryani"
    assert search_items(_items(), "the dosa")[0][0].name == "Masala Dosa"
    assert search_items(_items(), "mango lassie")[0][0].name == "Mango Lassi"
    assert search_items(_items(), "pizza") == []


def test_text_score_exact_is_best():
    assert text_score("Mango Lassi", "mango lassi") == 1.0


def test_resolve_modifiers_requires_spice_level():
    chosen, problems = resolve_modifiers(_biryani(), [])
    assert chosen == []
    assert any("Spice level" in p for p in problems)


def test_resolve_modifiers_matches_and_flags_unavailable():
    chosen, problems = resolve_modifiers(_biryani(), ["hot", "extra raita"])
    assert [m.name for m in chosen] == ["Hot", "Extra raita"]
    assert problems == []
    _, problems = resolve_modifiers(_biryani(), ["mild", "boiled egg"])
    assert any("not available" in p for p in problems)
    _, problems = resolve_modifiers(_biryani(), ["mild", "cheese"])
    assert any("not an option" in p for p in problems)


def test_format_money():
    assert format_money(3778) == "$37.78"
    assert format_money(5) == "$0.05"
    assert format_money(-150) == "-$1.50"
