"""
saas/tenant_features.py — Per-Tenant Feature Flags & AI Role Configuration

PURPOSE
───────
This module is a READ-ONLY config layer on top of the existing system.
It never modifies `generate_reply()`, the conversation state machine, or any
existing route. It only provides helper functions that callers can optionally
consult before enabling premium features.

AI ROLES (injected via existing ai_context field — never touching generate_reply)
────────────────────────────────────────────────────────────────────────────────
  sales_assistant    — optimised for product discovery, upsell, and checkout
  support_assistant  — optimised for order status, refunds, and issue resolution
  booking_assistant  — optimised for appointment scheduling and confirmations
  general            — default WaziBot behaviour (no change)

FEATURE FLAGS
─────────────
All flags come from the subscription tier (billing/stripe_service.py TIERS).
A missing flag or free tier always falls back to the safe default (False/off).
"""

from __future__ import annotations

import logging
import os
from typing import Optional

log = logging.getLogger(__name__)

# ── AI Role definitions ───────────────────────────────────────────────────────

AI_ROLES: dict[str, dict] = {
    "general": {
        "label":       "General Assistant",
        "emoji":       "🤖",
        "description": "Default WaziBot behaviour — handles ordering, support, and bookings.",
        "ai_context_prefix": "",  # empty = no change to existing behaviour
    },
    "sales_assistant": {
        "label":       "Sales Assistant",
        "emoji":       "🛍️",
        "description": "Optimised for product discovery, cross-sell, upsell, and checkout.",
        "ai_context_prefix": (
            "You are a friendly sales assistant. Your primary goal is to help customers "
            "discover products, understand their options, and complete their purchases. "
            "Proactively suggest complementary products. Always be enthusiastic about "
            "the products and highlight their value. Guide customers smoothly to checkout."
        ),
    },
    "support_assistant": {
        "label":       "Support Assistant",
        "emoji":       "🎧",
        "description": "Optimised for order status, refunds, complaints, and issue resolution.",
        "ai_context_prefix": (
            "You are a dedicated customer support assistant. Your primary goal is to "
            "resolve customer issues quickly and professionally. Always acknowledge the "
            "customer's concern first. Prioritise providing clear, accurate information "
            "about order status and next steps. Escalate to a human agent when needed."
        ),
    },
    "booking_assistant": {
        "label":       "Booking Assistant",
        "emoji":       "📅",
        "description": "Optimised for appointment scheduling and confirmations.",
        "ai_context_prefix": (
            "You are a professional booking assistant. Your primary goal is to help "
            "customers schedule, reschedule, or cancel appointments efficiently. "
            "Always confirm booking details clearly and remind customers of any "
            "preparation needed. Be precise about dates, times, and availability."
        ),
    },
}


# ── Feature flag helpers ──────────────────────────────────────────────────────

def get_feature_flags(business_id: int) -> dict:
    """
    Return feature flags for a business based on their subscription tier.
    Always returns a complete dict — defaults to free tier on any error.
    """
    try:
        from billing.stripe_service import get_tier_features
        return get_tier_features(business_id)
    except ImportError:
        # Stripe module not yet deployed — return free tier
        return _free_features()
    except Exception as exc:
        log.warning("get_feature_flags error business=%s: %s — returning free", business_id, exc)
        return _free_features()


def _free_features() -> dict:
    return {
        "messages_per_day": 50,
        "products_limit":   10,
        "campaigns":        False,
        "analytics":        False,
        "multi_agent":      False,
        "api_access":       False,
        "priority_support": False,
        "ai_roles":         False,
        "catalog_images":   False,
    }


def feature_enabled(business_id: int, feature: str) -> bool:
    """
    Check whether a specific feature is enabled for a business.
    Safe: always returns False on error.
    """
    try:
        flags = get_feature_flags(business_id)
        val   = flags.get(feature)
        if isinstance(val, bool):
            return val
        if isinstance(val, int):
            return val != 0  # -1 = unlimited = enabled
        return bool(val)
    except Exception:
        return False


def get_messages_limit(business_id: int) -> int:
    """Return daily message limit. -1 = unlimited. Default: 50 (free)."""
    try:
        return int(get_feature_flags(business_id).get("messages_per_day", 50))
    except Exception:
        return 50


def get_products_limit(business_id: int) -> int:
    """Return product count limit. -1 = unlimited. Default: 10 (free)."""
    try:
        return int(get_feature_flags(business_id).get("products_limit", 10))
    except Exception:
        return 10


# ── AI Role helpers ───────────────────────────────────────────────────────────

def get_ai_role(business_id: int) -> str:
    """
    Return the AI role key configured for a business.
    Reads from businesses.ai_role column (nullable, defaults to "general").
    Only active if ai_roles feature is enabled for the business's tier.
    """
    if not feature_enabled(business_id, "ai_roles"):
        return "general"
    try:
        from core.db import supabase
        res = (
            supabase.table("businesses")
            .select("ai_role")
            .eq("id", business_id)
            .limit(1)
            .execute()
        )
        role = (res.data or [{}])[0].get("ai_role") or "general"
        if role not in AI_ROLES:
            return "general"
        return role
    except Exception as exc:
        log.debug("get_ai_role error business=%s: %s", business_id, exc)
        return "general"


