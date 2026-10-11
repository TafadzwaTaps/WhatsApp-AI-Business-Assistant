"""
billing/stripe_service.py — WaziBot Stripe SaaS Billing

DESIGN PRINCIPLES
─────────────────
• This module is a NEW layer, completely separate from existing payment flows.
• EcoCash / PayPal / Cash on existing orders are NEVER touched by this module.
• All functions are safe-by-default: if STRIPE_SECRET_KEY is not set, every
  function returns a safe no-op result rather than crashing.
• All business data goes through the existing `businesses` table — we extend
  it with non-breaking nullable columns (see BILLING_SCHEMA_SQL below).
• Feature flags control access — missing flag = free tier behaviour.

SUBSCRIPTION TIERS
──────────────────
  free       — 50 messages/day, 10 products, core ordering only
  pro        — unlimited messages, 100 products, campaigns, analytics, images
  business   — everything + multi-agent, API access, priority support
  enterprise — custom limits, white-label, dedicated onboarding

STRIPE OBJECTS
──────────────
  Customer     — one per WaziBot business (created on first checkout)
  Price        — recurring monthly/annual (set up in Stripe dashboard)
  Subscription — one active per customer
  Webhook      — checkout.session.completed, subscription.updated/deleted,
                 invoice.payment_failed
"""

from __future__ import annotations

import os
import logging
import warnings
from typing import Optional
warnings.filterwarnings("ignore", message=".*Accounts v2.*", category=UserWarning)

log = logging.getLogger(__name__)

# ── Tier definitions ──────────────────────────────────────────────────────────

TIERS: dict[str, dict] = {
    # ── Free (post-trial, limited) ────────────────────────────────────────────
    "free": {
        "label":               "Free",
        "price_monthly":       0,
        "price_annual":        0,
        "trial_days":          0,
        "messages_per_day":    30,
        "products_limit":      5,
        "campaigns":           False,
        "analytics":           False,
        "multi_agent":         False,
        "api_access":          False,
        "priority_support":    False,
        "ai_roles":            False,
        "catalog_images":      False,
        "broadcasts_per_month":0,
        "contacts_limit":      50,
        "stripe_price_id_monthly": "",
        "stripe_price_id_annual":  "",
    },
    # ── Trial (30-day, full access) ───────────────────────────────────────────
    "trial": {
        "label":               "Free Trial",
        "price_monthly":       0,
        "price_annual":        0,
        "trial_days":          30,
        "messages_per_day":    -1,
        "products_limit":      -1,
        "campaigns":           True,
        "analytics":           True,
        "multi_agent":         False,
        "api_access":          False,
        "priority_support":    False,
        "ai_roles":            True,
        "catalog_images":      True,
        "broadcasts_per_month":5,
        "contacts_limit":      -1,
        "stripe_price_id_monthly": "",
        "stripe_price_id_annual":  "",
    },
    # ── Starter — $5.99/month ─────────────────────────────────────────────────
    "starter": {
        "label":               "Starter",
        "price_monthly":       5.99,
        "price_annual":        57,      # ~$4.75/mo — save 20%
        "trial_days":          0,
        "messages_per_day":    200,
        "products_limit":      20,
        "campaigns":           False,
        "analytics":           False,
        "multi_agent":         False,
        "api_access":          False,
        "priority_support":    False,
        "ai_roles":            False,
        "catalog_images":      True,
        "broadcasts_per_month":2,
        "contacts_limit":      200,
        "stripe_price_id_monthly": os.getenv("STRIPE_PRICE_STARTER_MONTHLY", ""),
        "stripe_price_id_annual":  os.getenv("STRIPE_PRICE_STARTER_ANNUAL",  ""),
    },
    # ── Growth — $12/month (most popular) ────────────────────────────────────
    "growth": {
        "label":               "Growth",
        "price_monthly":       12,
        "price_annual":        115,
        "trial_days":          0,
        "messages_per_day":    -1,
        "products_limit":      -1,
        "campaigns":           True,
        "analytics":           True,
        "multi_agent":         False,
        "api_access":          False,
        "priority_support":    False,
        "ai_roles":            True,
        "catalog_images":      True,
        "broadcasts_per_month":-1,
        "contacts_limit":      -1,
        "stripe_price_id_monthly": os.getenv("STRIPE_PRICE_GROWTH_MONTHLY", ""),
        "stripe_price_id_annual":  os.getenv("STRIPE_PRICE_GROWTH_ANNUAL",  ""),
    },
    # ── Enterprise — custom pricing ───────────────────────────────────────────
    "enterprise": {
        "label":               "Enterprise",
        "price_monthly":       0,
        "price_annual":        0,
        "trial_days":          0,
        "messages_per_day":    -1,
        "products_limit":      -1,
        "campaigns":           True,
        "analytics":           True,
        "multi_agent":         True,
        "api_access":          True,
        "priority_support":    True,
        "ai_roles":            True,
        "catalog_images":      True,
        "broadcasts_per_month":-1,
        "contacts_limit":      -1,
        "stripe_price_id_monthly": "",
        "stripe_price_id_annual":  "",
    },
    # Legacy aliases (kept for backward compat with webhooks that stored 'pro'/'business')
    "pro": {
        "label": "Growth", "price_monthly": 12, "price_annual": 115,
        "trial_days": 0, "messages_per_day": -1, "products_limit": -1,
        "campaigns": True, "analytics": True, "multi_agent": False,
        "api_access": False, "priority_support": False, "ai_roles": True,
        "catalog_images": True, "broadcasts_per_month": -1, "contacts_limit": -1,
        "stripe_price_id_monthly": os.getenv("STRIPE_PRICE_GROWTH_MONTHLY", ""),
        "stripe_price_id_annual":  os.getenv("STRIPE_PRICE_GROWTH_ANNUAL",  ""),
    },
    "business": {
        "label": "Growth", "price_monthly": 12, "price_annual": 115,
        "trial_days": 0, "messages_per_day": -1, "products_limit": -1,
        "campaigns": True, "analytics": True, "multi_agent": True,
        "api_access": True, "priority_support": True, "ai_roles": True,
        "catalog_images": True, "broadcasts_per_month": -1, "contacts_limit": -1,
        "stripe_price_id_monthly": os.getenv("STRIPE_PRICE_BUSINESS_MONTHLY", ""),
        "stripe_price_id_annual":  os.getenv("STRIPE_PRICE_BUSINESS_ANNUAL",  ""),
    },
}



# ═══════════════════════════════════════════════════════════════════════════════
# REGIONAL PRICING CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────
# Plans and features are IDENTICAL across all regions.
# Only the displayed price and Stripe checkout amount differ.
# To add a country: add it to the appropriate PRICING_REGIONS tier.
# To add a region tier: add a new entry to PRICING_REGIONS and
#   create matching Stripe Price objects + env vars.
#
# Env vars per region tier:
#   STRIPE_PRICE_STARTER_MONTHLY_B / _C     (region B/C monthly)
#   STRIPE_PRICE_STARTER_ANNUAL_B  / _C     (region B/C annual)
#   STRIPE_PRICE_GROWTH_MONTHLY_B  / _C
#   STRIPE_PRICE_GROWTH_ANNUAL_B   / _C
# ═══════════════════════════════════════════════════════════════════════════════

PRICING_REGIONS: dict[str, dict] = {
    # ── Tier A — Global / High-income markets ─────────────────────────────────
    "A": {
        "label":          "Global",
        "countries":      {
            "US","CA","GB","DE","AU","NZ","SE","NO","DK","FI","NL","BE",
            "AT","CH","IE","FR","IT","ES","JP","SG","HK","AE","IL","KR",
        },
        "starter_monthly": 5.99,
        "starter_annual":  57,
        "growth_monthly":  12,
        "growth_annual":   115,
        "price_ids": {
            "starter_monthly": os.getenv("STRIPE_PRICE_STARTER_MONTHLY",   ""),
            "starter_annual":  os.getenv("STRIPE_PRICE_STARTER_ANNUAL",    ""),
            "growth_monthly":  os.getenv("STRIPE_PRICE_GROWTH_MONTHLY",    ""),
            "growth_annual":   os.getenv("STRIPE_PRICE_GROWTH_ANNUAL",     ""),
        },
    },
    # ── Tier B — Mid-income markets ───────────────────────────────────────────
    "B": {
        "label":          "Regional",
        "countries":      {
            "PL","RO","PT","HR","SK","SI","CZ","HU","GR","BG","RS","UA",
            "MY","TH","ID","PH","BR","MX","AR","CO","CL","TR","ZA","EG",
            "MA","TN","VN",
        },
        "starter_monthly": 4.99,
        "starter_annual":  48,
        "growth_monthly":  10,
        "growth_annual":   96,
        "price_ids": {
            "starter_monthly": os.getenv("STRIPE_PRICE_STARTER_MONTHLY_B", os.getenv("STRIPE_PRICE_STARTER_MONTHLY", "")),
            "starter_annual":  os.getenv("STRIPE_PRICE_STARTER_ANNUAL_B",  os.getenv("STRIPE_PRICE_STARTER_ANNUAL",  "")),
            "growth_monthly":  os.getenv("STRIPE_PRICE_GROWTH_MONTHLY_B",  os.getenv("STRIPE_PRICE_GROWTH_MONTHLY",  "")),
            "growth_annual":   os.getenv("STRIPE_PRICE_GROWTH_ANNUAL_B",   os.getenv("STRIPE_PRICE_GROWTH_ANNUAL",   "")),
        },
    },
    # ── Tier C — Emerging markets ─────────────────────────────────────────────
    "C": {
        "label":          "Emerging",
        "countries":      {
            "ZW","ZM","ZA","KE","UG","TZ","NG","GH","ET","RW","SN","CI",
            "IN","PK","BD","LK","NP","MM","KH","LA","AF",
        },
        "starter_monthly": 3.99,
        "starter_annual":  38,
        "growth_monthly":  8.99,
        "growth_annual":   86,
        "price_ids": {
            "starter_monthly": os.getenv("STRIPE_PRICE_STARTER_MONTHLY_C", os.getenv("STRIPE_PRICE_STARTER_MONTHLY", "")),
            "starter_annual":  os.getenv("STRIPE_PRICE_STARTER_ANNUAL_C",  os.getenv("STRIPE_PRICE_STARTER_ANNUAL",  "")),
            "growth_monthly":  os.getenv("STRIPE_PRICE_GROWTH_MONTHLY_C",  os.getenv("STRIPE_PRICE_GROWTH_MONTHLY",  "")),
            "growth_annual":   os.getenv("STRIPE_PRICE_GROWTH_ANNUAL_C",   os.getenv("STRIPE_PRICE_GROWTH_ANNUAL",   "")),
        },
    },
}

