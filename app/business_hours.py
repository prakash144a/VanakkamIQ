from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.db.models import Business

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_NAMES = {
    "mon": "Monday", "tue": "Tuesday", "wed": "Wednesday", "thu": "Thursday",
    "fri": "Friday", "sat": "Saturday", "sun": "Sunday",
}


def local_now(business: Business, now: datetime | None = None) -> datetime:
    tz = ZoneInfo(business.timezone)
    return now.astimezone(tz) if now else datetime.now(tz)


def closure_on(business: Business, day: date) -> dict | None:
    """The closure (holiday) covering this local date, if any."""
    iso = day.isoformat()
    return next((c for c in (business.closures or []) if c.get("date") == iso), None)


def is_open(business: Business, now: datetime | None = None) -> bool:
    local = local_now(business, now)
    hhmm = local.strftime("%H:%M")
    today = DAYS[local.weekday()]
    if closure_on(business, local.date()) is None:
        for start, end in (business.hours or {}).get(today, []):
            if start <= end:
                if start <= hhmm < end:
                    return True
            elif hhmm >= start:  # opened today, closes after midnight
                return True
    # Still open from yesterday's past-midnight hours (e.g. 18:00-02:00)?
    yesterday = local - timedelta(days=1)
    if closure_on(business, yesterday.date()) is None:
        for start, end in (business.hours or {}).get(DAYS[yesterday.weekday()], []):
            if start > end and hhmm < end:
                return True
    return False


def hours_text(business: Business) -> str:
    lines = []
    for day in DAYS:
        ranges = (business.hours or {}).get(day, [])
        label = ", ".join(f"{s}-{e}" for s, e in ranges) if ranges else "closed"
        lines.append(f"{DAY_NAMES[day]}: {label}")
    return "\n".join(lines)


def upcoming_closures(business: Business, now: datetime | None = None, days: int = 60) -> list[dict]:
    today = local_now(business, now).date()
    last = today + timedelta(days=days)
    return sorted(
        (c for c in (business.closures or []) if today.isoformat() <= c.get("date", "") <= last.isoformat()),
        key=lambda c: c["date"],
    )


def closures_text(business: Business, now: datetime | None = None) -> str:
    upcoming = upcoming_closures(business, now)
    if not upcoming:
        return ""
    return "\n".join(
        f"{date.fromisoformat(c['date']).strftime('%A %B %d')}: closed{' (' + c['note'] + ')' if c.get('note') else ''}"
        for c in upcoming
    )
