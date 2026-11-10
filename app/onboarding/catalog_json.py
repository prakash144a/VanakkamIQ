"""Catalog (menu, products, services) as a JSON file: upload with a preview, and download.

    {
      "sections": [
        {
          "name": "Biryani",
          "items": [
            {
              "id": "BIR-001",                       optional: your own code (POS number, SKU)
              "name": "Chicken Biryani",             required
              "price": 15.99,                        required: dollars, number or "15.99"
              "description": "...",                  optional
              "aliases": ["chicken biriyani"],       optional: other names callers say
              "tags": ["spicy", "halal"],            optional
              "available": true,                     optional, default true
              "options": [                           optional
                {
                  "name": "Spice level",             required
                  "min": 1,                          optional, default 0 (choosing is optional)
                  "max": 1,                          optional, default 1
                  "choices": [                       required, at least one
                    {"name": "Mild", "price": 0, "available": true}
                  ]
                }
              ]
            }
          ]
        }
      ]
    }

The whole file is checked before anything is saved; every problem is reported with where it is.
"""

import json
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from app.onboarding.menu_import import ParsedCategory, ParsedGroup, ParsedItem

MAX_BYTES = 1_000_000
MAX_ITEMS = 2_000
MAX_PRICE_CENTS = 10_000_000  # $100,000

_ITEM_KEYS = {"id", "name", "price", "description", "aliases", "tags", "available", "options"}
_GROUP_KEYS = {"name", "min", "max", "choices"}
_CHOICE_KEYS = {"name", "price", "available"}


class _Problems:
    def __init__(self) -> None:
        self.items: list[str] = []

    def add(self, where: str, message: str) -> None:
        if len(self.items) < 50:
            self.items.append(f"{where}: {message}" if where else message)


def _text(value, where: str, field_name: str, problems: _Problems, *, required: bool, limit: int) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            problems.add(where, f"\"{field_name}\" is required.")
        return None
    if not isinstance(value, str):
        problems.add(where, f"\"{field_name}\" should be text.")
        return None
    value = value.strip()
    if len(value) > limit:
        problems.add(where, f"\"{field_name}\" is longer than {limit} characters.")
        return None
    return value


def _cents(value, where: str, field_name: str, problems: _Problems, *, required: bool) -> int | None:
    if value is None:
        if required:
            problems.add(where, f"\"{field_name}\" is required.")
        return None if required else 0
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        problems.add(where, f"\"{field_name}\" should be an amount like 12.99.")
        return None
    try:
        amount = Decimal(str(value).strip().lstrip("$"))
    except InvalidOperation:
        problems.add(where, f"\"{field_name}\" should be an amount like 12.99, not {value!r}.")
        return None
    if amount < 0 or amount * 100 != int(amount * 100) or amount * 100 > MAX_PRICE_CENTS:
        problems.add(where, f"\"{field_name}\" should be a positive amount with at most 2 decimals, like 12.99.")
        return None
    return int(amount * 100)


def _bool(value, where: str, field_name: str, problems: _Problems) -> bool:
    if value is None:
        return True
    if not isinstance(value, bool):
        problems.add(where, f"\"{field_name}\" should be true or false.")
        return True
    return value


def _int(value, where: str, field_name: str, problems: _Problems, default: int) -> int | None:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        problems.add(where, f"\"{field_name}\" should be a whole number (0 or more).")
        return None
    return value


def _words(value, where: str, field_name: str, problems: _Problems, *, lower: bool = False) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        problems.add(where, f"\"{field_name}\" should be a list of text, like [\"spicy\", \"halal\"].")
        return []
    words = [v.strip() for v in value if v.strip()]
    return [w.lower() for w in words] if lower else words


def _unknown(obj: dict, allowed: set[str], where: str, problems: _Problems) -> None:
    extra = sorted(set(obj) - allowed)
    if extra:
        problems.add(where, f"unknown field{'s' if len(extra) > 1 else ''} {', '.join(extra)} (allowed: {', '.join(sorted(allowed))}).")


