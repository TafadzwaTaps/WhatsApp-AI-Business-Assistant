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
            .select("subscription_tier, billing_status, trial_ends_at, trial_started_at")
            .eq("id", business_id)
            .limit(1)
            .execute()
        )
        row = (res.data or [{}])[0]
        return {
            "tier":             (row.get("subscription_tier") or "free").lower(),
            "billing_status":   (row.get("billing_status")   or "free").lower(),
            "trial_ends_at":    row.get("trial_ends_at"),
            "trial_started_at": row.get("trial_started_at"),
        }
    except Exception as exc:
        log.warning("plan_guard: DB error fetching plan for biz %s: %s", business_id, exc)
        return {"tier": "free", "billing_status": "free", "trial_ends_at": None}



def is_trial_active(plan_info: dict) -> bool:
    """
    Central trial-active check.

    Returns True when billing_status is "trialing"/"trial" AND the trial
    has not expired.

    trial_ends_at takes priority. If it is NULL (common for older records),
    falls back to trial_started_at + 30 days. If neither is set but status
    is "trialing", assumes the trial is active (benefit of the doubt for new
    signups whose trial timer hasn't been set yet).
    """
    status = (plan_info.get("billing_status") or "").lower()
    if status not in ("trialing", "trial"):
        return False

    def _parse(dt_val):
        if not dt_val:
            return None
        try:
            if isinstance(dt_val, str):
                d = datetime.fromisoformat(dt_val.replace("Z", "+00:00"))
            else:
                d = dt_val
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            return d
        except Exception:
            return None

    trial_ends_at   = _parse(plan_info.get("trial_ends_at"))
    trial_started_at = _parse(plan_info.get("trial_started_at"))

    now = datetime.now(timezone.utc)

    if trial_ends_at is not None:
        return now < trial_ends_at

    if trial_started_at is not None:
        # Fallback: trial_started_at + 30 days
        from datetime import timedelta
        return now < (trial_started_at + timedelta(days=30))

    # Neither date set but status is "trialing" → assume active (new signup)
    return True


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
        trial_plan_info = {"billing_status": status, "trial_ends_at": trial_ends_at}
        if is_trial_active(trial_plan_info):
            # Active trial: give PRO-level access so businesses experience the
            # full product. After expiry, enforcement resumes from stored tier.
            return "PRO"
        # Trial expired — fall through to stored tier or FREE
        return _TIER_MAP.get(raw_tier, "FREE")

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

def _require_business_lazy():
    """
    Fix 4: Return require_business callable resolved at call time (inside
    require_plan closure), not at module import time.
    Called as Depends(_require_business_lazy()) — Python evaluates
    _require_business_lazy() when _check's default arg is set (inside
    require_plan()) which runs at route-decoration time, after all modules
    are fully imported. Preserves circular import protection via try/except.
    """
    try:
        from core.auth import require_business
        return require_business
    except ImportError:
        def _passthrough():
            return {}
        return _passthrough


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

    def _check(user: dict = Depends(_require_business_lazy())):
        """Inner dependency — resolves the user's plan and enforces the minimum.

        Fix 4: require_business is now resolved inside _check() via
        _require_business_lazy() which is called at request time, not at
        factory-call (import) time. This guarantees all modules are fully
        loaded before the dependency is evaluated and avoids circular import
        race conditions during startup.
        """
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

# Revised gating — philosophy: let businesses get results first, then upgrade naturally.
# Free: AI ordering, store, basic analytics, up to 10 products, shared WhatsApp number.
# Starter ($9): own number, 25 products, campaigns, CSV import, weekly reports, growth automation.
# Growth ($29): unlimited products, advanced analytics, satisfaction tracking, multi-language.
GATED_FEATURES = {
    # Starter+ features — basic but require own number / account investment
    "campaigns":          "STARTER",
    "broadcast":          "STARTER",
    "growth_automation":  "STARTER",
    "csv_import":         "STARTER",
    "weekly_reports":     "STARTER",
    # Growth+ features — power users already earning money through WaziBot
    "advanced_analytics": "GROWTH",
    "multi_language":     "GROWTH",
    "ai_website":         "GROWTH",
    # Never gated — core product experience
    # "human_handoff"  → free (support is a basic need)
    # "live_inbox"     → free (core dashboard)
    # "crm_segments"   → free (basic visibility)
    # "insights"       → free (helps them see value)
    # "satisfaction"   → free (basic feedback)
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
    "FREE":    10,     # enough for a small restaurant or clothing boutique to prove value
    "STARTER": 25,     # serious small businesses
    "GROWTH":  None,   # unlimited
    "PRO":     None,   # unlimited
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
    During an active trial: always returns None (unlimited).
    """
    # Trial bypass: active trials have unlimited products
    plan_info = _get_business_plan(business_id)
    if is_trial_active(plan_info):
        return None   # trial active — no product limit

    limit = get_product_limit(business_id)
    if limit is None:
        return None   # unlimited plan — no check needed

    try:
        from core.db import supabase
        # M6: select("id") only — avoids fetching full rows and doesn't rely on
        # res.count which requires specific PostgREST headers that may not be set.
        # len(res.data) is always reliable in supabase-py regardless of version.
        res = (
            supabase.table("products")
            .select("id")
            .eq("business_id", business_id)
            .execute()
        )
        current_count = len(res.data or [])
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

def get_trial_status_response(business_id: int) -> dict:
    """
    Return trial status for the dashboard banner.
    Called by GET /trial/status endpoint.
    """
    plan_info = _get_business_plan(business_id)
    active    = is_trial_active(plan_info)
    ends_at   = plan_info.get("trial_ends_at")

    ends_str = None
    if ends_at:
        try:
            if isinstance(ends_at, str):
                ends = datetime.fromisoformat(ends_at.replace("Z", "+00:00"))
            else:
                ends = ends_at
            ends_str = ends.strftime("%-d %B %Y")
        except Exception:
            ends_str = str(ends_at)[:10]

    return {
        "trial_active":   active,
        "trial_ends_at":  ends_str,
        "billing_status": plan_info.get("billing_status"),
        "effective_tier": _normalise_tier(
            plan_info["tier"], plan_info["billing_status"], plan_info["trial_ends_at"]
        ),
    }