# Reverse lookup: country code → region tier key
_COUNTRY_TO_REGION: dict[str, str] = {}
for _rk, _rv in PRICING_REGIONS.items():
    for _cc in _rv["countries"]:
        _COUNTRY_TO_REGION[_cc.upper()] = _rk


def get_pricing_for_country(country_code: str) -> dict:
    """
    Return the pricing region dict for a given ISO-3166-1 alpha-2 country code.
    Falls back to Tier A (global pricing) if country not found.
    Never raises — pricing failures should never break checkout.
    """
    region_key = _COUNTRY_TO_REGION.get((country_code or "").strip().upper(), "A")
    region = PRICING_REGIONS.get(region_key, PRICING_REGIONS["A"])
    return {
        "region":          region_key,
        "region_label":    region["label"],
        "starter_monthly": region["starter_monthly"],
        "starter_annual":  region["starter_annual"],
        "growth_monthly":  region["growth_monthly"],
        "growth_annual":   region["growth_annual"],
        "price_ids":       region["price_ids"],
    }


def get_price_id_for_checkout(tier: str, billing_period: str, country_code: str = "") -> str:
    """
    Return the Stripe Price ID for a given tier + period + country.
    Prefers regional price ID; falls back to global price ID.
    Falls back gracefully — never raises.
    """
    try:
        region = get_pricing_for_country(country_code)
        key    = f"{tier}_{billing_period}"   # e.g. "starter_monthly"
        pid    = region["price_ids"].get(key, "")
        if pid:
            return pid
    except Exception:
        pass
    # Ultimate fallback: global TIERS dict
    tier_data = TIERS.get(tier, {})
    return tier_data.get(f"stripe_price_id_{billing_period}", "")


_stripe_module = None

def _stripe():
    global _stripe_module
    key = os.getenv("STRIPE_SECRET_KEY", "")
    if not key:
        return None
    if _stripe_module is not None:
        _stripe_module.api_key = key
        return _stripe_module
    try:
        import stripe as _s
        _s.api_key             = key
        _s.api_version         = "2024-06-20"
        _s.max_network_retries = 3
        _stripe_module = _s
        try:
            for problem in billing_config_check()["problems"]:
                log.warning("Stripe config: %s", problem)
        except Exception:
            pass
        return _s
    except ImportError:
        log.warning("stripe package not installed — add stripe>=7.0.0 to requirements.txt")
        return None


# ── Customer management ───────────────────────────────────────────────────────

def get_or_create_stripe_customer(
    business_id: int,
    business_name: str,
    owner_email: str,
) -> Optional[str]:
    """
    Return existing Stripe customer_id for a business, or create one.
    Returns None (safe) if Stripe unavailable.
    """
    stripe = _stripe()
    if not stripe:
        return None
    try:
        from core.db import supabase
        res = (
            supabase.table("businesses")
            .select("stripe_customer_id")
            .eq("id", business_id)
            .limit(1)
            .execute()
        )
        existing = (res.data or [{}])[0].get("stripe_customer_id")
        if existing:
            return existing

        customer = stripe.Customer.create(
            email=owner_email,
            name=business_name,
            metadata={"wazibot_business_id": str(business_id)},
            # Concurrent first-time checkouts must not create two customers.
            idempotency_key=f"cust_biz_{business_id}",
        )
        cid = customer["id"]

        try:
            supabase.table("businesses").update(
                {"stripe_customer_id": cid}
            ).eq("id", business_id).execute()
        except Exception as exc:
            log.debug("stripe_customer_id column may not exist yet: %s", exc)

        log.info("Stripe customer created  business=%s  customer=%s", business_id, cid)
        return cid
    except Exception as exc:
        log.error("get_or_create_stripe_customer error: %s", exc)
        return None


# ── Billing hardening helpers ─────────────────────────────────────────────────
# Stripe is the source of truth for subscription state; WaziBot owns the
# business↔customer mapping. Everything below derives local state from a
# freshly-retrieved Stripe object rather than trusting browser input or the
# (possibly stale / out-of-order) webhook payload.

PURCHASABLE_TIERS = ("starter", "growth")          # enterprise is custom / sales-led
_PERIOD_ALIASES = {"monthly": "monthly", "month": "monthly",
                   "annual": "annual", "yearly": "annual", "year": "annual"}
_TIER_RANK = {"starter": 1, "growth": 2}
_LIVE_SUB_STATUSES = ("active", "trialing", "past_due")

# Stripe subscription status → businesses.billing_status. "canceled" keeps the
# spelling the rest of WaziBot already uses ("cancelled"). Every other value is
# the real Stripe status — no invented states.
_STRIPE_TO_LOCAL_STATUS = {
    "active": "active", "trialing": "trialing", "past_due": "past_due",
    "unpaid": "unpaid", "canceled": "cancelled", "paused": "paused",
}

_LEGACY_BIZ_COLS = ("name, owner_email, subscription_tier, billing_status, trial_ends_at, "
                    "stripe_customer_id, stripe_subscription_id")
# Added by backend/migrations/billing_lifecycle_hardening.sql. All code that
# touches these tolerates the migration not having been applied yet.
_BILLING_EXT_COLS = (
    "stripe_price_id", "billing_interval", "current_period_start", "current_period_end",
    "cancel_at_period_end", "canceled_at", "stripe_subscription_status",
    "payment_failed_at", "stripe_last_invoice_id", "billing_synced_at",
)


def _g(obj, key, default=None):
    """Read a field from a dict OR a StripeObject (never raises)."""
    if obj is None:
        return default
    try:
        v = obj[key]
    except (KeyError, TypeError, AttributeError, IndexError):
        v = getattr(obj, key, None)
    return default if v is None else v


def _key_mode() -> str:
    """'live' | 'test' | 'unknown' — derived from the key prefix only (key never logged)."""
    key = os.getenv("STRIPE_SECRET_KEY", "")
    if key.startswith(("sk_live_", "rk_live_")):
        return "live"
    if key.startswith(("sk_test_", "rk_test_")):
        return "test"
    return "unknown"


def _past_due_grace_days() -> int:
    try:
        return max(0, int(os.getenv("BILLING_PAST_DUE_GRACE_DAYS", "3")))
    except ValueError:
        return 3


def _iso(ts) -> Optional[str]:
    from datetime import datetime, timezone
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat() if ts else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _normalize_period(period: str) -> Optional[str]:
    return _PERIOD_ALIASES.get((period or "").strip().lower())


def _price_index() -> dict:
    """price_id → (tier, period) for every price this deployment has configured.
    Built from env at call time so a Price ID can never be 'accepted' unless the
    operator configured it."""
    idx: dict = {}

    def add(pid, tier, period):
        if pid:
            idx.setdefault(pid, (tier, period))

    for tier in ("starter", "growth", "business"):
        for period, suffix in (("monthly", "MONTHLY"), ("annual", "ANNUAL")):
            for region in ("", "_B", "_C"):
                add(os.getenv(f"STRIPE_PRICE_{tier.upper()}_{suffix}{region}", ""), tier, period)
    for tier in ("starter", "growth", "business"):
        data = TIERS.get(tier, {})
        add(data.get("stripe_price_id_monthly"), tier, "monthly")
        add(data.get("stripe_price_id_annual"), tier, "annual")
    for region in PRICING_REGIONS.values():
        for key, pid in region["price_ids"].items():
            t, p = key.rsplit("_", 1)
            add(pid, t, p)
    return idx


def _safe_redirect(url: str, default: str) -> str:
    """Only allow redirect URLs on the configured WaziBot origin (no open redirects)."""
    from urllib.parse import urlparse
    if not url:
        return default
    try:
        u, b = urlparse(url), urlparse(os.getenv("WAZIBOT_URL", "https://wazibothq.com"))
        if u.scheme in ("http", "https") and u.netloc and u.scheme == b.scheme and u.netloc == b.netloc:
            return url
    except Exception:
        pass
    log.warning("Rejected non-WaziBot redirect URL in billing request")
    return default


def _load_biz(bid: int) -> dict:
    """Business billing row. Falls back to legacy columns if the billing
    migration hasn't been applied. Raises on DB outage (callers must not treat
    an unreadable row as 'unpaid' or 'paid')."""
    from core.db import supabase
    try:
        res = (supabase.table("businesses")
               .select(_LEGACY_BIZ_COLS + ", " + ", ".join(_BILLING_EXT_COLS))
               .eq("id", bid).limit(1).execute())
    except Exception:
        res = (supabase.table("businesses").select(_LEGACY_BIZ_COLS)
               .eq("id", bid).limit(1).execute())
    return (res.data or [{}])[0]


