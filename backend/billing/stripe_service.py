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
    # ── Starter — $1.99/month ─────────────────────────────────────────────────
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


# ── Checkout session ──────────────────────────────────────────────────────────

def create_checkout_session(
    business_id:       int,
    tier:              str,
    billing_period:    str = "monthly",
    success_url:       str = "",
    cancel_url:        str = "",
    customer_email:    str = "",
    price_id_override: str = "",    # regional price ID — falls back to global if empty
) -> dict:
    """
    Create a Stripe Checkout Session for a subscription upgrade.
    Returns {"url": "..."} on success, {"error": "..."} on failure.

    price_id_override: optional regional Stripe Price ID from get_price_id_for_checkout().
    """
    stripe = _stripe()
    if not stripe:
        return {"error": "Stripe not configured — add STRIPE_SECRET_KEY to env"}

    tier_data = TIERS.get(tier)
    if not tier_data:
        return {"error": f"Unknown tier: {tier}"}

    # Regional override → global tier price → error
    price_id = (price_id_override or "").strip() or tier_data.get(f"stripe_price_id_{billing_period}", "")
    if not price_id:
        return {"error": f"No Stripe price configured for {tier}/{billing_period}. Add STRIPE_PRICE_{tier.upper()}_{billing_period.upper()} env var."}

    base = os.getenv("WAZIBOT_URL", "https://wazibothq.com")
    success_url = success_url or f"{base}/billing/success?session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url  = cancel_url  or f"{base}/billing/cancel"

    try:
        params: dict = {
            "mode":              "subscription",
            "line_items":        [{"price": price_id, "quantity": 1}],
            "success_url":       success_url,
            "cancel_url":        cancel_url,
            "metadata":          {"wazibot_business_id": str(business_id), "tier": tier},
            "subscription_data": {"metadata": {"wazibot_business_id": str(business_id), "tier": tier}},
        }
        if customer_email:
            params["customer_email"] = customer_email

        session = stripe.checkout.Session.create(**params)
        log.info("Checkout session created  business=%s  tier=%s", business_id, tier)
        return {"url": session["url"], "session_id": session["id"]}
    except Exception as exc:
        log.error("create_checkout_session error: %s", exc)
        return {"error": str(exc)}


def cancel_subscription(business_id: int) -> dict:
    """Cancel a business's active subscription at period end."""
    stripe = _stripe()
    if not stripe:
        return {"error": "Stripe not configured"}
    try:
        from core.db import supabase
        res = (
            supabase.table("businesses")
            .select("stripe_subscription_id")
            .eq("id", business_id)
            .limit(1)
            .execute()
        )
        sub_id = (res.data or [{}])[0].get("stripe_subscription_id")
        if not sub_id:
            return {"error": "No active Stripe subscription found"}
        stripe.Subscription.modify(sub_id, cancel_at_period_end=True)
        log.info("Subscription scheduled for cancellation  business=%s  sub=%s",
                 business_id, sub_id)
        return {"ok": True, "cancelled_at_period_end": True}
    except Exception as exc:
        log.error("cancel_subscription error: %s", exc)
        return {"error": str(exc)}


# ── Subscription status ───────────────────────────────────────────────────────

