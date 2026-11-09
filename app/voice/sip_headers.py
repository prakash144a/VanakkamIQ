"""Pull phone numbers out of SIP headers.

Header values look like:  "Priya S" <sip:+15551234567@sip.telnyx.com>;tag=abc
SIP headers are caller-supplied metadata: use them for lookup only, never trust them for auth.
"""

import re

_URI_NUMBER = re.compile(r"(?:sips?|tel):\+?(\d{7,15})(?=[@;>\s]|$)")
_DISPLAY_NAME = re.compile(r'^\s*"([^"]+)"')


def normalize_e164(digits: str, default_country: str = "1") -> str:
    digits = re.sub(r"\D", "", digits)
    if len(digits) == 10 and default_country == "1":
        return f"+1{digits}"
    return f"+{digits}"


def header_values(headers: list[dict], name: str) -> list[str]:
    name = name.lower()
    return [h.get("value", "") for h in headers if str(h.get("name", "")).lower() == name]


def numbers_in(value: str) -> list[str]:
    found = [normalize_e164(m) for m in _URI_NUMBER.findall(value)]
    if not found:
        bare = re.sub(r"\D", "", value)
        if 10 <= len(bare) <= 15 and re.fullmatch(r"[\s+\-().\d]+", value.strip()):
            found = [normalize_e164(bare)]
    return found


def caller_number(headers: list[dict]) -> str | None:
    for name in ("From", "P-Asserted-Identity"):
        for value in header_values(headers, name):
            nums = numbers_in(value)
            if nums:
                return nums[0]
    return None


def caller_display_name(headers: list[dict]) -> str | None:
    for value in header_values(headers, "From"):
        m = _DISPLAY_NAME.match(value)
        if m and not re.fullmatch(r"[\s+\d\-()]+", m.group(1)):
            return m.group(1).strip()
    return None


def called_number_candidates(headers: list[dict], header_names: list[str]) -> list[str]:
    seen: list[str] = []
    for name in header_names:
        for value in header_values(headers, name):
            for num in numbers_in(value):
                if num not in seen:
                    seen.append(num)
    return seen