def set_ai_role(business_id: int, role: str) -> bool:
    """
    Set the AI role for a business (requires ai_roles feature).
    Returns True on success.
    """
    if role not in AI_ROLES:
        return False
    if not feature_enabled(business_id, "ai_roles"):
        return False
    try:
        from core.db import supabase
        supabase.table("businesses").update({"ai_role": role}).eq("id", business_id).execute()
        log.info("AI role set  business=%s  role=%s", business_id, role)
        return True
    except Exception as exc:
        log.error("set_ai_role error: %s", exc)
        return False


def build_ai_context_prefix(business_id: int, existing_ai_context: str = "") -> str:
    """
    Build an ai_context string for injection into generate_reply().

    INJECTION POINT: This is how AI roles work without touching generate_reply().
    The existing code already reads business.ai_context and passes it to the AI.
    We prepend the role's context prefix to whatever the business owner has set.

    If ai_roles is disabled or role is "general", returns the existing context unchanged.
    """
    role = get_ai_role(business_id)
    if role == "general":
        return existing_ai_context

    role_config   = AI_ROLES.get(role, AI_ROLES["general"])
    role_prefix   = role_config.get("ai_context_prefix", "")
    if not role_prefix:
        return existing_ai_context

    parts = [p for p in [role_prefix, existing_ai_context.strip()] if p]
    return "\n\n".join(parts)


# ── Per-tenant branding ───────────────────────────────────────────────────────

def get_tenant_branding(business_id: int) -> dict:
    """
    Return optional branding config for a business.
    All keys are optional — missing = use WaziBot defaults.
    Reads from businesses.features_json (nullable JSONB column).
    """
    try:
        from core.db import supabase
        res = (
            supabase.table("businesses")
            .select("features_json, name")
            .eq("id", business_id)
            .limit(1)
            .execute()
        )
        row          = (res.data or [{}])[0]
        features     = row.get("features_json") or {}
        biz_name     = row.get("name", "WaziBot")
        return {
            "brand_name":      features.get("brand_name",      biz_name),
            "primary_color":   features.get("primary_color",   "#00C853"),
            "greeting_tone":   features.get("greeting_tone",   "friendly"),
            "language":        features.get("language",        "en"),
            "currency_symbol": features.get("currency_symbol", "$"),
        }
    except Exception as exc:
        log.debug("get_tenant_branding error: %s", exc)
        return {
            "brand_name": "WaziBot",
            "primary_color": "#00C853",
            "greeting_tone": "friendly",
            "language": "en",
            "currency_symbol": "$",
        }


# ── Onboarding status ─────────────────────────────────────────────────────────

ONBOARDING_STEPS = [
    {"key": "business_registered",   "label": "Business registered",          "required": True},
    {"key": "whatsapp_connected",     "label": "WhatsApp number connected",    "required": True},
    {"key": "first_product_added",    "label": "First product added",          "required": True},
    {"key": "payment_method_set",     "label": "Payment method configured",    "required": True},
    {"key": "test_order_placed",      "label": "Test order completed",         "required": False},
    {"key": "campaign_sent",          "label": "First campaign sent",          "required": False},
    {"key": "subscription_upgraded",  "label": "Subscription upgraded",        "required": False},
]


def get_onboarding_status(business_id: int) -> dict:
    """
    Compute onboarding completion status for a business.
    Uses existing data — no new tables needed.
    """
    try:
        from core.db import supabase

        biz_res  = supabase.table("businesses").select("*").eq("id", business_id).limit(1).execute()
        biz      = (biz_res.data or [{}])[0]
        prod_res = supabase.table("products").select("id").eq("business_id", business_id).limit(1).execute()
        ord_res  = supabase.table("orders").select("id").eq("business_id", business_id).limit(1).execute()

        has_wa    = bool(biz.get("whatsapp_phone_id") or biz.get("wa_phone_number_id")
                         or os.getenv("SHARED_PHONE_NUMBER_ID"))
        has_pay   = bool(biz.get("ecocash_number") or biz.get("paypal_email") or
                         biz.get("stripe_customer_id"))
        has_sub   = (biz.get("subscription_tier") or "free") != "free"

        step_status = {
            "business_registered":  True,
            "whatsapp_connected":   has_wa,
            "first_product_added":  bool(prod_res.data),
            "payment_method_set":   has_pay,
            "test_order_placed":    bool(ord_res.data),
            "campaign_sent":        False,  # would need campaigns table query
            "subscription_upgraded": has_sub,
        }

        completed_required = sum(
            1 for s in ONBOARDING_STEPS if s["required"] and step_status.get(s["key"])
        )
        total_required = sum(1 for s in ONBOARDING_STEPS if s["required"])
        pct = round(completed_required / total_required * 100) if total_required else 0

        return {
            "steps":              [{**s, "completed": step_status.get(s["key"], False)}
                                   for s in ONBOARDING_STEPS],
            "completion_pct":     pct,
            "is_fully_onboarded": pct == 100,
        }
    except Exception as exc:
        log.warning("get_onboarding_status error: %s", exc)
        return {"steps": [], "completion_pct": 0, "is_fully_onboarded": False}