def _verify_price(stripe, price_id: str, tier: str, period: str) -> Optional[str]:
    """Return an error string if the Price is not a usable recurring price for
    this plan/interval in the environment the secret key belongs to."""
    import time
    cache = _verify_price.__dict__.setdefault("_cache", {})
    hit = cache.get((price_id, tier, period))
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    try:
        p = stripe.Price.retrieve(price_id)
    except Exception as exc:
        log.error("price verification failed  tier=%s period=%s err=%s", tier, period, type(exc).__name__)
        return "Could not verify pricing with Stripe — please try again shortly"
    err = None
    rec = _g(p, "recurring")
    want = "month" if period == "monthly" else "year"
    if not _g(p, "active", False):
        err = "This plan is not currently available"
    elif not rec or _g(rec, "interval") != want or int(_g(rec, "interval_count", 1)) != 1:
        err = "Plan price is misconfigured (billing interval mismatch)"
    else:
        mode, live = _key_mode(), _g(p, "livemode")
        if (mode == "live" and live is False) or (mode == "test" and live is True):
            err = "Plan price belongs to a different Stripe environment than the configured key"
    if not err:
        if (_g(p, "currency", "") or "").lower() != "usd":
            log.warning("price currency is not USD  tier=%s period=%s", tier, period)
        expected = TIERS.get(tier, {}).get("price_monthly" if period == "monthly" else "price_annual")
        amt = _g(p, "unit_amount")
        if expected and amt is not None and abs(float(amt) / 100.0 - float(expected)) > 0.01:
            log.warning("price amount differs from TIERS display price  tier=%s period=%s stripe=%s display=%s",
                        tier, period, float(amt) / 100.0, expected)
    cache[(price_id, tier, period)] = (time.time(), err)
    return err


def _resolve_price_id(tier: str, period: str, override: str = "") -> tuple:
    """(price_id, error). The price always comes from server config; an
    override is accepted only if it is itself a configured price for exactly
    this tier+period."""
    idx = _price_index()
    override = (override or "").strip()
    if override:
        if idx.get(override) == (tier, period):
            return override, None
        log.warning("Rejected price override not in server allowlist  tier=%s period=%s", tier, period)
    pid = (TIERS.get(tier, {}).get(f"stripe_price_id_{period}", "")
           or os.getenv(f"STRIPE_PRICE_{tier.upper()}_{period.upper()}", ""))
    if not pid:
        return None, f"No Stripe price configured for {tier}/{period}"
    return pid, None


def _verified_customer_id(stripe, bid: int) -> tuple:
    """(customer_id, error). A stored customer is only trusted if Stripe says it
    carries THIS business's id in its metadata (set server-side at creation)."""
    cid = _load_biz(bid).get("stripe_customer_id")
    if not cid:
        return None, None
    try:
        c = stripe.Customer.retrieve(cid)
    except Exception as exc:
        if getattr(exc, "code", None) == "resource_missing":
            log.error("stored Stripe customer not found  business=%s", bid)
            return None, "Billing profile could not be verified — contact support"
        raise
    owner = _g(_g(c, "metadata"), "wazibot_business_id")
    if _g(c, "deleted", False) or owner != str(bid):
        log.error("Stripe customer mapping mismatch  business=%s", bid)
        return None, "Billing profile could not be verified — contact support"
    return cid, None


def _live_subscriptions(stripe, cid: str) -> list:
    subs = stripe.Subscription.list(customer=cid, status="all", limit=10)
    return [s for s in (_g(subs, "data", []) or []) if _g(s, "status") in _LIVE_SUB_STATUSES]


def billing_config_check() -> dict:
    """Non-sensitive readiness report for admins (never returns key material)."""
    mode = _key_mode()
    idx = _price_index()
    have = {f"{t}_{p}": any(v == (t, p) for v in idx.values())
            for t in PURCHASABLE_TIERS for p in ("monthly", "annual")}
    problems = []
    if mode == "unknown":
        problems.append("STRIPE_SECRET_KEY missing or not a recognised sk_/rk_ key")
    if not os.getenv("STRIPE_WEBHOOK_SECRET"):
        problems.append("STRIPE_WEBHOOK_SECRET missing")
    problems += [f"no Stripe price configured for {k}" for k, ok in have.items() if not ok]
    base = os.getenv("WAZIBOT_URL", "")
    if not base:
        problems.append("WAZIBOT_URL not set (default https://wazibothq.com is used)")
    if mode == "live" and base.startswith("http://"):
        problems.append("WAZIBOT_URL is not https while using a live key")
    return {"key_mode": mode, "webhook_secret_set": bool(os.getenv("STRIPE_WEBHOOK_SECRET")),
            "prices_configured": have, "past_due_grace_days": _past_due_grace_days(),
            "problems": problems, "ok": not problems}


# ── Checkout session ──────────────────────────────────────────────────────────

def create_checkout_session(
    business_id:       int,
    tier:              str,
    billing_period:    str = "monthly",
    success_url:       str = "",
    cancel_url:        str = "",
    customer_email:    str = "",
    price_id_override: str = "",    # regional price ID — must be in the server allowlist
) -> dict:
    """
    Create a Stripe Checkout Session for a subscription upgrade.
    Returns {"url", "session_id"} on success, {"error", "code"?} on failure.
    Paid access is NOT granted here — only by a verified webhook / Stripe sync.
    """
    import hashlib, json, time
    stripe = _stripe()
    if not stripe:
        return {"error": "Stripe not configured — add STRIPE_SECRET_KEY to env"}

    tier = (tier or "").strip().lower()
    period = _normalize_period(billing_period)
    if tier == "enterprise":
        return {"error": "Enterprise pricing is custom — please contact sales", "code": "enterprise_contact_sales"}
    if tier not in PURCHASABLE_TIERS:
        return {"error": f"Unknown tier: {tier}"}
    if not period:
        return {"error": "Invalid billing period"}

    price_id, perr = _resolve_price_id(tier, period, price_id_override)
    if perr:
        return {"error": perr + ". Add the matching STRIPE_PRICE_* env var."}

    try:
        verr = _verify_price(stripe, price_id, tier, period)
        if verr:
            return {"error": verr}

        row = _load_biz(business_id)
        if not row:
            return {"error": "Business not found"}

        # One Stripe customer per business, created once and verified.
        cid, cerr = _verified_customer_id(stripe, business_id)
        if cerr:
            return {"error": cerr}
        if not cid:
            cid = get_or_create_stripe_customer(
                business_id, row.get("name") or "", customer_email or row.get("owner_email") or "")
            if not cid:
                return {"error": "Could not create billing profile — please try again"}

        # Authoritative duplicate-subscription guard (asks Stripe, not our cache).
        if _live_subscriptions(stripe, cid):
            return {"error": "You already have a subscription. Use Manage Subscription to change or cancel it.",
                    "code": "already_subscribed"}

        base = os.getenv("WAZIBOT_URL", "https://wazibothq.com")
        success_url = _safe_redirect(success_url, f"{base}/billing/success?session_id={{CHECKOUT_SESSION_ID}}")
        cancel_url  = _safe_redirect(cancel_url,  f"{base}/billing/cancel")
        meta = {"wazibot_business_id": str(business_id), "tier": tier, "billing_period": period}

        params: dict = {
            "mode":              "subscription",
            "customer":          cid,
            "client_reference_id": str(business_id),
            "line_items":        [{"price": price_id, "quantity": 1}],
            "success_url":       success_url,
            "cancel_url":        cancel_url,
            "metadata":          meta,
            "subscription_data": {"metadata": meta},
            # Checkout Studio parameters. NOTE: ui_mode / branding_settings /
            # origin_context / integration_identifier are intentionally NOT sent:
            # this module pins api_version 2024-06-20, which rejects them (see
            # STRIPE_INTEGRATION_TODO.md).
            "billing_address_collection": "auto",
            "phone_number_collection":   {"enabled": True},
            "automatic_tax":             {"enabled": False},
            "allow_promotion_codes":     True,
            "payment_method_collection": "always",
        }
        # Same params inside the same 5-minute window → same Stripe session
        # (double-clicks, retries, refreshes). Different params never collide.
        digest = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:24]
        idem = f"co_{business_id}_{digest}_{int(time.time() // 300)}"
        session = stripe.checkout.Session.create(**params, idempotency_key=idem)
        log.info("Checkout session created  business=%s  tier=%s  period=%s", business_id, tier, period)
        return {"url": _g(session, "url"), "session_id": _g(session, "id")}
    except Exception as exc:
        log.error("create_checkout_session error  business=%s  err=%s", business_id, type(exc).__name__)
        return {"error": "Could not start checkout — please try again"}


def _owned_subscription(stripe, business_id: int) -> tuple:
    """(subscription_object, error, code) for the business's own subscription,
    retrieved from Stripe and checked against the trusted customer mapping."""
    row = _load_biz(business_id)
    sub_id = row.get("stripe_subscription_id")
    if not sub_id:
        return None, "No active subscription found", "no_subscription"
    sub = stripe.Subscription.retrieve(sub_id)
    stored = row.get("stripe_customer_id")
    cust = _g(sub, "customer")
    cust = _g(cust, "id", cust) if not isinstance(cust, str) else cust
    if stored and cust and stored != cust:
        log.error("subscription/customer mismatch  business=%s", business_id)
        return None, "Billing profile could not be verified — contact support", "mismatch"
    return sub, None, None


def cancel_subscription(business_id: int) -> dict:
    """Cancel at period end. Access continues until the paid period ends; the
    local record is synced from Stripe's response, not a local boolean."""
    stripe = _stripe()
    if not stripe:
        return {"error": "Stripe not configured"}
    try:
        sub, err, code = _owned_subscription(stripe, business_id)
        if err:
            return {"error": err, "code": code}
        if _g(sub, "status") not in _LIVE_SUB_STATUSES:
            return {"error": "No active subscription found", "code": "no_subscription"}
        sub = stripe.Subscription.modify(_g(sub, "id"), cancel_at_period_end=True)
        res = _sync_subscription(sub, source="api_cancel")
        _send_lifecycle_emails(res)
        end = _iso(_period_end(sub))
        log.info("Subscription cancellation scheduled  business=%s  sub=%s", business_id, _g(sub, "id"))
        return {"ok": True, "cancelled_at_period_end": True, "cancel_at_period_end": True,
                "access_until": end, "synced": res.get("result") == "synced"}
    except Exception as exc:
        log.error("cancel_subscription error  business=%s  err=%s", business_id, type(exc).__name__)
        return {"error": "Could not cancel — please try again or use Manage Subscription"}


