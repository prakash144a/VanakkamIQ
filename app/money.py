SYMBOLS = {"USD": "$", "CAD": "$", "EUR": "€", "GBP": "£", "INR": "₹"}


def format_money(cents: int, currency: str = "USD") -> str:
    symbol = SYMBOLS.get(currency, "")
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}{symbol}{cents // 100}.{cents % 100:02d}"
