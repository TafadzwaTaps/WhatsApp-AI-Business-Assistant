"""
core/plan_guard.py
══════════════════
Plan enforcement and trial expiry checking.

PLACEMENT: backend/core/plan_guard.py

USAGE
-----
  # In a route that requires at least the GROWTH plan:
  from core.plan_guard import require_plan
  @router.get("/campaigns")
  def get_campaigns(user=Depends(require_business), _=Depends(require_plan("GROWTH"))):
      ...

  # Standalone trial/plan check:
  from core.plan_guard import check_trial_status
  status = check_trial_status(business_id)
  if not status["allowed"]:
      raise HTTPException(403, status["message"])

PLAN HIERARCHY (lowest → highest)
  FREE → STARTER → GROWTH → PRO

A business on GROWTH can access STARTER and FREE features.

BACKWARD COMPATIBILITY
  This module is NEW — it does not modify any existing code.
  Existing routes without Depends(require_plan(...)) are unaffected.
  Only routes that explicitly opt in to plan gating are guarded.

NEVER blocks:
  - Login / auth endpoints
  - Billing / subscription endpoints
  - Settings / profile endpoints
  - Webhook endpoints
  - Public marketplace / store pages
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, HTTPException

log = logging.getLogger("wazibot.plan_guard")

# Plan hierarchy — higher index = higher tier
_PLAN_ORDER = ["FREE", "STARTER", "GROWTH", "PRO"]

# Mapping from stored subscription_tier values to normalised names
_TIER_MAP = {
    "free":    "FREE",
    "trial":   "STARTER",   # trialing users get STARTER access
    "starter": "STARTER",
    "growth":  "GROWTH",
    "pro":     "PRO",
    "business":"GROWTH",    # legacy alias
    "enterprise":"PRO",     # legacy alias
}

# Human-readable plan names for upgrade messages
_PLAN_LABELS = {
    "FREE":    "Free",
    "STARTER": "Starter ($9/mo)",
    "GROWTH":  "Growth ($29/mo)",
    "PRO":     "Pro ($79/mo)",
}


def _get_business_plan(business_id: int) -> dict:
    """
    Fetch subscription info for a business from Supabase.
    Returns a dict with: tier, billing_status, trial_ends_at
    Falls back to FREE on any DB error.
    """
    try:
        from core.db import supabase
        res = (
            supabase.table("businesses")
            .select("subscription_tier, billing_status, trial_ends_at")
            .eq("id", business_id)
            .limit(1)
            .execute()
        )
        row = (res.data or [{}])[0]
        return {
            "tier":           (row.get("subscription_tier") or "free").lower(),
            "billing_status": (row.get("billing_status")   or "free").lower(),
            "trial_ends_at":  row.get("trial_ends_at"),
        }
    except Exception as exc:
        log.warning("plan_guard: DB error fetching plan for biz %s: %s", business_id, exc)
        return {"tier": "free", "billing_status": "free", "trial_ends_at": None}


def _normalise_tier(raw_tier: str, billing_status: str, trial_ends_at) -> str:
    """
    Resolve the effective plan tier, accounting for trial status.

    Active trialing businesses get STARTER-level access even if their
    tier is stored as 'free'.
    """
    status = (billing_status or "").lower()

    # Active paid subscription — use stored tier
    if status in ("active", "paid"):
        return _TIER_MAP.get(raw_tier, "FREE")

    # Trial — check if still active
    if status in ("trialing", "trial"):
        if trial_ends_at:
            try:
                if isinstance(trial_ends_at, str):
                    from datetime import datetime as dt
                    ends = dt.fromisoformat(trial_ends_at.replace("Z", "+00:00"))
                else:
                    ends = trial_ends_at
                if ends.tzinfo is None:
                    ends = ends.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) < ends:
                    # Trial is active — give STARTER-level access
                    return "STARTER"
            except Exception:
                pass
        # Trial expired — drop to FREE
        return "FREE"

    # Cancelled, expired, or unknown — fall back to stored tier or FREE
    return _TIER_MAP.get(raw_tier, "FREE")


def _meets_plan(effective_tier: str, required_tier: str) -> bool:
    """Return True if effective_tier >= required_tier in the hierarchy."""
    try:
        return _PLAN_ORDER.index(effective_tier) >= _PLAN_ORDER.index(required_tier)
    except ValueError:
        return False


def check_trial_status(business_id: int) -> dict:
    """
    Standalone trial / plan status check.

    Returns:
        {
            "allowed":   bool,        # True = premium features accessible
            "tier":      str,         # Effective tier (FREE / STARTER / GROWTH / PRO)
            "status":    str,         # billing_status value from DB
            "expired":   bool,        # True if trial has expired
            "message":   str,         # Human-readable status or upgrade prompt
            "upgrade_url": str,       # Link to pricing page
        }
    """
    plan_info      = _get_business_plan(business_id)
    effective_tier = _normalise_tier(
        plan_info["tier"],
        plan_info["billing_status"],
        plan_info["trial_ends_at"],
    )
    billing_status = plan_info["billing_status"]
    trial_expired  = (
        billing_status in ("trialing", "trial")
        and effective_tier == "FREE"
    )

    allowed = effective_tier != "FREE" or billing_status in ("active", "paid")

    if billing_status in ("active", "paid"):
        msg = f"Active {_PLAN_LABELS.get(effective_tier, effective_tier)} plan."
    elif effective_tier == "STARTER" and billing_status in ("trialing", "trial"):
        msg = "Free trial active. Upgrade to keep premium features after your trial."
    elif trial_expired:
        msg = (
            "Your free trial has expired. Upgrade to continue using premium features. "
            "Your data and products are safe."
        )
    else:
        msg = f"You are on the {_PLAN_LABELS.get(effective_tier, 'Free')} plan."

    return {
        "allowed":     allowed,
        "tier":        effective_tier,
        "status":      billing_status,
        "expired":     trial_expired,
        "message":     msg,
        "upgrade_url": "/pricing",
    }


# ─────────────────────────────────────────────────────────────────────────────
# FastAPI Dependency
# ─────────────────────────────────────────────────────────────────────────────

def require_plan(minimum_tier: str):
    """
    FastAPI dependency factory.  Returns a dependency that raises HTTP 403
    if the authenticated business's effective plan is below minimum_tier.

    Example:
        @router.get("/campaigns")
        def campaigns(user=Depends(require_business), _=Depends(require_plan("GROWTH"))):
            ...

    minimum_tier must be one of: FREE, STARTER, GROWTH, PRO
    """
    minimum_upper = minimum_tier.upper()
    if minimum_upper not in _PLAN_ORDER:
        raise ValueError(f"require_plan: unknown tier {minimum_tier!r}. Valid: {_PLAN_ORDER}")

    # C2 fix: import require_business lazily inside the closure to avoid
    # circular imports, then use it directly as the FastAPI dependency.
    try:
        from core.auth import require_business as _rb
    except ImportError:
        def _rb():
            return {}

    def _check(user: dict = Depends(_rb)):
        """Inner dependency — resolves the user's plan and enforces the minimum."""
        business_id = user.get("business_id")
        if not business_id:
            raise HTTPException(403, "Business ID not found in token.")

        plan_info      = _get_business_plan(business_id)
        effective_tier = _normalise_tier(
            plan_info["tier"],
            plan_info["billing_status"],
            plan_info["trial_ends_at"],
        )

        if not _meets_plan(effective_tier, minimum_upper):
            required_label = _PLAN_LABELS.get(minimum_upper, minimum_upper)
            current_label  = _PLAN_LABELS.get(effective_tier, effective_tier)
            log.info(
                "plan_guard: BLOCKED biz=%s required=%s effective=%s",
                business_id, minimum_upper, effective_tier,
            )
            raise HTTPException(
                status_code=403,
                detail={
                    "error":       "plan_required",
                    "message":     (
                        f"This feature requires the {required_label} plan. "
                        f"You are currently on {current_label}."
                    ),
                    "required_plan":  minimum_upper,
                    "current_plan":   effective_tier,
                    "upgrade_url":    "/pricing",
                },
            )
        return user

    return _check


