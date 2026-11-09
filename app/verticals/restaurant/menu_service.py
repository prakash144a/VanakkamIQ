"""Menu lookup tuned for speech: callers say "chicken biriyani" or "the dosa", not exact names."""

import re
from difflib import SequenceMatcher

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Faq, MenuCategory, MenuItem, Modifier
from app.money import format_money


# Filler words callers say around dish names ("can I get the dosa please").
STOPWORDS = {
    "a", "an", "the", "some", "please", "of", "and", "with", "can", "could", "get", "have", "want",
    "like", "order", "one", "two", "three", "four", "five", "my", "your", "i", "id", "me",
}


def _norm(text: str) -> str:
    words = re.sub(r"[^a-z0-9 ]+", " ", text.lower()).split()
    kept = [w for w in words if w not in STOPWORDS]
    return " ".join(kept or words)


def _tokens(text: str) -> set[str]:
    return {t for t in _norm(text).split() if len(t) > 1}


def text_score(query: str, candidate: str) -> float:
    q, c = _norm(query), _norm(candidate)
    if not q or not c:
        return 0.0
    if q == c:
        return 1.0
    ratio = SequenceMatcher(None, q, c).ratio()
    q_tokens, c_tokens = _tokens(q), _tokens(c)
    overlap = len(q_tokens & c_tokens) / len(q_tokens) if q_tokens else 0.0
    # fuzzy token overlap handles "biriyani" vs "biryani"
    fuzzy = 0.0
    if q_tokens and c_tokens:
        fuzzy = sum(max(SequenceMatcher(None, qt, ct).ratio() for ct in c_tokens) for qt in q_tokens) / len(q_tokens)
    contains = 0.9 if (q in c or c in q) else 0.0
    return max(ratio, overlap, fuzzy * 0.95, contains)


async def load_menu(db: AsyncSession, business_id: int) -> list[MenuItem]:
    rows = await db.scalars(
        select(MenuItem)
        .outerjoin(MenuCategory, MenuItem.category_id == MenuCategory.id)
        .where(MenuItem.business_id == business_id)
        .order_by(MenuCategory.sort_order, MenuItem.sort_order, MenuItem.id)
    )
    return list(rows)


def search_items(items: list[MenuItem], query: str, limit: int = 5, min_score: float = 0.6) -> list[tuple[MenuItem, float]]:
    scored = []
    for item in items:
        names = [item.name, *(item.aliases or [])]
        if item.category is not None:
            names.append(f"{item.category.name} {item.name}")
        score = max(text_score(query, n) for n in names)
        if score >= min_score:
            scored.append((item, score))
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[:limit]


def options_view(item: MenuItem, currency: str) -> list[dict]:
    view = []
    for group in item.modifier_groups:
        view.append({
            "group": group.name,
            "required": group.min_select > 0,
            "choose_up_to": group.max_select,
            "options": [
                {"name": m.name, **({"extra": format_money(m.price_delta_cents, currency)} if m.price_delta_cents else {})}
                for m in group.modifiers if m.is_available
            ],
        })
    return view


def item_view(item: MenuItem, currency: str) -> dict:
    view = {
        "item_id": item.id,
        "name": item.name,
        "price": format_money(item.price_cents, currency),
        "available": item.is_available,
    }
    if item.description:
        view["description"] = item.description
    if item.tags:
        view["tags"] = item.tags
    options = options_view(item, currency)
    if options:
        view["options"] = options
    return view


def resolve_modifiers(item: MenuItem, requested: list[str]) -> tuple[list[Modifier], list[str]]:
    """Match the caller's spoken options to real modifiers. Returns (chosen, problems)."""
    chosen: list[Modifier] = []
    problems: list[str] = []
    all_mods = [(g, m) for g in item.modifier_groups for m in g.modifiers]
    for request in requested:
        best, best_score = None, 0.0
        for group, mod in all_mods:
            score = text_score(request, mod.name)
            if score > best_score:
                best, best_score = (group, mod), score
        if best is None or best_score < 0.6:
            names = ", ".join(m.name for _, m in all_mods if m.is_available) or "none"
            problems.append(f"'{request}' is not an option for {item.name}. Options: {names}.")
        elif not best[1].is_available:
            problems.append(f"{best[1].name} is not available right now.")
        elif best[1] not in chosen:
            chosen.append(best[1])
    for group in item.modifier_groups:
        count = sum(1 for m in chosen if m.group_id == group.id)
        names = ", ".join(m.name for m in group.modifiers if m.is_available)
        if count < group.min_select:
            problems.append(f"{item.name} needs a choice for {group.name} ({names}). Ask the caller.")
        if count > group.max_select:
            problems.append(f"Only {group.max_select} choice(s) allowed for {group.name}.")
    return chosen, problems


def menu_prompt_text(items: list[MenuItem], currency: str) -> str:
    lines: list[str] = []
    current = object()
    for item in items:
        category = item.category.name if item.category else "Other"
        if category != current:
            lines.append(f"\n{category}:")
            current = category
        status = "" if item.is_available else " (SOLD OUT today)"
        tags = f" [{', '.join(item.tags)}]" if item.tags else ""
        lines.append(f"- {item.name}: {format_money(item.price_cents, currency)}{tags}{status}")
        for group in item.modifier_groups:
            opts = ", ".join(
                m.name + (f" +{format_money(m.price_delta_cents, currency)}" if m.price_delta_cents else "")
                for m in group.modifiers if m.is_available
            )
            need = "required" if group.min_select else "optional"
            lines.append(f"    {group.name} ({need}): {opts}")
    return "\n".join(lines).strip()


def search_faqs(faqs: list[Faq], question: str, limit: int = 2, min_score: float = 0.5) -> list[Faq]:
    scored = [(f, text_score(question, f.question)) for f in faqs]
    scored = [pair for pair in scored if pair[1] >= min_score]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return [f for f, _ in scored[:limit]]
