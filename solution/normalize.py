"""Pure normalisation functions. Each one implements one rule from
assignment-a-data/README.md ("Rules (the spec)" -> "Lead files" / "Event files").
No I/O, no database: easy to test, easy to defend."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, localcontext

IST = timezone(timedelta(hours=5, minutes=30))

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


def normalize_header(h: str) -> str:
    """lower-case, trim, collapse every run of '_' or whitespace into one space."""
    return re.sub(r"[_\s]+", " ", h.strip().lower())


def normalize_phone(raw: str | None) -> str | None:
    """Indian mobile -> '+91XXXXXXXXXX', or None if invalid."""
    if raw is None:
        return None
    digits = re.sub(r"[^0-9]", "", raw)          # ASCII digits only (\D would keep e.g. Devanagari digits)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10 and digits[0] in "6789":
        return "+91" + digits
    return None


_DATE_PATTERNS = [   # re.ASCII: \d means 0-9 only
    (re.compile(r"(\d{4})-(\d{2})-(\d{2})", re.ASCII), "ymd"),          # 2026-12-01
    (re.compile(r"(\d{2})/(\d{2})/(\d{4})", re.ASCII), "dmy"),          # 01/12/2026
    (re.compile(r"(\d{2})-(\d{2})-(\d{4})", re.ASCII), "dmy"),          # 01-12-2026
    (re.compile(r"(\d{2})-([A-Za-z]{3})-(\d{4})", re.ASCII), "dmony"),  # 01-Dec-2026
]


def parse_date(raw: str) -> date | None:
    """Day-first formats from the spec. Impossible dates (31/02) -> None."""
    for pattern, kind in _DATE_PATTERNS:
        m = pattern.fullmatch(raw)
        if not m:
            continue
        a, b, c = m.groups()
        try:
            if kind == "ymd":
                return date(int(a), int(b), int(c))
            if kind == "dmy":
                return date(int(c), int(b), int(a))
            month = _MONTHS.get(b.lower())
            return date(int(c), month, int(a)) if month else None
        except ValueError:          # e.g. month 13 or 31 Feb
            return None
    return None


_CURRENCY_PREFIX = re.compile(r"^(₹|rs\.?|inr)\s*", re.IGNORECASE)
_PLAIN_NUMBER = re.compile(r"\d+(\.\d+)?", re.ASCII)


def parse_decimal(raw: str) -> Decimal | None:
    """Optional ₹ / Rs / Rs. / INR prefix, commas are thousands separators, must be >= 0."""
    text = _CURRENCY_PREFIX.sub("", raw.strip()).replace(",", "")
    if not _PLAIN_NUMBER.fullmatch(text):   # rejects '-', 'N/A', '-5', 'twelve thousand'
        return None
    with localcontext() as ctx:             # enough precision for any length (default 28 digits would crash)
        ctx.prec = len(text) + 5
        return Decimal(text).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def parse_enum(raw: str, allowed: list[str]) -> str | None:
    """Case-insensitive on BOTH sides (schema values may be any case); stored lower-case."""
    value = raw.strip().lower()
    return value if value in {v.lower() for v in allowed} else None


def parse_timestamp(raw: str) -> datetime | None:
    """'...Z' (UTC), '...+05:30' (offset) or naive 'YYYY-MM-DD HH:MM:SS' (= IST).
    Returns an aware datetime, or None if unparseable."""
    if not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=IST) if dt.tzinfo is None else dt


def mask_phone(canonical: str) -> str:
    return "+91******" + canonical[-4:]