def resume_subscription(business_id: int) -> dict:
    """Undo a scheduled end-of-period cancellation (only valid before it takes effect)."""
    stripe = _stripe()
    if not stripe:
        return {"error": "Stripe not configured"}
    try:
        sub, err, code = _owned_subscription(stripe, business_id)
        if err:
            return {"error": err, "code": code}
        if _g(sub, "status") not in _LIVE_SUB_STATUSES or not _g(sub, "cancel_at_period_end", False):
            return {"error": "No scheduled cancellation to undo", "code": "not_cancelling"}
        sub = stripe.Subscription.modify(_g(sub, "id"), cancel_at_period_end=False)
        _sync_subscription(sub, source="api_resume")
        return {"ok": True, "cancel_at_period_end": False}
    except Exception as exc:
        log.error("resume_subscription error  business=%s  err=%s", business_id, type(exc).__name__)
        return {"error": "Could not resume subscription — please try again"}


def change_plan(business_id: int, tier: str, billing_period: str, confirm_proration: bool = False) -> dict:
    """
    Upgrade an existing subscription in place (same Stripe subscription — no
    second subscription is created).

    Policy: UPGRADES (higher tier, or monthly→annual on the same tier) are applied
    immediately with prorations invoiced immediately; the new plan only takes
    effect locally once Stripe reports the new price on the subscription
    (payment_behavior=pending_if_incomplete → a failed payment leaves the old plan
    in place). DOWNGRADES and annual→monthly are deliberately NOT done via API:
    they are handled in the Customer Portal so the customer never loses paid
    features early or is silently charged/credited.
    """
    import time
    stripe = _stripe()
    if not stripe:
        return {"error": "Stripe not configured"}
    tier = (tier or "").strip().lower()
    period = _normalize_period(billing_period)
    if tier not in PURCHASABLE_TIERS or not period:
        return {"error": "Invalid plan or billing period"}
    try:
        sub, err, code = _owned_subscription(stripe, business_id)
        if err:
            return {"error": err, "code": code}
        status = _g(sub, "status")
        if status == "past_due":
            return {"error": "Update your payment method before changing plan", "code": "payment_required"}
        if status != "active":
            return {"error": "Your subscription must be active to change plan", "code": "not_active"}
        if _g(sub, "cancel_at_period_end", False):
            return {"error": "Resume your subscription before changing plan", "code": "cancel_scheduled"}
        items = _g(_g(sub, "items"), "data", []) or []
        if len(items) != 1:
            return {"error": "This subscription can't be changed automatically — use Manage Subscription",
                    "code": "use_portal"}
        item = items[0]
        cur = _price_index().get(_g(_g(item, "price"), "id"))
        if not cur:
            return {"error": "Current plan could not be identified — use Manage Subscription", "code": "use_portal"}
        if cur == (tier, period):
            return {"error": "You are already on this plan", "code": "same_plan"}
        new_price, perr = _resolve_price_id(tier, period)
        if perr:
            return {"error": perr}
        verr = _verify_price(stripe, new_price, tier, period)
        if verr:
            return {"error": verr}
        cur_currency = (_g(_g(item, "price"), "currency", "") or "").lower()
        new_currency = (_g(stripe.Price.retrieve(new_price), "currency", "") or "").lower()
        if cur_currency and new_currency and cur_currency != new_currency:
            return {"error": "Plan currency differs from your current subscription — contact support",
                    "code": "currency_mismatch"}
        ctier, cperiod = cur
        is_upgrade = (_TIER_RANK.get(tier, 0) > _TIER_RANK.get(ctier, 0)
                      or (tier == ctier and period == "annual" and cperiod == "monthly"))
        if not is_upgrade:
            return {"error": "Downgrades and switching to a shorter billing interval are managed in the "
                             "billing portal so they take effect at the right time.",
                    "code": "use_portal"}
        if not confirm_proration:
            return {"error": "Confirmation required: upgrading charges the prorated difference to your "
                             "saved payment method immediately.", "code": "confirmation_required"}
        updated = stripe.Subscription.modify(
            _g(sub, "id"),
            items=[{"id": _g(item, "id"), "price": new_price}],
            proration_behavior="always_invoice",
            payment_behavior="pending_if_incomplete",
            idempotency_key=f"chg_{_g(sub, 'id')}_{new_price}_{int(time.time() // 300)}",
        )
        res = _sync_subscription(updated, source="api_change_plan")
        new_items = _g(_g(updated, "items"), "data", []) or []
        effective = bool(new_items) and _g(_g(new_items[0], "price"), "id") == new_price
        log.info("Plan change requested  business=%s  to=%s/%s  effective=%s", business_id, tier, period, effective)
        return {"ok": True, "effective": effective,
                "message": ("Your plan has been upgraded." if effective else
                            "Payment is required to complete the upgrade. Your current plan stays active until it succeeds."),
                "synced": res.get("result") == "synced"}
    except Exception as exc:
        log.error("change_plan error  business=%s  err=%s", business_id, type(exc).__name__)
        return {"error": "Could not change plan — please try again"}


def create_portal_session(business_id: int) -> dict:
    """Customer Portal for THIS business only; customer comes from the verified DB mapping."""
    stripe = _stripe()
    if not stripe:
        return {"error": "Stripe not configured on this server", "code": "unconfigured"}
    try:
        cid, err = _verified_customer_id(stripe, business_id)
        if err:
            return {"error": err, "code": "mismatch"}
        if not cid:
            return {"error": "No billing profile yet — subscribe first to manage billing", "code": "no_customer"}
        base = os.getenv("WAZIBOT_URL", "https://wazibothq.com")
        session = stripe.billing_portal.Session.create(customer=cid, return_url=f"{base}/static/dashboard.html")
        return {"url": _g(session, "url", "")}
    except Exception as exc:
        log.error("portal error  business=%s  err=%s", business_id, type(exc).__name__)
        return {"error": "Could not open billing portal — please try again"}


def list_invoices(business_id: int, limit: int = 12) -> dict:
    """Real invoice history from Stripe for this business's verified customer."""
    stripe = _stripe()
    if not stripe:
        return {"invoices": [], "available": False}
    try:
        cid, err = _verified_customer_id(stripe, business_id)
        if err or not cid:
            return {"invoices": [], "available": bool(cid)}
        res = stripe.Invoice.list(customer=cid, limit=max(1, min(int(limit), 24)))
        out = []
        for inv in (_g(res, "data", []) or []):
            out.append({
                "id": _g(inv, "id"), "number": _g(inv, "number"), "status": _g(inv, "status"),
                "amount_paid": (_g(inv, "amount_paid", 0) or 0) / 100.0,
                "amount_due": (_g(inv, "amount_due", 0) or 0) / 100.0,
                "currency": (_g(inv, "currency", "") or "").upper(),
                "created": _iso(_g(inv, "created")),
                "hosted_invoice_url": _g(inv, "hosted_invoice_url"),
                "invoice_pdf": _g(inv, "invoice_pdf"),
            })
        return {"invoices": out, "available": True}
    except Exception as exc:
        log.error("list_invoices error  business=%s  err=%s", business_id, type(exc).__name__)
        return {"invoices": [], "available": False, "error": "Invoice history unavailable"}


# ── Subscription status ───────────────────────────────────────────────────────

def get_subscription_status(business_id: int) -> dict:
    """
    Billing status for the dashboard. Built from the backend's own record
    (kept in sync with Stripe by webhooks) — never from browser input.
    Always returns a valid dict — defaults to free tier on any error.
    """
    try:
        row    = _load_biz(business_id)
        tier   = row.get("subscription_tier")  or "free"
        status = row.get("billing_status")      or "trialing"
        cancel = bool(row.get("cancel_at_period_end"))
        entitled_tier = tier
        try:
            from core.plan_guard import _get_business_plan, _normalise_tier
            pi = _get_business_plan(business_id)
            eff = _normalise_tier(pi["tier"], pi["billing_status"], pi["trial_ends_at"])
            if eff == "FREE" and tier != "free":
                entitled_tier = "free"          # lapsed (e.g. past-due beyond grace)
        except Exception:
            pass
        return {
            "tier":                   tier,
            "billing_status":         status,
            "trial_ends_at":          row.get("trial_ends_at"),
            "stripe_customer_id":     row.get("stripe_customer_id"),
            "stripe_subscription_id": row.get("stripe_subscription_id"),
            "billing_interval":       row.get("billing_interval"),
            "current_period_end":     row.get("current_period_end"),
            "cancel_at_period_end":   cancel,
            "canceled_at":            row.get("canceled_at"),
            "payment_failed_at":      row.get("payment_failed_at"),
            "entitled":               entitled_tier != "free" or tier == "free",
            "access_ends_at":         row.get("current_period_end") if cancel else None,
            "features":               TIERS.get(entitled_tier, TIERS["free"]),
        }
    except Exception as exc:
        log.warning("get_subscription_status error: %s — returning free tier", exc)
        return {"tier": "free", "billing_status": "active", "features": TIERS["free"]}


def get_tier_features(business_id: int) -> dict:
    """Return just the feature flags for a business's current tier."""
    return get_subscription_status(business_id).get("features", TIERS["free"])


# ── Stripe Connect — Merchant onboarding ──────────────────────────────────────
# These were referenced by routes_saas/billing_routes.py but never implemented,
# which caused every call to /billing/connect* to 500 with an ImportError.
# Follows the same safe-by-default rule as the rest of this module: never
# raise, always return a plain dict the route can pass straight through.

