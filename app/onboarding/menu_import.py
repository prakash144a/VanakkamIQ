"""Parse a menu pasted as plain text.

Format (one item per line):

    # Biryani
    Chicken Biryani | 15.99 | spicy | aka: chicken biriyani | Spice level*: Mild, Medium, Hot | Add-ons (up to 2): Extra raita +1.50, Boiled egg +2.00
    Vegetable Biryani | $13.99 | vegetarian

    # Drinks
    Mango Lassi | 4.99

- "# Name" starts a category.
- The first two fields are the item name and price. The rest are optional, in any order:
  - "aka: a, b"                 other ways callers say the dish (helps speech matching)
  - "Group: A, B +1.50"         an option group; "*" after the name means the caller must choose one;
                                "(up to N)" allows several choices
  - anything else               comma-separated tags, e.g. "vegetarian, spicy"
- Blank lines are ignored.
"""

import re
from dataclasses import dataclass, field

_MONEY = re.compile(r"^\$?\s*(\d{1,5})(?:\.(\d{1,2}))?$")
_GROUP_HEAD = re.compile(r"^(?P<name>[^:*()]+?)\s*(?P<req>\*)?\s*(?:\(\s*up to\s+(?P<max>\d+)\s*\))?\s*$", re.I)
_OPTION = re.compile(r"^(?P<name>.+?)(?:\s+\+\s*\$?(?P<price>\d{1,4}(?:\.\d{1,2})?))?$")


@dataclass
class ParsedGroup:
    name: str
    min_select: int
    max_select: int
    options: list[tuple[str, int]]  # (name, price delta in cents)


@dataclass
class ParsedItem:
    name: str
    price_cents: int
    tags: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    groups: list[ParsedGroup] = field(default_factory=list)


@dataclass
class ParsedCategory:
    name: str
    items: list[ParsedItem] = field(default_factory=list)


def parse_money(text: str) -> int | None:
    m = _MONEY.match(text.strip())
    if not m:
        return None
    cents = (m.group(2) or "0").ljust(2, "0")
    return int(m.group(1)) * 100 + int(cents)


def _parse_group(field_text: str, line_no: int, errors: list[str]) -> ParsedGroup | None:
    head, _, body = field_text.partition(":")
    m = _GROUP_HEAD.match(head)
    if not m or not body.strip():
        errors.append(f"Line {line_no}: couldn't read the option group \"{field_text}\".")
        return None
    options: list[tuple[str, int]] = []
    for raw in body.split(","):
        raw = raw.strip()
        if not raw:
            continue
        om = _OPTION.match(raw)
        price = parse_money(om.group("price")) if om and om.group("price") else 0
        options.append((om.group("name").strip() if om else raw, price or 0))
    if not options:
        errors.append(f"Line {line_no}: option group \"{head.strip()}\" has no options.")
        return None
    max_select = int(m.group("max")) if m.group("max") else 1
    min_select = 1 if m.group("req") else 0
    if max_select < 1 or max_select > len(options):
        errors.append(f"Line {line_no}: \"up to {max_select}\" doesn't fit the {len(options)} options in \"{head.strip()}\".")
        return None
    return ParsedGroup(m.group("name").strip(), min_select, max_select, options)


def parse_menu_text(text: str) -> tuple[list[ParsedCategory], list[str]]:
    categories: list[ParsedCategory] = []
    errors: list[str] = []
    seen_items: set[str] = set()

    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            name = line.lstrip("#").strip()
            if not name:
                errors.append(f"Line {line_no}: category name is missing after '#'.")
                continue
            categories.append(ParsedCategory(name))
            continue

        fields = [f.strip() for f in line.split("|")]
        if len(fields) < 2 or not fields[0]:
            errors.append(f"Line {line_no}: expected \"Item name | price\".")
            continue
        name, price_text = fields[0], fields[1]
        price = parse_money(price_text)
        if price is None:
            errors.append(f"Line {line_no}: \"{price_text}\" is not a price (use 12.99).")
            continue
        key = name.lower()
        if key in seen_items:
            errors.append(f"Line {line_no}: \"{name}\" appears more than once.")
            continue
        seen_items.add(key)

        item = ParsedItem(name=name, price_cents=price)
        for extra in fields[2:]:
            if not extra:
                continue
            if extra.lower().startswith("aka:"):
                item.aliases += [a.strip() for a in extra[4:].split(",") if a.strip()]
            elif ":" in extra:
                group = _parse_group(extra, line_no, errors)
                if group:
                    item.groups.append(group)
            else:
                item.tags += [t.strip().lower() for t in extra.split(",") if t.strip()]

        if not categories:
            categories.append(ParsedCategory("Menu"))
        categories[-1].items.append(item)

    categories = [c for c in categories if c.items]
    return categories, errors


def parse_faq_text(text: str) -> tuple[list[tuple[str, str]], list[str]]:
    """One FAQ per line:  Question? | Answer."""
    faqs: list[tuple[str, str]] = []
    errors: list[str] = []
    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        question, sep, answer = line.partition("|")
        if not sep or not question.strip() or not answer.strip():
            errors.append(f"FAQ line {line_no}: expected \"Question? | Answer\".")
            continue
        faqs.append((question.strip(), answer.strip()))
    return faqs, errors