def _group(raw, where: str, problems: _Problems) -> ParsedGroup | None:
    if not isinstance(raw, dict):
        problems.add(where, "each option group should be an object with \"name\" and \"choices\".")
        return None
    _unknown(raw, _GROUP_KEYS, where, problems)
    name = _text(raw.get("name"), where, "name", problems, required=True, limit=120)
    where = f"{where} ({name})" if name else where
    low = _int(raw.get("min"), where, "min", problems, 0)
    high = _int(raw.get("max"), where, "max", problems, 1)
    choices_raw = raw.get("choices")
    if not isinstance(choices_raw, list) or not choices_raw:
        problems.add(where, "\"choices\" is required: a list with at least one choice.")
        return None
    choices: list[tuple[str, int, bool]] = []
    seen: set[str] = set()
    for c_index, choice in enumerate(choices_raw):
        c_where = f"{where} → choices[{c_index}]"
        if not isinstance(choice, dict):
            problems.add(c_where, "each choice should be an object like {\"name\": \"Mild\"}.")
            continue
        _unknown(choice, _CHOICE_KEYS, c_where, problems)
        c_name = _text(choice.get("name"), c_where, "name", problems, required=True, limit=120)
        cents = _cents(choice.get("price"), c_where, "price", problems, required=False)
        available = _bool(choice.get("available"), c_where, "available", problems)
        if c_name and c_name.lower() in seen:
            problems.add(c_where, f"\"{c_name}\" is listed twice.")
        elif c_name and cents is not None:
            seen.add(c_name.lower())
            choices.append((c_name, cents, available))
    if name is None or low is None or high is None:
        return None
    if high < 1:
        problems.add(where, "\"max\" should be at least 1.")
    elif high > len(choices_raw):
        n = len(choices_raw)
        problems.add(where, f"\"max\" is {high} but there {'is only 1 choice' if n == 1 else f'are only {n} choices'}.")
    elif low > high:
        problems.add(where, f"\"min\" ({low}) is larger than \"max\" ({high}).")
    return ParsedGroup(name, low, high, choices)


def parse_catalog_json(raw: bytes | str) -> tuple[list[ParsedCategory], list[str]]:
    """Read and check an uploaded file. Returns (catalog, problems); use it only if problems is empty."""
    if isinstance(raw, bytes):
        if len(raw) > MAX_BYTES:
            return [], [f"The file is too large (over {MAX_BYTES // 1_000_000} MB)."]
        try:
            raw = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            return [], ["The file isn't UTF-8 text. Save it as UTF-8 JSON and try again."]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return [], [f"This isn't valid JSON: {exc.msg} at line {exc.lineno}, column {exc.colno}."]

    problems = _Problems()
    if not isinstance(data, dict) or not isinstance(data.get("sections"), list):
        return [], ["The file should be an object with a \"sections\" list: {\"sections\": [ ... ]}."]
    _unknown(data, {"sections", "version"}, "", problems)

    catalog: list[ParsedCategory] = []
    names: dict[str, str] = {}
    ids: dict[str, str] = {}
    count = 0
    for s_index, section in enumerate(data["sections"]):
        s_where = f"sections[{s_index}]"
        if not isinstance(section, dict):
            problems.add(s_where, "each section should be an object with \"name\" and \"items\".")
            continue
        _unknown(section, {"name", "items"}, s_where, problems)
        s_name = _text(section.get("name"), s_where, "name", problems, required=True, limit=120)
        s_where = s_name or s_where
        items_raw = section.get("items")
        if not isinstance(items_raw, list):
            problems.add(s_where, "\"items\" is required: a list of items.")
            continue
        parsed = ParsedCategory(s_name or "Menu")
        for i_index, raw_item in enumerate(items_raw):
            count += 1
            i_where = f"{s_where} → items[{i_index}]"
            if not isinstance(raw_item, dict):
                problems.add(i_where, "each item should be an object with \"name\" and \"price\".")
                continue
            _unknown(raw_item, _ITEM_KEYS, i_where, problems)
            name = _text(raw_item.get("name"), i_where, "name", problems, required=True, limit=160)
            if name:
                i_where = f"{s_where} → {name}"
            price = _cents(raw_item.get("price"), i_where, "price", problems, required=True)
            raw_id = raw_item.get("id")
            if isinstance(raw_id, int) and not isinstance(raw_id, bool):
                raw_id = str(raw_id)
            external_id = _text(raw_id, i_where, "id", problems, required=False, limit=80)
            item = ParsedItem(
                name=name or "", price_cents=price or 0, external_id=external_id,
                description=_text(raw_item.get("description"), i_where, "description", problems, required=False, limit=1000),
                aliases=_words(raw_item.get("aliases"), i_where, "aliases", problems),
                tags=_words(raw_item.get("tags"), i_where, "tags", problems, lower=True),
                available=_bool(raw_item.get("available"), i_where, "available", problems),
            )
            options = raw_item.get("options")
            if options is not None and not isinstance(options, list):
                problems.add(i_where, "\"options\" should be a list of option groups.")
                options = []
            for g_index, group in enumerate(options or []):
                parsed_group = _group(group, f"{i_where} → options[{g_index}]", problems)
                if parsed_group:
                    item.groups.append(parsed_group)
            if name:
                if name.lower() in names:
                    problems.add(i_where, f"\"{name}\" is already in {names[name.lower()]}. Each name must be unique.")
                names[name.lower()] = s_where
            if external_id:
                if external_id in ids:
                    problems.add(i_where, f"id \"{external_id}\" is also used by {ids[external_id]}.")
                ids[external_id] = i_where
            if name and price is not None:
                parsed.items.append(item)
        catalog.append(parsed)
    if count > MAX_ITEMS:
        problems.add("", f"The file has {count} items; the limit is {MAX_ITEMS}.")
    if count == 0 and not problems.items:
        problems.add("", "The file has no items.")
    return [c for c in catalog if c.items], problems.items