def create_connect_account(business_id: int, business_name: str, owner_email: str) -> dict:
    """
    Create (or resume) a Stripe Connect Express account for a merchant and
    return an onboarding link. Returns {"error": "..."} if Stripe isn't
    configured or the call fails.
    """
    stripe = _stripe()
    if not stripe:
        return {"error": "Stripe not configured — add STRIPE_SECRET_KEY to env"}

    try:
        from core.db import supabase
        res = (
            supabase.table("businesses")
            .select("stripe_account_id")
            .eq("id", business_id)
            .limit(1)
            .execute()
        )
        account_id = (res.data or [{}])[0].get("stripe_account_id")

        if not account_id:
            account = stripe.Account.create(
                type="express",
                email=owner_email,
                business_type="individual",
                business_profile={"name": business_name} if business_name else None,
                metadata={"wazibot_business_id": str(business_id)},
            )
            account_id = account["id"]
            try:
                supabase.table("businesses").update(
                    {"stripe_account_id": account_id}
                ).eq("id", business_id).execute()
            except Exception as exc:
                log.debug("stripe_account_id column may not exist yet: %s", exc)
            log.info("Stripe Connect account created  business=%s  account=%s", business_id, account_id)

        base = os.getenv("WAZIBOT_URL", "https://wazibothq.com")
        link = stripe.AccountLink.create(
            account=account_id,
            refresh_url=f"{base}/billing/connect/refresh",
            return_url=f"{base}/billing/connect/return",
            type="account_onboarding",
        )
        return {"url": link["url"], "account_id": account_id}
    except Exception as exc:
        log.error("create_connect_account error: %s", exc)
        return {"error": str(exc)}


def get_connect_account_status(business_id: int) -> dict:
    """
    Return Stripe Connect account status for a merchant.
    Always returns a valid dict — defaults to "not connected" on any error
    (missing column, Stripe not configured, account not found, etc.)
    so the dashboard never sees a 500 here.
    """
    default = {"connected": False, "status": "not_connected", "charges_enabled": False,
               "payouts_enabled": False, "account_id": None}
    try:
        from core.db import supabase
        res = (
            supabase.table("businesses")
            .select("stripe_account_id")
            .eq("id", business_id)
            .limit(1)
            .execute()
        )
        account_id = (res.data or [{}])[0].get("stripe_account_id")
        if not account_id:
            return default

        stripe = _stripe()
        if not stripe:
            return {**default, "account_id": account_id, "status": "unconfigured"}

        account = stripe.Account.retrieve(account_id)
        # Stripe SDK objects support attribute access AND dict-style .get() in v7+
        # Use getattr with fallback to be safe across SDK versions
        charges_enabled = bool(getattr(account, "charges_enabled", None) or account.get("charges_enabled", False))
        payouts_enabled = bool(getattr(account, "payouts_enabled", None) or account.get("payouts_enabled", False))
        return {
            "connected":        True,
            "status":           "active" if (charges_enabled and payouts_enabled) else "pending",
            "charges_enabled":  charges_enabled,
            "payouts_enabled":  payouts_enabled,
            "account_id":       account_id,
        }
    except Exception as exc:
        log.warning("get_connect_account_status error: %s — returning not_connected", exc)
        return default


def create_connect_dashboard_link(business_id: int) -> dict:
    """Generate a Stripe Express Dashboard login link for this merchant."""
    stripe = _stripe()
    if not stripe:
        return {"error": "Stripe not configured — add STRIPE_SECRET_KEY to env"}
    try:
        from core.db import supabase
        res = (
            supabase.table("businesses")
            .select("stripe_account_id")
            .eq("id", business_id)
            .limit(1)
            .execute()
        )
        account_id = (res.data or [{}])[0].get("stripe_account_id")
        if not account_id:
            return {"error": "No connected Stripe account found for this business"}
        link = stripe.Account.create_login_link(account_id)
        return {"url": link["url"]}
    except Exception as exc:
        log.error("create_connect_dashboard_link error: %s", exc)
        return {"error": str(exc)}


# ── Product checkout — customer purchases ─────────────────────────────────────

def create_product_checkout_session(
    business_id: int,
    items: list,
    currency: str = "usd",
    customer_email: str = "",
    success_url: str = "",
    cancel_url: str = "",
) -> dict:
    """
    Create a one-off Stripe Checkout Session for a storefront purchase,
    routing funds to the merchant's connected account when one exists.
    Returns {"url": "..."} on success, {"error": "..."} on failure.
    """
    stripe = _stripe()
    if not stripe:
        return {"error": "Stripe not configured — add STRIPE_SECRET_KEY to env"}
    if not items:
        return {"error": "No items provided"}

    try:
        # Stripe requires amounts in the smallest currency unit.
        # Most currencies: multiply by 100 (e.g. $5.99 → 599 cents).
        # Zero-decimal currencies: use the value as-is (e.g. ¥599 → 599).
        _ZERO_DECIMAL = {
            "bif","clp","djf","gnf","jpy","kmf","krw","mga",
            "pyg","rwf","ugx","vnd","vuv","xaf","xof","xpf",
        }
        cur_norm   = (currency or "usd").strip().lower()
        zero_dec   = cur_norm in _ZERO_DECIMAL

        line_items = []
        for item in items:
            price = float(item.get("price", 0) or 0)
            unit_amount = int(round(price)) if zero_dec else int(round(price * 100))
            if unit_amount <= 0:
                continue  # skip free or invalid items
            line_items.append({
                "price_data": {
                    "currency": cur_norm,
                    "unit_amount": unit_amount,
                    "product_data": {
                        "name": (item.get("name") or "Item")[:250],
                        "description": item.get("description") or None,
                    },
                },
                "quantity": max(1, int(item.get("quantity", 1))),
            })

        base = os.getenv("WAZIBOT_URL", "https://wazibothq.com")
        success_url = success_url or f"{base}/billing/success?session_id={{CHECKOUT_SESSION_ID}}"
        cancel_url  = cancel_url  or f"{base}/billing/cancel-page"

        params: dict = {
            "mode":        "payment",
            "line_items":  line_items,
            "success_url": success_url,
            "cancel_url":  cancel_url,
            # Marks this session as a storefront product/service purchase —
            # the webhook handler uses this to route correctly instead of
            # treating every completed checkout as a WaziBot subscription
            # upgrade (see _on_checkout_completed).
            "metadata":    {"wazibot_business_id": str(business_id), "wazibot_purpose": "product_purchase"},
            # Optional phone collection — if the customer provides one, we
            # can send them a WhatsApp receipt in addition to the email
            # receipt, and log a proper order against their number. Not
            # required, so it never blocks checkout for customers who skip it.
            "phone_number_collection": {"enabled": True},
            # Checkout Studio parameters (ui_mode / branding_settings / origin_context /
            # integration_identifier omitted — rejected by pinned api_version 2024-06-20).
            "billing_address_collection": "auto",
            "automatic_tax":             {"enabled": False},
            "allow_promotion_codes":     True,
            "submit_type":               "auto",
        }
        if customer_email:
            params["customer_email"] = customer_email

        status = get_connect_account_status(business_id)
        if status.get("connected") and status.get("charges_enabled"):
            params["payment_intent_data"] = {
                "transfer_data": {"destination": status["account_id"]},
            }

        session = stripe.checkout.Session.create(**params)
        log.info("Product checkout session created  business=%s", business_id)
        return {"url": session["url"], "session_id": session["id"]}
    except Exception as exc:
        log.error("create_product_checkout_session error: %s", exc)
        return {"error": str(exc)}


# ── Payment analytics ──────────────────────────────────────────────────────────

def get_payment_analytics(business_id: int) -> dict:
    """
    Return simple aggregate payment stats for a business.
    Always returns a valid dict — defaults to zeroed-out stats on any error.
    """
    default = {"total_revenue": 0, "total_orders": 0, "currency": "usd"}
    stripe = _stripe()
    if not stripe:
        return default
    try:
        status = get_connect_account_status(business_id)
        account_id = status.get("account_id")
        if not account_id:
            return default

        charges = stripe.Charge.list(limit=100, **({"stripe_account": account_id} if account_id else {}))
        total = sum(c["amount"] for c in charges.get("data", []) if c.get("paid"))
        return {
            "total_revenue": total / 100,
            "total_orders":  len(charges.get("data", [])),
            "currency":      "usd",
        }
    except Exception as exc:
        log.warning("get_payment_analytics error: %s — returning zeroed stats", exc)
        return default


# ── Webhook handler ───────────────────────────────────────────────────────────

