"""
services/pricing_display.py: DISPLAY-ONLY local-currency estimates for the public pricing page (Stage 6).

Rules (approved decision)
  * Stripe prices and the billing currency (USD) are authoritative. Nothing here is ever used to compute a
    charge, to pick a Stripe price, or to validate a payment. billing/stripe_service.py does not import this.
  * A local-currency figure is only an ESTIMATE, always labelled as one, always next to the real USD price.
  * No rate is invented. Indicative rates must be supplied by the operator in the environment variable
    PRICING_DISPLAY_RATES (JSON, local units per 1 USD, with an as_of date). Without it, no estimate is returned
    and the page shows USD only.
  * Plan prices (Starter / Growth / Custom) are never changed here.
"""
from __future__ import annotations

import json
import os
import re

BILLING_CURRENCY = "USD"

# ISO 3166 country -> ISO 4217 currency for the markets we know. Unknown countries get no estimate.
COUNTRY_CURRENCY = {
    "GB": "GBP", "DE": "EUR", "FR": "EUR", "IT": "EUR", "ES": "EUR", "NL": "EUR", "BE": "EUR", "AT": "EUR",
    "IE": "EUR", "FI": "EUR", "PT": "EUR", "GR": "EUR", "PL": "PLN", "SE": "SEK", "NO": "NOK", "DK": "DKK",
    "CH": "CHF", "CZ": "CZK", "HU": "HUF", "RO": "RON", "CA": "CAD", "AU": "AUD", "NZ": "NZD", "JP": "JPY",
    "KR": "KRW", "SG": "SGD", "HK": "HKD", "AE": "AED", "SA": "SAR", "IL": "ILS", "TR": "TRY", "IN": "INR",
    "PK": "PKR", "BD": "BDT", "ID": "IDR", "MY": "MYR", "PH": "PHP", "TH": "THB", "VN": "VND", "CN": "CNY",
    "ZA": "ZAR", "NG": "NGN", "KE": "KES", "GH": "GHS", "UG": "UGX", "TZ": "TZS", "ZM": "ZMW", "ZW": "USD",
    "BW": "BWP", "MW": "MWK", "MZ": "MZN", "NA": "NAD", "RW": "RWF", "ET": "ETB", "EG": "EGP", "MA": "MAD",
    "BR": "BRL", "MX": "MXN", "AR": "ARS", "CL": "CLP", "CO": "COP", "PE": "PEN", "US": "USD",
}
ZERO_DECIMAL = {"JPY", "KRW", "VND", "UGX", "RWF", "CLP", "IDR", "HUF"}      # shown without minor units
_CODE = re.compile(r"^[A-Z]{3}$")
DISCLAIMER = ("Estimate only. Prices are set and charged in US dollars (USD); your bank or card issuer applies "
              "its own exchange rate, so the amount in your currency may differ.")
_PRICE_KEYS = ("starter_monthly", "starter_annual", "growth_monthly", "growth_annual")


def _load_rates() -> tuple[dict, str]:
    """({'GBP': 0.79, ...}, as_of) from PRICING_DISPLAY_RATES; ({}, '') when unset or malformed."""
    raw = os.getenv("PRICING_DISPLAY_RATES", "").strip()
    if not raw:
        return {}, ""
    try:
        data = json.loads(raw)
        rates = data.get("rates") if isinstance(data, dict) else None
        as_of = str((data or {}).get("as_of") or "")[:10]
        if not isinstance(rates, dict) or not re.match(r"^\d{4}-\d{2}-\d{2}$", as_of):
            return {}, ""
        clean = {}
        for k, v in rates.items():
            k = str(k).upper()
            if _CODE.match(k) and isinstance(v, (int, float)) and not isinstance(v, bool) and 0 < float(v) < 1e7:
                clean[k] = float(v)
        return clean, as_of
    except (ValueError, TypeError):
        return {}, ""


def currency_for_country(country) -> str | None:
    return COUNTRY_CURRENCY.get(str(country or "").strip().upper())


def local_estimate(country, pricing: dict) -> dict | None:
    """{'currency','rates_as_of','disclaimer','prices':{starter_monthly:..,...}} for a non-USD market with a
    configured indicative rate, else None. The USD amounts in `pricing` are never modified."""
    cur = currency_for_country(country)
    if not cur or cur == BILLING_CURRENCY:
        return None
    rates, as_of = _load_rates()
    rate = rates.get(cur)
    if not rate:
        return None
    digits = 0 if cur in ZERO_DECIMAL else 2
    prices = {}
    for k in _PRICE_KEYS:
        try:
            prices[k] = round(float(pricing[k]) * rate, digits)
        except (KeyError, TypeError, ValueError):
            return None
    return {"currency": cur, "rates_as_of": as_of, "estimate": True, "disclaimer": DISCLAIMER, "prices": prices}
