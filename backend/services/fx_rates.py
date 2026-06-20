"""
services/fx_rates.py
═════════════════════
Currency exchange rate lookup for the "Convert my prices" Settings feature.

PLACEMENT: backend/services/fx_rates.py

PURPOSE:
  Businesses can change their store currency (USD → PLN, ZAR, etc.) in
  Settings, but the numeric price values on their products were never
  actually converted — changing currency only relabelled the same number
  with a new symbol (19.50 USD became "19.50 zł", not the real ~80 PLN
  equivalent).

  This module fetches a live mid-market exchange rate so the dashboard can
  show the business a per-product preview (old price → converted price)
  before they confirm anything. It NEVER writes to the database itself —
  see routes/business_routes.py POST /products/convert-currency, which
  applies the conversion only after the business has reviewed the preview
  and explicitly confirmed.

DATA SOURCE:
  frankfurter.app — free, no API key required, daily-updated mid-market
  rates sourced from the European Central Bank. Good enough for retail
  price-tagging; NOT suitable for financial trading or accounting purposes
  (clearly communicated to the business in the UI).

  Falls back to exchangerate-api.com's free open endpoint if frankfurter
  is unavailable, then to a cached last-known rate, then fails closed
  (returns None) — the frontend must handle "rate unavailable" by refusing
  to proceed rather than guessing a rate.

CACHING:
  Rates are cached in-memory for 24 hours per currency PAIR (not per
  business) — exchange rates don't change meaningfully more often than
  that for this use case, and this avoids hammering the free API tier.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

log = logging.getLogger("wazibot")

# In-memory cache: {"USD_PLN": (rate, fetched_at_epoch_seconds)}
_rate_cache: dict[str, tuple[float, float]] = {}
_CACHE_TTL_SECONDS = 24 * 60 * 60  # 24 hours


def get_exchange_rate(from_currency: str, to_currency: str) -> Optional[float]:
    """
    Return how many units of to_currency equal 1 unit of from_currency.
    e.g. get_exchange_rate("USD", "PLN") -> ~4.05

    Returns None if the rate could not be determined from any source —
    callers MUST treat None as "do not proceed", never assume 1.0.
    """
    from_currency = (from_currency or "").upper().strip()
    to_currency   = (to_currency or "").upper().strip()

    if not from_currency or not to_currency:
        return None
    if from_currency == to_currency:
        return 1.0

    cache_key = f"{from_currency}_{to_currency}"
    cached = _rate_cache.get(cache_key)
    if cached and (time.time() - cached[1]) < _CACHE_TTL_SECONDS:
        return cached[0]

    rate = _fetch_from_frankfurter(from_currency, to_currency)
    if rate is None:
        rate = _fetch_from_exchangerate_api(from_currency, to_currency)

    if rate is not None:
        _rate_cache[cache_key] = (rate, time.time())
        return rate

    # Both live sources failed — serve a stale cached rate rather than
    # nothing, but log loudly so this doesn't go unnoticed for weeks.
    if cached:
        log.warning(
            "fx_rates: live fetch failed for %s, serving stale cached rate "
            "from %.1f hours ago", cache_key, (time.time() - cached[1]) / 3600,
        )
        return cached[0]

    log.warning("fx_rates: no rate available for %s (all sources failed)", cache_key)
    return None


def _fetch_from_frankfurter(from_currency: str, to_currency: str) -> Optional[float]:
    # frankfurter.app doesn't cover crypto (BTC/USDT) or some pegged
    # currencies — that's fine, the caller falls through to the second
    # source or fails closed.
    try:
        import requests
        resp = requests.get(
            "https://api.frankfurter.app/latest",
            params={"from": from_currency, "to": to_currency},
            timeout=5,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        rate = (data.get("rates") or {}).get(to_currency)
        return float(rate) if rate is not None else None
    except Exception as exc:
        log.debug("fx_rates: frankfurter fetch failed: %s", exc)
        return None


def _fetch_from_exchangerate_api(from_currency: str, to_currency: str) -> Optional[float]:
    try:
        import requests
        resp = requests.get(
            f"https://open.er-api.com/v6/latest/{from_currency}",
            timeout=5,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        if data.get("result") != "success":
            return None
        rate = (data.get("rates") or {}).get(to_currency)
        return float(rate) if rate is not None else None
    except Exception as exc:
        log.debug("fx_rates: exchangerate-api fetch failed: %s", exc)
        return None