def handle_stripe_webhook(payload: bytes, sig_header: str) -> dict:
    """
    Process an incoming Stripe webhook (raw body + Stripe-Signature header).

    Reliability model
      • Signature verified against the raw body with STRIPE_WEBHOOK_SECRET.
      • Events from the wrong Stripe environment (livemode vs key mode) are rejected.
      • Durable ledger (crud/stripe_webhook_events.py): processed events are skipped;
        a failed event is recorded as failed and a 5xx is returned so Stripe retries.
      • Handlers are state-convergent: they re-read the Stripe object and write the
        result, so retries, duplicates and out-of-order delivery are harmless.

    Events handled:
      checkout.session.completed / async_payment_succeeded → bind customer + sync
      customer.subscription.created|updated|deleted        → sync from Stripe
      invoice.paid / invoice.payment_succeeded             → sync (renewal / recovery)
      invoice.payment_failed / payment_action_required     → sync (past_due)
      charge.refunded, charge.dispute.*, checkout.session.expired → audit log only
    """
    stripe = _stripe()
    secret = os.getenv("STRIPE_WEBHOOK_SECRET", "")
    if not stripe or not secret:
        return {"error": "Stripe webhook not configured", "status": 503}

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, secret)
    except Exception as exc:
        log.warning("Stripe webhook verification failed: %s", type(exc).__name__)
        return {"error": "Invalid signature", "status": 400}

    etype    = event["type"]
    event_id = event["id"]
    data     = event["data"]["object"]
    log.info("Stripe webhook  type=%s  id=%s", etype, event_id)

    ev_live, mode = _g(event, "livemode"), _key_mode()
    if (mode == "live" and ev_live is False) or (mode == "test" and ev_live is True):
        log.error("Stripe webhook environment mismatch  id=%s  key_mode=%s", event_id, mode)
        return {"error": "Event environment does not match server configuration", "status": 400}

    from crud.stripe_webhook_events import already_processed, mark_processed, claim_event, mark_failed
    if already_processed(event_id):
        log.info("Stripe webhook  type=%s  id=%s  — already processed, skipping", etype, event_id)
        return {"ok": True, "event": etype, "duplicate": True}

    claim = claim_event(event_id, etype)
    if claim == "duplicate":
        return {"ok": True, "event": etype, "duplicate": True}
    if claim == "in_progress":
        # Another delivery of the same event is being handled right now; make Stripe retry later.
        return {"error": "Event is being processed", "status": 409}

    handlers = {
        "checkout.session.completed":              _on_checkout_completed,
        "checkout.session.async_payment_succeeded": _on_checkout_completed,
        "checkout.session.async_payment_failed":   _on_checkout_async_failed,
        "checkout.session.expired":                _on_audit_only,
        "customer.subscription.created":           _on_subscription_updated,
        "customer.subscription.updated":           _on_subscription_updated,
        "customer.subscription.deleted":           _on_subscription_deleted,
        "invoice.paid":                            _on_invoice_paid,
        "invoice.payment_succeeded":               _on_invoice_paid,
        "invoice.payment_failed":                  _on_payment_failed,
        "invoice.payment_action_required":         _on_payment_failed,
        "charge.refunded":                         _on_audit_only,
        "charge.dispute.created":                  _on_audit_only,
        "charge.dispute.closed":                   _on_audit_only,
    }
    handler = handlers.get(etype)
    if handler:
        try:
            if handler is _on_audit_only:
                handler(data, etype)
            else:
                handler(data)
        except Exception as exc:
            # Do NOT mark processed: a required update failed, so let Stripe retry.
            log.error("Stripe webhook handler FAILED  type=%s  id=%s  err=%s", etype, event_id, type(exc).__name__)
            mark_failed(event_id, etype, f"{type(exc).__name__}: {str(exc)[:200]}")
            return {"error": "Processing failed; will be retried", "status": 500}

    mark_processed(event_id, etype)
    return {"ok": True, "event": etype}


def _bid_from_meta(obj: dict) -> Optional[int]:
    for src in [_g(obj, "metadata") or {}, (_g(obj, "subscription_data") or {}).get("metadata") or {}]:
        v = _g(src, "wazibot_business_id")
        if v:
            try: return int(v)
            except (ValueError, TypeError): pass
    return None


# Columns that only exist after billing_lifecycle_hardening.sql has been applied.
_EXT_SET = set(_BILLING_EXT_COLS)


def _patch_biz(bid: int, patch: dict) -> None:
    """Persist a billing patch. RAISES on failure so webhooks can be retried.
    If the billing migration hasn't been applied, retries once without the new
    columns so the core tier/status update still lands."""
    from core.db import supabase
    try:
        supabase.table("businesses").update(patch).eq("id", bid).execute()
    except Exception as exc:
        legacy = {k: v for k, v in patch.items() if k not in _EXT_SET}
        if len(legacy) == len(patch) or not legacy:
            raise
        log.warning("billing patch retried without extended columns (migration not applied?)  business=%s  err=%s",
                    bid, type(exc).__name__)
        supabase.table("businesses").update(legacy).eq("id", bid).execute()
    log.info("Billing patch  business=%s  keys=%s", bid, sorted(patch.keys()))


def _prev_tier_status(bid: int) -> tuple:
    """Tier/status BEFORE a patch (best-effort; used only for the audit trail)."""
    try:
        row = _load_biz(bid)
        return row.get("subscription_tier"), row.get("billing_status")
    except Exception as exc:
        log.debug("_prev_tier_status failed  business=%s  error=%s", bid, exc)
        return None, None


def _log_subscription_event(bid: int, prev_tier, new_tier, prev_status, new_status,
                            source: str = "stripe_webhook") -> None:
    """Best-effort audit row (crud/subscription_history.py). Never blocks billing."""
    try:
        from crud.subscription_history import log_subscription_event
        log_subscription_event(
            business_id=bid, previous_tier=prev_tier, new_tier=new_tier,
            previous_status=prev_status, new_status=new_status, source=source,
        )
    except Exception as exc:
        log.debug("_log_subscription_event skipped  business=%s  error=%s", bid, exc)


def _fetch_subscription(stripe, sub_id: str, fallback=None):
    """Current subscription from Stripe. Deleted/missing → fallback payload.
    Any other failure RAISES so the webhook is retried (never guess state)."""
    try:
        return stripe.Subscription.retrieve(sub_id)
    except Exception as exc:
        if getattr(exc, "code", None) == "resource_missing":
            return fallback
        raise


def _period_end(sub):
    pe = _g(sub, "current_period_end")
    if pe:
        return pe
    items = _g(_g(sub, "items"), "data", []) or []
    return _g(items[0], "current_period_end") if items else None


def _period_start(sub):
    ps = _g(sub, "current_period_start")
    if ps:
        return ps
    items = _g(_g(sub, "items"), "data", []) or []
    return _g(items[0], "current_period_start") if items else None


def _derive(sub) -> dict:
    """Pure: everything WaziBot needs to know from a Stripe subscription object."""
    items = _g(_g(sub, "items"), "data", []) or []
    price = _g(items[0], "price") if items else None
    price_id = _g(price, "id")
    tier = period = None
    hit = _price_index().get(price_id) if price_id else None
    if hit:
        tier, period = hit
    if not tier:
        t = _g(_g(price, "metadata"), "wazibot_tier") or _g(_g(sub, "metadata"), "tier")
        if t in TIERS and t not in ("free", "trial"):
            tier = t
    if not period:
        interval = _g(_g(price, "recurring"), "interval")
        period = {"month": "monthly", "year": "annual"}.get(interval)
    cust = _g(sub, "customer")
    if cust is not None and not isinstance(cust, str):
        cust = _g(cust, "id")
    status = _g(sub, "status", "")
    return {
        "sub_id": _g(sub, "id"), "customer_id": cust, "stripe_status": status,
        "live": status in _LIVE_SUB_STATUSES, "tier": tier, "interval": period,
        "price_id": price_id, "currency": (_g(price, "currency", "") or "").upper(),
        "amount": _g(price, "unit_amount"),
        "cancel_at_period_end": bool(_g(sub, "cancel_at_period_end", False)),
        "period_start": _period_start(sub), "period_end": _period_end(sub),
        "canceled_at": _g(sub, "ended_at") or _g(sub, "canceled_at"),
        "trial_end": _g(sub, "trial_end"),
    }


def _resolve_business_id(sub, d: dict) -> Optional[int]:
    bid = _bid_from_meta(sub)
    if bid:
        return bid
    from core.db import supabase
    for col, val in (("stripe_subscription_id", d["sub_id"]), ("stripe_customer_id", d["customer_id"])):
        if val:
            r = supabase.table("businesses").select("id").eq(col, val).limit(1).execute()
            if r.data:
                return int(r.data[0]["id"])
    return None


def _fmt_amount(d: dict) -> str:
    if d.get("amount") is None:
        return ""
    sym = {"USD": "$"}.get(d.get("currency"), "")
    amt = f"{float(d['amount']) / 100.0:g}" if d.get("currency") not in ("JPY",) else str(d["amount"])
    unit = "year" if d.get("interval") == "annual" else "month"
    return f"{sym}{amt}{'' if sym else ' ' + (d.get('currency') or '')}/{unit}"


def _sync_subscription(sub, source: str) -> dict:
    """
    Make WaziBot's record for the owning business match this Stripe subscription.
    Safe to call repeatedly / out of order (callers pass a freshly retrieved object).
    Returns {"result": "synced"|"ignored", ...transition info...}.
    """
    d = _derive(sub)
    if not d["sub_id"]:
        return {"result": "ignored", "reason": "no_subscription_id"}
    bid = _resolve_business_id(sub, d)
    if not bid:
        log.warning("subscription has no business mapping  sub=%s  source=%s", d["sub_id"], source)
        return {"result": "ignored", "reason": "no_business"}
    row = _load_biz(bid)
    if not row:
        log.warning("subscription maps to unknown business  business=%s  sub=%s", bid, d["sub_id"])
        return {"result": "ignored", "reason": "unknown_business"}

    stored_cid = row.get("stripe_customer_id")
    if stored_cid and d["customer_id"] and stored_cid != d["customer_id"]:
        log.error("customer mismatch — ignoring  business=%s  sub=%s", bid, d["sub_id"])
        return {"result": "ignored", "reason": "customer_mismatch"}

    cur_id = row.get("stripe_subscription_id")
    if cur_id and cur_id != d["sub_id"]:
        if not d["live"]:
            log.info("stale event for non-current subscription ignored  business=%s  sub=%s", bid, d["sub_id"])
            return {"result": "ignored", "reason": "stale_subscription"}
        cur = _fetch_subscription(_stripe(), cur_id, None)
        if cur is not None and _g(cur, "status") in _LIVE_SUB_STATUSES:
            log.error("DUPLICATE_ACTIVE_SUBSCRIPTION  business=%s  current=%s  other=%s", bid, cur_id, d["sub_id"])
            return {"result": "ignored", "reason": "duplicate_active_subscription"}

    status = d["stripe_status"]
    if status in ("incomplete", "incomplete_expired"):
        # First payment never completed: must not alter a trial/free business.
        return {"result": "ignored", "reason": f"status_{status}"}

    prev_tier, prev_status = row.get("subscription_tier"), row.get("billing_status")
    local_status = _STRIPE_TO_LOCAL_STATUS.get(status)
    if not local_status:
        log.warning("unrecognised Stripe subscription status  business=%s  status=%s", bid, status)
        return {"result": "ignored", "reason": "unknown_status"}

    tier = d["tier"]
    if status == "canceled":
        new_tier = "free"
    elif tier:
        new_tier = tier
    else:
        new_tier = prev_tier or "free"
        log.error("UNKNOWN_PRICE_ID — tier not changed  business=%s  sub=%s  price=%s", bid, d["sub_id"], d["price_id"])

    from datetime import datetime, timezone
    patch: dict = {
        "subscription_tier": new_tier, "billing_status": local_status,
        "stripe_subscription_id": d["sub_id"],
        "stripe_subscription_status": status,
        "stripe_price_id": d["price_id"], "billing_interval": d["interval"],
        "current_period_start": _iso(d["period_start"]), "current_period_end": _iso(d["period_end"]),
        "cancel_at_period_end": d["cancel_at_period_end"] and status != "canceled",
        "canceled_at": _iso(d["canceled_at"]),
        "billing_synced_at": datetime.now(timezone.utc).isoformat(),
        "payment_failed_at": (row.get("payment_failed_at") or datetime.now(timezone.utc).isoformat())
                             if status == "past_due" else None,
    }
    if d["customer_id"] and not stored_cid:
        patch["stripe_customer_id"] = d["customer_id"]
    if status == "trialing" and d["trial_end"]:
        patch["trial_ends_at"] = _iso(d["trial_end"])

    _patch_biz(bid, patch)
    if (prev_tier, prev_status) != (new_tier, local_status):
        _log_subscription_event(bid, prev_tier, new_tier, prev_status, local_status, source=f"stripe:{source}"[:60])
        log.info("Subscription state change  business=%s  %s/%s → %s/%s  source=%s",
                 bid, prev_tier, prev_status, new_tier, local_status, source)
    return {
        "result": "synced", "business_id": bid, "d": d, "row": row,
        "prev_tier": prev_tier, "prev_status": prev_status,
        "new_tier": new_tier, "new_status": local_status,
        "new_subscription": cur_id != d["sub_id"],
        "was_cancel_scheduled": bool(row.get("cancel_at_period_end")),
    }