def get_subscription_status(business_id: int) -> dict:
    """
    Return full billing status for a business.
    Always returns a valid dict — defaults to free tier on any error.
    """
    try:
        from core.db import supabase
        res = (
            supabase.table("businesses")
            .select("subscription_tier, billing_status, trial_ends_at, stripe_customer_id, stripe_subscription_id")
            .eq("id", business_id)
            .limit(1)
            .execute()
        )
        row    = (res.data or [{}])[0]
        tier   = row.get("subscription_tier")  or "free"
        status = row.get("billing_status")      or "trialing"
        return {
            "tier":                   tier,
            "billing_status":         status,
            "trial_ends_at":          row.get("trial_ends_at"),
            "stripe_customer_id":     row.get("stripe_customer_id"),
            "stripe_subscription_id": row.get("stripe_subscription_id"),
            "features":               TIERS.get(tier, TIERS["free"]),
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
            .select("stripe_connect_account_id")
            .eq("id", business_id)
            .limit(1)
            .execute()
        )
        account_id = (res.data or [{}])[0].get("stripe_connect_account_id")

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
                    {"stripe_connect_account_id": account_id}
                ).eq("id", business_id).execute()
            except Exception as exc:
                log.debug("stripe_connect_account_id column may not exist yet: %s", exc)
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
            .select("stripe_connect_account_id")
            .eq("id", business_id)
            .limit(1)
            .execute()
        )
        account_id = (res.data or [{}])[0].get("stripe_connect_account_id")
        if not account_id:
            return default

        stripe = _stripe()
        if not stripe:
            return {**default, "account_id": account_id, "status": "unconfigured"}

        account = stripe.Account.retrieve(account_id)
        charges_enabled = bool(account.get("charges_enabled"))
        payouts_enabled = bool(account.get("payouts_enabled"))
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
            .select("stripe_connect_account_id")
            .eq("id", business_id)
            .limit(1)
            .execute()
        )
        account_id = (res.data or [{}])[0].get("stripe_connect_account_id")
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
        line_items = []
        for item in items:
            unit_amount = int(round(float(item.get("price", 0)) * 100))
            line_items.append({
                "price_data": {
                    "currency": currency,
                    "unit_amount": unit_amount,
                    "product_data": {
                        "name": item.get("name", "Item"),
                        "description": item.get("description") or None,
                    },
                },
                "quantity": int(item.get("quantity", 1)),
            })

        base = os.getenv("WAZIBOT_URL", "https://wazibothq.com")
        success_url = success_url or f"{base}/billing/success?session_id={{CHECKOUT_SESSION_ID}}"
        cancel_url  = cancel_url  or f"{base}/billing/cancel-page"

        params: dict = {
            "mode":        "payment",
            "line_items":  line_items,
            "success_url": success_url,
            "cancel_url":  cancel_url,
            "metadata":    {"wazibot_business_id": str(business_id)},
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
    Process an incoming Stripe webhook.
    Called by routes/billing_routes.py after raw body is read.

    Events handled:
      checkout.session.completed       → activate subscription
      customer.subscription.updated    → sync tier/status
      customer.subscription.deleted    → downgrade to free
      invoice.payment_failed           → mark past_due
    """
    stripe = _stripe()
    secret = os.getenv("STRIPE_WEBHOOK_SECRET", "")
    if not stripe or not secret:
        return {"error": "Stripe webhook not configured", "status": 400}

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, secret)
    except Exception as exc:
        log.warning("Stripe webhook verification failed: %s", exc)
        return {"error": "Invalid signature", "status": 400}

    etype = event["type"]
    data  = event["data"]["object"]
    log.info("Stripe webhook  type=%s  id=%s", etype, event["id"])

    handlers = {
        "checkout.session.completed":     _on_checkout_completed,
        "customer.subscription.created":  _on_subscription_updated,
        "customer.subscription.updated":  _on_subscription_updated,
        "customer.subscription.deleted":  _on_subscription_deleted,
        "invoice.payment_failed":         _on_payment_failed,
    }
    handler = handlers.get(etype)
    if handler:
        try:
            handler(data)
        except Exception as exc:
            log.error("Stripe webhook handler error  type=%s  error=%s", etype, exc)

    return {"ok": True, "event": etype}


def _bid_from_meta(obj: dict) -> Optional[int]:
    for src in [obj.get("metadata") or {}, (obj.get("subscription_data") or {}).get("metadata") or {}]:
        v = src.get("wazibot_business_id")
        if v:
            try: return int(v)
            except (ValueError, TypeError): pass
    return None


def _patch_biz(bid: int, patch: dict) -> None:
    try:
        from core.db import supabase
        supabase.table("businesses").update(patch).eq("id", bid).execute()
        log.info("Billing patch  business=%s  keys=%s", bid, list(patch.keys()))
    except Exception as exc:
        log.error("_patch_biz error  business=%s  error=%s", bid, exc)


def _on_checkout_completed(obj: dict) -> None:
    bid  = _bid_from_meta(obj)
    if not bid: return
    tier = (obj.get("metadata") or {}).get("tier", "pro")
    _patch_biz(bid, {
        "subscription_tier":      tier,
        "billing_status":         "active",
        "stripe_subscription_id": obj.get("subscription", ""),
    })
    log.info("SUBSCRIPTION ACTIVATED  business=%s  tier=%s", bid, tier)
    # Send subscription confirmation email — fire-and-forget
    try:
        from core.db import supabase
        from services.email_service import send_subscription_confirmed
        biz = supabase.table("businesses").select("name,owner_email").eq("id", bid).limit(1).execute()
        b   = (biz.data or [{}])[0]
        if b.get("owner_email"):
            tier_labels = {"starter": "Starter", "growth": "Growth", "enterprise": "Enterprise"}
            tier_prices = {"starter": "$5.99/month", "growth": "$12/month", "enterprise": "Custom"}
            send_subscription_confirmed(
                to_email      = b["owner_email"],
                business_name = b.get("name", ""),
                plan_name     = tier_labels.get(tier, tier.title()),
                amount        = tier_prices.get(tier, ""),
                next_billing  = "See your Stripe dashboard",
            )
    except Exception as _e:
        log.debug("checkout email error: %s", _e)


def _on_subscription_updated(obj: dict) -> None:
    bid = _bid_from_meta(obj)
    if not bid: return
    status_map = {"active": "active", "trialing": "trialing",
                  "past_due": "past_due", "canceled": "cancelled", "unpaid": "past_due"}
    billing_status = status_map.get(obj.get("status", ""), "active")
    patch: dict = {"billing_status": billing_status, "stripe_subscription_id": obj.get("id")}
    # Try to derive tier from price metadata
    for item in ((obj.get("items") or {}).get("data") or []):
        t = ((item.get("price") or {}).get("metadata") or {}).get("wazibot_tier")
        if t:
            patch["subscription_tier"] = t
            break
    _patch_biz(bid, patch)


def _on_subscription_deleted(obj: dict) -> None:
    bid = _bid_from_meta(obj)
    if not bid: return
    _patch_biz(bid, {"subscription_tier": "free", "billing_status": "cancelled",
                     "stripe_subscription_id": None})
    log.info("SUBSCRIPTION CANCELLED → FREE  business=%s", bid)
    # Send cancellation confirmation email — fire-and-forget
    try:
        from core.db import supabase
        from services.email_service import send_subscription_cancelled
        biz = supabase.table("businesses").select("name,owner_email,subscription_tier").eq("id", bid).limit(1).execute()
        b   = (biz.data or [{}])[0]
        if b.get("owner_email"):
            send_subscription_cancelled(
                to_email      = b["owner_email"],
                business_name = b.get("name", ""),
                plan_name     = (b.get("subscription_tier") or "").title() or "Paid",
                access_ends   = "end of current billing period",
            )
    except Exception as _e:
        log.debug("cancellation email error: %s", _e)


def _on_payment_failed(invoice: dict) -> None:
    cid = invoice.get("customer")
    if not cid: return
    try:
        from core.db import supabase
        res = supabase.table("businesses").select("id,name,owner_email,subscription_tier").eq("stripe_customer_id", cid).limit(1).execute()
        b   = (res.data or [{}])[0]
        bid = b.get("id")
        if bid:
            _patch_biz(bid, {"billing_status": "past_due"})
            log.warning("PAYMENT FAILED  business=%s  customer=%s", bid, cid)
            # Send payment failed email — fire-and-forget
            try:
                from services.email_service import send_payment_failed
                if b.get("owner_email"):
                    tier = b.get("subscription_tier") or "starter"
                    tier_prices = {"starter": "$5.99", "growth": "$12.00", "enterprise": "Custom"}
                    send_payment_failed(
                        to_email      = b["owner_email"],
                        business_name = b.get("name", ""),
                        plan_name     = tier.title(),
                        amount        = tier_prices.get(tier, ""),
                        retry_date    = "3 days",
                    )
            except Exception as _e:
                log.debug("payment failed email error: %s", _e)
    except Exception as exc:
        log.error("_on_payment_failed error: %s", exc)


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
