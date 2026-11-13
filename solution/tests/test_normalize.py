from datetime import date
from decimal import Decimal

import pytest

from solution.normalize import (IST, mask_phone, normalize_header, normalize_phone, parse_date,
                                parse_decimal, parse_enum, parse_timestamp)


@pytest.mark.parametrize("raw, expected", [
    ("Mobile No", "mobile no"),
    ("  Insured_Name ", "insured name"),
    ("policy__no", "policy no"),
    ("Order\t ID", "order id"),
    ("preferred_language", "preferred language"),
    ("agent_notes", "agent notes"),          # matches no field -> ignored by the caller
    ("Order Date", "order date"),
])
def test_header(raw, expected):
    assert normalize_header(raw) == expected


@pytest.mark.parametrize("raw, expected", [
    ("9876543210", "+919876543210"),         # plain
    ("+91 98765 43210", "+919876543210"),    # spaced
    ("09876543210", "+919876543210"),        # leading 0 (11 digits)
    ("91-98765-43210", "+919876543210"),     # dashes (12 digits)
    ("(+91) 9876543210", "+919876543210"),   # brackets
    ("+919876543210", "+919876543210"),
    ("9198765432", "+919198765432"),         # bare 10-digit number starting 91 is valid (D9)
    ("9८76543210", None),                 # non-ASCII digit is not a digit
    ("5876543210", None),                    # starts with 5
    ("987654321", None),                     # 9 digits
    ("022-2345-6789", None),                 # landline: 11 digits, 0 dropped, starts with 2
    ("+1 415 555 0134", None),               # US number
    ("N/A", None),                           # no digits at all
    ("", None),
])
def test_phone(raw, expected):
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize("raw, expected", [
    ("2026-12-01", date(2026, 12, 1)),
    ("01/12/2026", date(2026, 12, 1)),       # day-first!
    ("01-12-2026", date(2026, 12, 1)),
    ("01-Dec-2026", date(2026, 12, 1)),
    ("01-DEC-2026", date(2026, 12, 1)),      # month name case-insensitive
    ("15-Sep-2026", date(2026, 9, 15)),
    ("29/02/2028", date(2028, 2, 29)),       # leap year
    ("29/02/2026", None),                    # not a leap year
    ("1/12/2026", None),                     # D24: zero-padded only
    ("01-December-2026", None),              # D24: 3-letter month only
    ("2026-12-01\n", None),                  # fullmatch: no trailing newline
    ("31/02/2026", None),                    # impossible
    ("2026-13-01", None),                    # month 13
    ("soon", None),
    ("TBD", None),
])
def test_date(raw, expected):
    assert parse_date(raw) == expected


@pytest.mark.parametrize("raw, expected", [
    ("12000", Decimal("12000.00")),
    ("12345.67", Decimal("12345.67")),
    ("1" + "0" * 30, Decimal("1" + "0" * 30 + ".00")),   # long number must not crash
    ("١٢", None),                            # non-ASCII digits
    ("₹27,554", Decimal("27554.00")),
    ("Rs. 26505", Decimal("26505.00")),
    ("rs26505", Decimal("26505.00")),
    ("INR 18678.3", Decimal("18678.30")),
    ("1,00,000", Decimal("100000.00")),      # Indian grouping
    ("10.005", Decimal("10.01")),            # 2 places, half-up
    ("-", None), ("N/A", None), ("-5", None), ("twelve thousand", None),
])
def test_decimal(raw, expected):
    assert parse_decimal(raw) == expected


LANGS = ["en", "hi", "ta", "kn", "te", "mr"]


@pytest.mark.parametrize("raw, expected", [
    ("ta", "ta"), ("HI", "hi"), (" kn ", "kn"), (" TA ", "ta"),
    ("Hindi", None), ("english", None), ("tamil", None),   # not in values -> warning + null
    ("", None),   # NOTE: the caller treats "" as MISSING (default, no warning) before calling this
])
def test_enum(raw, expected):
    assert parse_enum(raw, LANGS) == expected


def test_enum_schema_values_any_case():
    assert parse_enum("en", ["EN", "Hi"]) == "en"


def test_timestamps_all_mean_the_same_instant():
    utc = parse_timestamp("2026-10-03T04:31:00Z")
    off = parse_timestamp("2026-10-03T10:01:00+05:30")
    naive = parse_timestamp("2026-10-03 10:01:00")          # naive means IST
    assert utc == off == naive
    assert naive.astimezone(IST).isoformat() == "2026-10-03T10:01:00+05:30"


def test_ist_date_crosses_midnight():
    # 20:00 UTC on the 4th is 01:30 IST on the 5th -> the call belongs to the 5th
    ts = parse_timestamp("2026-10-04T20:00:00Z")
    assert ts.astimezone(IST).date() == date(2026, 10, 5)


def test_bad_timestamp():
    assert parse_timestamp("yesterday") is None
    assert parse_timestamp(None) is None


def test_mask():
    assert mask_phone("+919876543210") == "+91******3210"