def _send_lifecycle_emails(res: dict, *, activated_hint: bool = False) -> None:
    """Fire-and-forget emails, only on real transitions (so duplicates/retries are silent)."""
    if res.get("result") != "synced":
        return
    d, row, bid = res["d"], res["row"], res["business_id"]
    to = row.get("owner_email")
    if not to:
        return
    try:
        from datetime import datetime
        end = _iso(d["period_end"])
        end_h = datetime.fromisoformat(end).strftime("%d %B %Y") if end else "the end of your billing period"
        plan = {"starter": "Starter", "growth": "Growth", "enterprise": "Enterprise"}.get(res["new_tier"], str(res["new_tier"]).title())
        name = row.get("name", "")
        became_active = res["new_status"] == "active" and (res["prev_status"] != "active" or res["new_subscription"])
        if activated_hint and became_active:
            from services.email_service import send_subscription_confirmed
            send_subscription_confirmed(to_email=to, business_name=name, plan_name=plan,
                                        amount=_fmt_amount(d), next_billing=end_h)
        elif res["new_status"] == "past_due" and res["prev_status"] != "past_due":
            from services.email_service import send_payment_failed
            send_payment_failed(to_email=to, business_name=name, plan_name=plan,
                                amount=_fmt_amount(d), retry_date="the next few days")
        elif d["cancel_at_period_end"] and not res["was_cancel_scheduled"] and res["new_status"] != "cancelled":
            from services.email_service import send_subscription_cancelled
            send_subscription_cancelled(to_email=to, business_name=name, plan_name=plan, access_ends=end_h)
        elif res["new_status"] == "cancelled" and res["prev_status"] != "cancelled" and not res["was_cancel_scheduled"]:
            from services.email_service import send_subscription_cancelled
            send_subscription_cancelled(to_email=to, business_name=name, plan_name=plan, access_ends="today")
    except Exception as exc:
        log.debug("lifecycle email skipped  business=%s  err=%s", bid, type(exc).__name__)


def _on_product_purchase_completed(obj: dict) -> None:
    """
    Handles a completed storefront product/service purchase (the "Pay"
    button on a business's public site) — previously this fell through to
    _on_checkout_completed and was misinterpreted as a subscription payment.

    Always sends an email receipt (Stripe guarantees customer_details.email
    on every completed session). If the customer also provided a phone
    number, additionally creates a proper order record and sends a WhatsApp
    PDF receipt. The business is always notified via WhatsApp regardless,
    since that only needs the business's own contact number.

    Fire-and-forget in spirit — every step is wrapped so a failure in one
    (e.g. no email service configured) never blocks the others.
    """
    bid = _bid_from_meta(obj)
    if not bid:
        log.warning("product purchase webhook: no business_id in metadata")
        return

    session_id = obj.get("id", "")
    currency   = (obj.get("currency") or "usd").upper()
    total      = float(obj.get("amount_total") or 0) / (1 if currency in
                 {"BIF","CLP","DJF","GNF","JPY","KMF","KRW","MGA","PYG",
                  "RWF","UGX","VND","VUV","XAF","XOF","XPF"} else 100)
    customer   = obj.get("customer_details") or {}
    cust_email = (customer.get("email") or "").strip()
    cust_phone = (customer.get("phone") or "").strip()
    cust_name  = (customer.get("name") or "Customer").strip()

    try:
        from core.db import supabase
        biz_res = (
            supabase.table("businesses")
            .select("name, is_service_business, currency_symbol, contact_phone")
            .eq("id", bid).limit(1).execute()
        )
        biz = (biz_res.data or [{}])[0]
    except Exception as exc:
        log.warning("product purchase webhook: business lookup failed: %s", exc)
        biz = {}

    biz_name     = biz.get("name") or "the business"
    is_service   = bool(biz.get("is_service_business"))
    currency_sym = biz.get("currency_symbol") or "$"
    noun         = "booking" if is_service else "order"

    # Fetch the actual line items purchased — checkout.session.completed
    # doesn't include them by default.
    items = []
    try:
        stripe = _stripe()
        if stripe:
            li = stripe.checkout.Session.list_line_items(session_id, limit=100)
            for row in li.get("data", []):
                items.append({
                    "name":  row.get("description") or "Item",
                    "qty":   int(row.get("quantity") or 1),
                    "price": float(row.get("amount_total") or 0) / (1 if currency in
                             {"BIF","CLP","DJF","GNF","JPY","KMF","KRW","MGA","PYG",
                              "RWF","UGX","VND","VUV","XAF","XOF","XPF"} else 100)
                             / max(1, int(row.get("quantity") or 1)),
                })
    except Exception as exc:
        log.warning("product purchase webhook: line item fetch failed: %s", exc)

    log.info("PRODUCT PURCHASE COMPLETED  business=%s  total=%s %s  email=%s",
             bid, total, currency, cust_email or "—")

    # 1. Email receipt — always attempted, the one channel Stripe guarantees.
    if cust_email:
        try:
            from services.email_service import send_purchase_receipt
            send_purchase_receipt(
                to_email=cust_email, customer_name=cust_name, business_name=biz_name,
                items=items, total=total, currency_symbol=currency_sym,
                is_service=is_service,
            )
        except Exception as exc:
            log.warning("product purchase webhook: email receipt failed: %s", exc)

    # 2. If a phone was collected, also create a real order + WhatsApp receipt.
    order = None
    if cust_phone:
        try:
            from workflows.order_lifecycle import create_order_supabase
            cart = [{"name": i["name"], "qty": i["qty"], "price": i["price"]} for i in items] or [
                {"name": f"Stripe purchase {session_id[-8:]}", "qty": 1, "price": total}
            ]
            order = create_order_supabase(
                business_id=bid, customer_phone=cust_phone, cart=cart, payment_method="stripe",
            )
            order["business_name"]   = biz_name
            order["currency_symbol"] = currency_sym
            try:
                from core.db import supabase as _sb2
                _sb2.table("orders").update({"payment_status": "paid", "status": "confirmed"}) \
                    .eq("id", order.get("id")).execute()
            except Exception:
                pass
            from services._ai_payments import _send_pdf_invoice
            _send_pdf_invoice(order, cust_phone, bid)
        except Exception as exc:
            log.warning("product purchase webhook: order/WhatsApp receipt failed: %s", exc)

    # 3. Notify the business — only needs their own contact number, works
    #    regardless of whether the customer gave a phone or just an email.
    #    Resolves the business's own outbound WhatsApp credentials the same
    #    way the conversational bot does: their dedicated number/token if
    #    set, otherwise the platform's shared number/token.
    try:
        import crud as _crud
        biz_full = _crud.get_business_by_id(bid)
        dedicated_phone_id = (biz_full or {}).get("whatsapp_phone_id") or ""
        if dedicated_phone_id:
            try:
                notify_token = _crud.get_decrypted_token(biz_full)
            except Exception:
                notify_token = ""
            notify_phone_id = dedicated_phone_id
        else:
            notify_phone_id = os.getenv("SHARED_PHONE_NUMBER_ID", "")
            notify_token    = os.getenv("SHARED_WA_TOKEN", "")

        from services._ai_payments import _notify_business_new_order
        cart_for_notify = [{"name": i["name"], "qty": i["qty"]} for i in items]
        fake_order = {"id": (order or {}).get("id"), "total_price": total}
        _notify_business_new_order(
            bid, fake_order, cust_phone or cust_email or "Stripe customer",
            cart_for_notify, currency_sym, notify_phone_id, notify_token,
        )
    except Exception as exc:
        log.debug("product purchase webhook: business notification skipped: %s", exc)