def _get_current_user_dep():
    """
    DEPRECATED — C2 fix replaced Depends(_get_current_user_dep) with
    Depends(require_business) directly inside require_plan closure.
    Kept to avoid breaking any external callers; do not use for new code.
    """
    try:
        from core.auth import require_business
        return require_business
    except ImportError:
        def _passthrough():
            return {}
        return _passthrough


# ─────────────────────────────────────────────────────────────────────────────
# Convenience: which features are gated at each tier
# ─────────────────────────────────────────────────────────────────────────────

GATED_FEATURES = {
    "campaigns":          "GROWTH",
    "ai_website":         "PRO",
    "multi_language":     "PRO",
    "advanced_analytics": "PRO",
    "growth_automation":  "GROWTH",
    "crm_segments":       "GROWTH",
    "human_handoff":      "GROWTH",
    "broadcast":          "GROWTH",
    "live_inbox":         "GROWTH",
}


def feature_access(feature_key: str, business_id: int) -> dict:
    """
    Non-blocking check: does this business have access to a named feature?

    Returns:
        {"allowed": bool, "required_tier": str, "current_tier": str, "upgrade_url": str}
    """
    required = GATED_FEATURES.get(feature_key, "FREE").upper()
    plan_info = _get_business_plan(business_id)
    effective = _normalise_tier(
        plan_info["tier"], plan_info["billing_status"], plan_info["trial_ends_at"]
    )
    allowed = _meets_plan(effective, required)
    return {
        "allowed":       allowed,
        "required_tier": required,
        "current_tier":  effective,
        "upgrade_url":   "/pricing",
    }