def _money(cents: int) -> float | int:
    return cents // 100 if cents % 100 == 0 else cents / 100


def catalog_to_json(categories: list, items: list) -> dict:
    """The current catalog (CatalogCategory / CatalogItem rows) in the upload format."""
    def item_json(i) -> dict:
        out: dict = {}
        if i.external_id:
            out["id"] = i.external_id
        out["name"], out["price"] = i.name, _money(i.price_cents)
        if i.description:
            out["description"] = i.description
        if i.aliases:
            out["aliases"] = list(i.aliases)
        if i.tags:
            out["tags"] = list(i.tags)
        if not i.is_available:
            out["available"] = False
        if i.modifier_groups:
            out["options"] = [{
                "name": g.name, "min": g.min_select, "max": g.max_select,
                "choices": [{"name": m.name, **({"price": _money(m.price_delta_cents)} if m.price_delta_cents else {}),
                             **({} if m.is_available else {"available": False})} for m in g.modifiers],
            } for g in i.modifier_groups]
        return out

    sections = [{"name": c.name, "items": [item_json(i) for i in items if i.category_id == c.id]} for c in categories]
    loose = [item_json(i) for i in items if i.category_id is None]
    if loose:
        sections.insert(0, {"name": "Other", "items": loose})
    return {"version": 1, "sections": [s for s in sections if s["items"]]}


@dataclass
class CatalogChanges:
    added: list[str] = field(default_factory=list)
    changed: list[tuple[str, list[str]]] = field(default_factory=list)  # (name, what changed)
    removed: list[str] = field(default_factory=list)
    unchanged: int = 0
    sections: int = 0
    total: int = 0


def preview_changes(current: list, catalog: list[ParsedCategory]) -> CatalogChanges:
    """What replacing the current items (CatalogItem rows) with this catalog would do.
    Items are matched by id when both have one, otherwise by name."""
    by_id = {i.external_id: i for i in current if i.external_id}
    by_name = {i.name.lower(): i for i in current}
    matched: set[int] = set()
    changes = CatalogChanges(sections=len(catalog))
    for section in catalog:
        for new in section.items:
            changes.total += 1
            old = by_id.get(new.external_id) if new.external_id else None
            old = old or by_name.get(new.name.lower())
            if old is None or old.id in matched:
                changes.added.append(new.name)
                continue
            matched.add(old.id)
            what = []
            if old.name != new.name:
                what.append(f"renamed from {old.name}")
            if old.price_cents != new.price_cents:
                what.append(f"price {old.price_cents / 100:.2f} → {new.price_cents / 100:.2f}")
            if old.is_available != new.available:
                what.append("now available" if new.available else "now unavailable")
            if (old.category.name if old.category else None) != section.name:
                what.append(f"moved to {section.name}")
            old_groups = [(g.name, g.min_select, g.max_select, [(m.name, m.price_delta_cents, m.is_available) for m in g.modifiers])
                          for g in old.modifier_groups]
            new_groups = [(g.name, g.min_select, g.max_select, list(g.options)) for g in new.groups]
            if old_groups != new_groups:
                what.append("options")
            if (old.description or None) != new.description or list(old.aliases or []) != new.aliases \
                    or list(old.tags or []) != new.tags or (old.external_id or None) != new.external_id:
                what.append("details")
            if what:
                changes.changed.append((new.name, what))
            else:
                changes.unchanged += 1
    changes.removed = [i.name for i in current if i.id not in matched]
    return changes
