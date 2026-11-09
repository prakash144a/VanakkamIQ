from datetime import datetime
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


def is_open(business: Business, now: datetime | None = None) -> bool:
    local = local_now(business, now)
    day = DAYS[local.weekday()]
    hhmm = local.strftime("%H:%M")
    for start, end in (business.hours or {}).get(day, []):
        if start <= end:
            if start <= hhmm < end:
                return True
        elif hhmm >= start or hhmm < end:  # past midnight, e.g. 18:00-02:00
            return True
    return False


def hours_text(business: Business) -> str:
    lines = []
    for day in DAYS:
        ranges = (business.hours or {}).get(day, [])
        label = ", ".join(f"{s}-{e}" for s, e in ranges) if ranges else "closed"
        lines.append(f"{DAY_NAMES[day]}: {label}")
    return "\n".join(lines)
