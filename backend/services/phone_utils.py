"""
services/phone_utils.py: business-aware phone number normalisation (Phase 6, Stage 5b).

Rules
  * Output is the WhatsApp / E.164 form WITHOUT the plus: digits only, country code first (e.g. 447700900123).
  * Nothing is assumed about the country. The default dialing code comes from, in order: the business's own
    `features_json.default_dialing_code`, then the dialing code of the business's `country` (ISO 2-letter).
    With neither, only numbers that are already international (+ or 00 prefix, or too long to be national)
    are changed; anything else is returned untouched (None from normalise_phone).
  * Stored numbers and WhatsApp ids are never rewritten by this module; callers use it for new input, for
    comparing numbers, and for display.
"""
from __future__ import annotations

import re

# ISO 3166-1 alpha-2 -> international dialing code (digits). Countries sharing +1 use "1".
COUNTRY_DIAL = {
    "AE": "971", "AR": "54", "AT": "43", "AU": "61", "BD": "880", "BE": "32", "BR": "55", "BW": "267", "CA": "1",
    "CH": "41", "CL": "56", "CM": "237", "CN": "86", "CO": "57", "CZ": "420", "DE": "49", "DK": "45", "EG": "20",
    "ES": "34", "ET": "251", "FI": "358", "FR": "33", "GB": "44", "GH": "233", "GR": "30", "HK": "852", "HU": "36",
    "ID": "62", "IE": "353", "IL": "972", "IN": "91", "IT": "39", "JM": "1", "JP": "81", "KE": "254", "KR": "82",
    "LK": "94", "LS": "266", "MA": "212", "MU": "230", "MW": "265", "MX": "52", "MY": "60", "MZ": "258", "NA": "264",
    "NG": "234", "NL": "31", "NO": "47", "NZ": "64", "PE": "51", "PH": "63", "PK": "92", "PL": "48", "PT": "351",
    "RO": "40", "RW": "250", "SA": "966", "SE": "46", "SG": "65", "SZ": "268", "TH": "66", "TR": "90", "TZ": "255",
    "UA": "380", "UG": "256", "US": "1", "VN": "84", "ZA": "27", "ZM": "260", "ZW": "263",
}
_DIAL_OK = re.compile(r"^[1-9][0-9]{0,3}$")
_MIN, _MAX = 7, 15            # E.164: at most 15 digits


def clean_dialing_code(value) -> str | None:
    """'+263', '00263', ' 263 ' -> '263'; anything that is not a plausible code -> None."""
    d = re.sub(r"\D", "", str(value or ""))
    if d.startswith("00"):
        d = d[2:]
    return d if _DIAL_OK.match(d) else None


def dialing_code_for_country(country) -> str | None:
    return COUNTRY_DIAL.get(str(country or "").strip().upper())


def business_dialing_code(business: dict | None) -> str | None:
    """The business's default dialing code: its own setting first, then its country's. None if neither is known."""
    fj = (business or {}).get("features_json") or {}
    if not isinstance(fj, dict):
        return None
    return clean_dialing_code(fj.get("default_dialing_code")) or dialing_code_for_country(fj.get("country"))


def digits(value) -> str:
    return re.sub(r"\D", "", str(value or ""))


def normalise_phone(raw, default_dialing_code=None) -> str | None:
    """Digits-only international number, or None when it cannot be made one with confidence.

    '+44 7700 900123' -> '447700900123'      (already international)
    '0044 7700 900123' -> '447700900123'     ('00' international prefix)
    '07700 900123' + '44' -> '447700900123'  (national trunk '0' replaced by the default code)
    '771000111' + '263' -> '263771000111'    (bare national number)
    '263771000111' + '263' -> unchanged      (already starts with the code and is long enough)
    No default code and no international marker -> returned as digits only if 10-15 long, else None."""
    s = str(raw or "").strip()
    if not s:
        return None
    s = re.sub(r"\(0\)", "", s)                        # '+44 (0) 7700...' -> trunk zero shown in brackets
    explicit_plus = s.startswith("+")
    d = digits(s)
    dial = clean_dialing_code(default_dialing_code)
    if d.startswith("00") and not explicit_plus:
        d, explicit_plus = d[2:], True
    if explicit_plus:
        if d.startswith("0"):                            # '+0771...' is not an international number
            return None
        return d if _MIN <= len(d) <= _MAX else None
    if dial:
        if d.startswith("0"):
            d = dial + d.lstrip("0")
        elif not (d.startswith(dial) and len(d) - len(dial) >= 7 and len(d) >= 11):
            d = dial + d
        return d if _MIN <= len(d) <= _MAX else None
    # no default code: keep as it is when it already looks international (WhatsApp ids always are)
    return d if 10 <= len(d) <= _MAX and not d.startswith("0") else None


def same_number(a, b) -> bool:
    """True when two numbers are the same subscriber ignoring '+', spaces, dashes and brackets."""
    da, db = digits(a), digits(b)
    return bool(da) and da == db


def display_number(value) -> str:
    """Human-friendly international form ('+447700900123'); unparseable input is returned as it was."""
    d = digits(value)
    return f"+{d}" if _MIN <= len(d) <= _MAX else str(value or "")