def _on_checkout_completed(obj: dict) -> None:
    # Storefront product/service purchases (mode="payment") are routed to the
    # order/receipt flow and must never touch the merchant's own WaziBot plan.
    mode = _g(obj, "mode")
    if mode == "payment":
        _on_product_purchase_completed(obj)
        return
    if mode != "subscription":
        log.info("checkout.session.completed ignored  mode=%s", mode)
        return

    bid = _bid_from_meta(obj)
    if not bid:
        try:
            bid = int(_g(obj, "client_reference_id") or 0) or None
        except (TypeError, ValueError):
            bid = None
    if not bid:
        log.warning("subscription checkout without business reference  session=%s", _g(obj, "id"))
        return

    # A completed Checkout page is not proof of payment: wait for 'paid'
    # (async methods deliver checkout.session.async_payment_succeeded later).
    if _g(obj, "payment_status") not in ("paid", "no_payment_required"):
        log.info("checkout completed but not yet paid — no access granted  business=%s  session=%s",
                 bid, _g(obj, "id"))
        return
    sub_id = _g(obj, "subscription")
    if not sub_id:
        return
    sub_id = sub_id if isinstance(sub_id, str) else _g(sub_id, "id")

    row = _load_biz(bid)
    if not row:
        log.warning("checkout for unknown business  business=%s", bid)
        return
    cust = _g(obj, "customer")
    cust = cust if (cust is None or isinstance(cust, str)) else _g(cust, "id")
    stored = row.get("stripe_customer_id")
    if stored and cust and stored != cust:
        log.error("checkout customer does not match business mapping — ignored  business=%s", bid)
        return
    if cust and not stored:
        _patch_biz(bid, {"stripe_customer_id": cust})

    sub = _fetch_subscription(_stripe(), sub_id, None)
    if sub is None:
        log.error("checkout references missing subscription  business=%s", bid)
        return
    # The subscription must belong to the business named on the session.
    sub_bid = _bid_from_meta(sub)
    if sub_bid and sub_bid != bid:
        log.error("session/subscription business mismatch — ignored  session_biz=%s  sub_biz=%s", bid, sub_bid)
        return
    res = _sync_subscription(sub, source="checkout.session.completed")
    if res.get("result") == "synced":
        log.info("SUBSCRIPTION ACTIVATED  business=%s  tier=%s", bid, res["new_tier"])
    _send_lifecycle_emails(res, activated_hint=True)


def _on_checkout_async_failed(obj: dict) -> None:
    log.warning("checkout async payment failed — no access granted  session=%s", _g(obj, "id"))


def _on_audit_only(obj: dict, etype: str) -> None:
    """Refunds / disputes / expired sessions: recorded for audit, no automatic
    access change (policy: a refund or dispute never silently cancels a plan —
    cancellations arrive as customer.subscription.* events)."""
    log.warning("BILLING_AUDIT  type=%s  object=%s  customer=%s  amount=%s  currency=%s",
                etype, _g(obj, "id"), _g(obj, "customer"),
                _g(obj, "amount_refunded", _g(obj, "amount")), _g(obj, "currency"))


def _on_subscription_updated(obj: dict) -> None:
    sub_id = _g(obj, "id")
    sub = _fetch_subscription(_stripe(), sub_id, obj) if sub_id else obj
    res = _sync_subscription(sub, source="customer.subscription.updated")
    _send_lifecycle_emails(res)


def _on_subscription_deleted(obj: dict) -> None:
    sub_id = _g(obj, "id")
    sub = _fetch_subscription(_stripe(), sub_id, obj) if sub_id else obj
    res = _sync_subscription(sub, source="customer.subscription.deleted")
    if res.get("result") == "synced":
        log.info("SUBSCRIPTION ENDED → FREE  business=%s", res["business_id"])
    _send_lifecycle_emails(res)


def _invoice_subscription_id(invoice) -> Optional[str]:
    sid = _g(invoice, "subscription")
    if not sid:
        sid = _g(_g(_g(invoice, "parent"), "subscription_details"), "subscription")
    if sid and not isinstance(sid, str):
        sid = _g(sid, "id")
    return sid


def _sync_from_invoice(invoice, source: str) -> dict:
    sub_id = _invoice_subscription_id(invoice)
    if not sub_id:
        return {"result": "ignored", "reason": "not_a_subscription_invoice"}
    sub = _fetch_subscription(_stripe(), sub_id, None)
    if sub is None:
        return {"result": "ignored", "reason": "subscription_missing"}
    res = _sync_subscription(sub, source=source)
    if res.get("result") == "synced" and _g(invoice, "id"):
        try:
            _patch_biz(res["business_id"], {"stripe_last_invoice_id": _g(invoice, "id")})
        except Exception as exc:
            log.debug("last-invoice id not stored: %s", type(exc).__name__)
    return res


def _on_invoice_paid(invoice: dict) -> None:
    """Renewal / recovery: state comes from the subscription, so a stale or
    duplicate invoice event can't regress a newer state."""
    res = _sync_from_invoice(invoice, "invoice.paid")
    if res.get("result") == "synced":
        log.info("RENEWAL/PAYMENT OK  business=%s  invoice=%s", res["business_id"], _g(invoice, "id"))


def _on_payment_failed(invoice: dict) -> None:
    res = _sync_from_invoice(invoice, "invoice.payment_failed")
    if res.get("result") == "synced":
        log.warning("PAYMENT FAILED  business=%s  invoice=%s  status=%s",
                    res["business_id"], _g(invoice, "id"), res["new_status"])
    _send_lifecycle_emails(res)


# ── Reconciliation (admin-triggered, report-first) ────────────────────────────

def reconcile_billing(fix: bool = False, limit: int = 200) -> dict:
    """
    Compare local billing records with Stripe and report discrepancies.
    Default is READ-ONLY. With fix=True it only re-syncs local metadata from the
    Stripe subscription (deterministic); it never refunds, cancels, charges or
    creates subscriptions. Idempotent.
    """
    stripe = _stripe()
    if not stripe:
        return {"error": "Stripe not configured"}
    from core.db import supabase
    rows = (supabase.table("businesses")
            .select("id, subscription_tier, billing_status, stripe_customer_id, stripe_subscription_id, "
                    "stripe_price_id, cancel_at_period_end")
            .limit(max(1, min(int(limit), 1000))).execute().data or [])
    out, repaired, checked = [], 0, 0
    for r in rows:
        bid, sub_id = r["id"], r.get("stripe_subscription_id")
        tier, status = r.get("subscription_tier") or "free", r.get("billing_status")
        paid_local = tier != "free" and status in ("active", "past_due", "trialing_paid")
        if not sub_id:
            if paid_local:
                out.append({"business_id": bid, "kind": "paid_without_subscription",
                            "local": {"tier": tier, "status": status}})
            continue
        checked += 1
        try:
            sub = stripe.Subscription.retrieve(sub_id)
        except Exception as exc:
            kind = "stripe_subscription_missing" if getattr(exc, "code", None) == "resource_missing" else "stripe_lookup_failed"
            out.append({"business_id": bid, "kind": kind, "local": {"tier": tier, "status": status}})
            continue
        d = _derive(sub)
        expect_status = _STRIPE_TO_LOCAL_STATUS.get(d["stripe_status"])
        issues = []
        if d["stripe_status"] in ("incomplete", "incomplete_expired"):
            continue
        if expect_status and expect_status != status:
            issues.append("status_mismatch")
        exp_tier = "free" if d["stripe_status"] == "canceled" else d["tier"]
        if exp_tier and exp_tier != tier:
            issues.append("tier_mismatch")
        if d["price_id"] and r.get("stripe_price_id") and d["price_id"] != r.get("stripe_price_id"):
            issues.append("price_mismatch")
        if d["customer_id"] and r.get("stripe_customer_id") and d["customer_id"] != r.get("stripe_customer_id"):
            issues.append("customer_mismatch")
        if d["tier"] is None and d["live"]:
            issues.append("unknown_price_id")
        if d["stripe_status"] == "canceled" and tier != "free":
            issues.append("stripe_canceled_local_entitled")
        if d["live"] and tier == "free":
            issues.append("stripe_active_local_free")
        if issues:
            item = {"business_id": bid, "kinds": sorted(set(issues)),
                    "local": {"tier": tier, "status": status},
                    "stripe": {"status": d["stripe_status"], "tier": d["tier"], "interval": d["interval"]}}
            if fix and "customer_mismatch" not in issues:
                try:
                    res = _sync_subscription(sub, source="reconcile")
                    item["repaired"] = res.get("result") == "synced"
                    repaired += 1 if item["repaired"] else 0
                except Exception as exc:
                    item["repair_error"] = type(exc).__name__
            out.append(item)
    for item in out:
        log.warning("RECONCILE_DISCREPANCY  business=%s  kinds=%s", item["business_id"],
                    item.get("kinds") or item.get("kind"))
    from crud.stripe_webhook_events import list_failed_events
    return {"checked": checked, "discrepancies": out, "repaired": repaired,
            "dry_run": not fix, "failed_webhook_events": list_failed_events()}


# ── One-time SQL migration ────────────────────────────────────────────────────

BILLING_SCHEMA_SQL = """
-- Run ONCE in Supabase SQL Editor.
-- All columns are nullable with safe defaults — zero impact on existing rows.

ALTER TABLE businesses
  ADD COLUMN IF NOT EXISTS subscription_tier      TEXT         DEFAULT 'free',
  ADD COLUMN IF NOT EXISTS billing_status         TEXT         DEFAULT 'trialing',
  ADD COLUMN IF NOT EXISTS trial_ends_at          TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS stripe_customer_id     TEXT,
  ADD COLUMN IF NOT EXISTS stripe_subscription_id TEXT,
  ADD COLUMN IF NOT EXISTS features_json          JSONB;

COMMENT ON COLUMN businesses.subscription_tier      IS 'free | pro | business | enterprise';
COMMENT ON COLUMN businesses.billing_status         IS 'trialing | active | past_due | cancelled';
COMMENT ON COLUMN businesses.trial_ends_at          IS 'NULL = no trial or trial already expired';
COMMENT ON COLUMN businesses.stripe_customer_id     IS 'Stripe Customer ID (cus_...)';
COMMENT ON COLUMN businesses.stripe_subscription_id IS 'Stripe Subscription ID (sub_...)';
COMMENT ON COLUMN businesses.features_json          IS 'Override feature flags (optional, future use)';
"""
