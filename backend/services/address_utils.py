"""
services/address_utils.py: country-aware postal-code and address helpers (Phase 6, Stage 5b).

Free-text delivery addresses stay free text (no schema change). This only (a) tidies whitespace, (b) knows the
LABEL and a light FORMAT CHECK for the postal code of the business's country, and (c) never rejects an address
for a country it knows nothing about, and never invents one (Zimbabwe, among others, has no postal-code system).
"""
from __future__ import annotations

import re

# country -> (label shown to customers, regex for a well-formed code or None when the country has no standard
# code / is not covered). Checks are deliberately lenient: they catch typos, they do not validate existence.
POSTAL = {
    "US": ("ZIP code", r"^\d{5}(-\d{4})?$"),
    "CA": ("postal code", r"^[A-Z]\d[A-Z] ?\d[A-Z]\d$"),
    "GB": ("postcode", r"^[A-Z]{1,2}\d[A-Z\d]? ?\d[A-Z]{2}$"),
    "IE": ("Eircode", r"^[A-Z]\d{2} ?[A-Z\d]{4}$"),
    "PL": ("postal code", r"^\d{2}-\d{3}$"),
    "DE": ("Postleitzahl", r"^\d{5}$"),
    "FR": ("code postal", r"^\d{5}$"),
    "ES": ("código postal", r"^\d{5}$"),
    "IT": ("CAP", r"^\d{5}$"),
    "NL": ("postcode", r"^\d{4} ?[A-Z]{2}$"),
    "PT": ("código postal", r"^\d{4}-\d{3}$"),
    "AU": ("postcode", r"^\d{4}$"),
    "NZ": ("postcode", r"^\d{4}$"),
    "ZA": ("postal code", r"^\d{4}$"),
    "NG": ("postal code", r"^\d{6}$"),
    "KE": ("postal code", r"^\d{5}$"),
    "IN": ("PIN code", r"^\d{6}$"),
    "BR": ("CEP", r"^\d{5}-?\d{3}$"),
    "MX": ("código postal", r"^\d{5}$"),
    "ZW": ("postal code", None),     # Zimbabwe has no postcode system: never required, never validated
    "GH": ("postal code", None),     # digital address codes exist but are not mandatory
    "UG": ("postal code", None),
    "TZ": ("postal code", r"^\d{5}$"),
    "ZM": ("postal code", r"^\d{5}$"),
}


def postal_label(country) -> str:
    return POSTAL.get(str(country or "").strip().upper(), ("postal code", None))[0]


def requires_postal_code(country) -> bool:
    """Only countries with a standard code are ever asked for one."""
    return bool(POSTAL.get(str(country or "").strip().upper(), (None, None))[1])


def normalise_postal_code(code, country) -> str | None:
    """Tidied code when it is well-formed for the country (upper case, single space where the format has one);
    None when it is not. Countries with no check accept any non-empty code as typed."""
    c = re.sub(r"\s+", " ", str(code or "")).strip()
    if not c:
        return None
    label_rx = POSTAL.get(str(country or "").strip().upper())
    if not label_rx or not label_rx[1]:
        return c
    c = c.upper()
    iso = str(country).strip().upper()
    compact = c.replace(" ", "")
    if not (re.match(label_rx[1], c) or re.match(label_rx[1], compact)):
        return None
    # canonical spacing for the formats that have one
    if iso == "GB":
        return compact[:-3] + " " + compact[-3:]
    if iso in ("CA", "IE"):
        return compact[:3] + " " + compact[3:]
    if iso == "NL":
        return compact[:4] + " " + compact[4:]
    return c if re.match(label_rx[1], c) else compact


def tidy_address(raw) -> str:
    """Whitespace only: trims, collapses runs of spaces/tabs and blank lines. The wording is never changed."""
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in str(raw or "").replace("\r", "").split("\n")]
    return "\n".join(ln for ln in lines if ln)


def address_prompt(country) -> str:
    """The question to ask for a delivery address, with the postal code only where the country uses one."""
    if requires_postal_code(country):
        return f"Please type your full delivery address, including your {postal_label(country)}."
    return "Please type your full delivery address (street, area and city)."


def address_hint(country) -> str:
    """The italic hint under the address prompt; mentions the postal code only where the country uses one."""
    if requires_postal_code(country):
        return f"Just type your full address (street, area, city and {postal_label(country)})."
    return "Just type your full address (street, suburb, city)."