# ─────────────────────────────────────────────────────────────────────────────
# Sprint 2 — Product count limits per plan
# ─────────────────────────────────────────────────────────────────────────────

PLAN_PRODUCT_LIMITS: dict[str, int | None] = {
    "FREE":    10,
    "STARTER": 25,
    "GROWTH":  None,  # None = unlimited
    "PRO":     None,
}


def get_product_limit(business_id: int) -> int | None:
    """
    Return the product count limit for this business's current plan.
    None means unlimited.
    Fails safely — returns None (unlimited) on any error.
    """
    try:
        plan_info = _get_business_plan(business_id)
        effective = _normalise_tier(
            plan_info["tier"],
            plan_info["billing_status"],
            plan_info["trial_ends_at"],
        )
        return PLAN_PRODUCT_LIMITS.get(effective, 10)
    except Exception as exc:
        log.warning("get_product_limit: error for biz %s: %s — allowing", business_id, exc)
        return None   # fail open: don't block on plan check errors


def check_product_limit(business_id: int) -> dict | None:
    """
    Check if this business has reached its product limit.

    Returns None if the limit is not reached (proceed normally).
    Returns a structured error dict if the limit IS reached:
        {"error": "plan_limit", "message": "...", "limit": N, "upgrade_url": "/pricing"}

    Called ONLY in POST /products — never in PATCH, DELETE, or import.
    """
    limit = get_product_limit(business_id)
    if limit is None:
        return None   # unlimited plan — no check needed

    try:
        from core.db import supabase
        res = (
            supabase.table("products")
            .select("id", count="exact")
            .eq("business_id", business_id)
            .execute()
        )
        current_count = res.count if hasattr(res, "count") and res.count is not None else len(res.data or [])
    except Exception as exc:
        log.warning("check_product_limit: count query failed for biz %s: %s — allowing", business_id, exc)
        return None   # fail open

    if current_count >= limit:
        plan_info = _get_business_plan(business_id)
        effective = _normalise_tier(
            plan_info["tier"], plan_info["billing_status"], plan_info["trial_ends_at"]
        )
        plan_label = _PLAN_LABELS.get(effective, effective)
        log.info(
            "PRODUCT_LIMIT: biz=%s plan=%s limit=%d current=%d",
            business_id, effective, limit, current_count,
        )
        return {
            "error":       "plan_limit",
            "message":     (
                f"You have reached the {limit}-product limit on your {plan_label} plan. "
                f"Upgrade to add more products."
            ),
            "limit":       limit,
            "current":     current_count,
            "upgrade_url": "/pricing",
        }
    return None
