"""
routes/saas_admin_routes.py — WaziBot SaaS Admin Layer

Endpoints (all require superadmin role):
  GET /admin/saas/overview    — platform-wide KPIs
  GET /admin/saas/tenants     — all business/tenant status
  GET /admin/saas/revenue     — MRR, churn, tier breakdown
  GET /admin/saas/health      — system health snapshot

These routes are COMPLETELY SEPARATE from the existing business dashboard.
They are only accessible to the superadmin role (require_superadmin dep).
They do NOT affect any existing business-facing endpoints.
"""

from fastapi import APIRouter, Depends, HTTPException
import logging

log    = logging.getLogger(__name__)
router = APIRouter()



# ── Overview ──────────────────────────────────────────────────────────────────

@router.get("/admin/saas/overview")
def saas_overview(user=Depends(require_superadmin)):
    """
    Platform-wide SaaS KPIs:
      total_businesses, active_today, messages_sent_today,
      orders_today, revenue_today, tier_breakdown
    """
    try:
        from core.db import supabase
        import datetime as _dt

        today = _dt.datetime.now(_dt.timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0).isoformat()

        biz_res  = supabase.table("businesses").select("id, subscription_tier, is_active").execute()
        businesses = biz_res.data or []

        ord_res  = supabase.table("orders").select("id, total_price, business_id").gte("created_at", today).execute()
        orders_today = ord_res.data or []

        msg_res  = supabase.table("messages").select("id").gte("created_at", today).execute()

        tier_counts: dict = {}
        for biz in businesses:
            t = biz.get("subscription_tier") or "free"
            tier_counts[t] = tier_counts.get(t, 0) + 1

        revenue_today = sum(float(o.get("total_price") or 0) for o in orders_today)

        return {
            "total_businesses":   len(businesses),
            "active_businesses":  sum(1 for b in businesses if b.get("is_active")),
            "messages_today":     len(msg_res.data or []),
            "orders_today":       len(orders_today),
            "revenue_today":      round(revenue_today, 2),
            "tier_breakdown":     tier_counts,
        }
    except Exception as exc:
        log.error("saas_overview error: %s", exc)
        raise HTTPException(500, str(exc))


# ── Tenants ───────────────────────────────────────────────────────────────────

@router.get("/admin/saas/tenants")
def saas_tenants(
    limit:  int = 50,
    offset: int = 0,
    tier:   str = "",
    user=Depends(require_superadmin),
):
    """
    List all businesses (tenants) with their subscription status.
    Filterable by tier. Paginated.
    """
    try:
        from core.db import supabase
        q = (
            supabase.table("businesses")
            .select("id, name, owner_username, subscription_tier, billing_status, is_active, created_at, stripe_customer_id")
            .order("created_at", desc=True)
            .range(offset, offset + limit - 1)
        )
        if tier:
            q = q.eq("subscription_tier", tier)
        res = q.execute()
        return {"tenants": res.data or [], "limit": limit, "offset": offset}
    except Exception as exc:
        log.error("saas_tenants error: %s", exc)
        raise HTTPException(500, str(exc))


# ── Revenue ───────────────────────────────────────────────────────────────────

@router.get("/admin/saas/revenue")
def saas_revenue(user=Depends(require_superadmin)):
    """
    MRR estimate, tier breakdown, and WaziBot platform revenue.

    Note: WaziBot MRR = subscription revenue (Stripe).
    Business order revenue belongs to individual businesses, not WaziBot.
    """
    try:
        from core.db import supabase
        from billing.stripe_service import TIERS

        biz_res = supabase.table("businesses").select(
            "subscription_tier, billing_status"
        ).execute()
        businesses = biz_res.data or []

        mrr = 0.0
        revenue_by_tier: dict = {}
        for biz in businesses:
            t = biz.get("subscription_tier") or "free"
            s = biz.get("billing_status")    or "trialing"
            if s == "active" and t in TIERS:
                monthly = TIERS[t].get("price_monthly", 0)
                mrr    += monthly
                revenue_by_tier[t] = revenue_by_tier.get(t, 0) + monthly

        active_paid = sum(
            1 for b in businesses
            if b.get("billing_status") == "active" and (b.get("subscription_tier") or "free") != "free"
        )
        churned = sum(1 for b in businesses if b.get("billing_status") == "cancelled")

        return {
            "mrr_estimate":    round(mrr, 2),
            "arr_estimate":    round(mrr * 12, 2),
            "active_paid":     active_paid,
            "churned":         churned,
            "revenue_by_tier": revenue_by_tier,
            "note":            "MRR is an estimate based on active subscriptions × monthly price",
        }
    except Exception as exc:
        log.error("saas_revenue error: %s", exc)
        raise HTTPException(500, str(exc))


# ── Health ────────────────────────────────────────────────────────────────────

@router.get("/admin/saas/health")
def saas_health(user=Depends(require_superadmin)):
    """
    System health snapshot:
      db connectivity, total rows, Stripe status, pending handoffs,
      pending payments.
    """
    import os

    checks: dict = {}

    # DB connectivity
    try:
        from core.db import supabase
        supabase.table("businesses").select("id").limit(1).execute()
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"error: {exc}"

    # Stripe configuration
    checks["stripe_configured"] = bool(os.getenv("STRIPE_SECRET_KEY"))
    checks["stripe_webhook_configured"] = bool(os.getenv("STRIPE_WEBHOOK_SECRET"))

    # Pending handoffs
    try:
        from core.db import supabase
        res = supabase.table("carts").select("phone").execute()
        import json
        pending_handoffs = sum(
            1 for r in (res.data or [])
            if (r.get("state_data") or {}).get("state") == "human_handoff"
        )
        checks["pending_handoffs"] = pending_handoffs
    except Exception:
        checks["pending_handoffs"] = "unknown"

    # Pending payments
    try:
        from core.db import supabase
        res = supabase.table("orders").select("id").in_(
            "payment_status", ["awaiting_payment", "payment_review"]
        ).execute()
        checks["pending_payments"] = len(res.data or [])
    except Exception:
        checks["pending_payments"] = "unknown"

    overall = "ok" if checks.get("database") == "ok" else "degraded"
    return {"status": overall, "checks": checks}


# ── Tenant detail ─────────────────────────────────────────────────────────────

@router.get("/admin/saas/tenants/{business_id}")
def saas_tenant_detail(business_id: int, user=Depends(require_superadmin)):
    """
    Full detail for a single tenant: subscription, onboarding, usage stats.
    """
    try:
        from core.db import supabase
        from saas.tenant_features import get_onboarding_status

        biz_res = supabase.table("businesses").select("*").eq("id", business_id).limit(1).execute()
        biz     = (biz_res.data or [{}])[0]
        if not biz:
            raise HTTPException(404, "Business not found")

        prod_count = len((supabase.table("products").select("id").eq("business_id", business_id).execute().data or []))
        ord_count  = len((supabase.table("orders").select("id").eq("business_id", business_id).execute().data or []))
        cust_count = len((supabase.table("customers").select("id").eq("business_id", business_id).execute().data or []))

        return {
            "business":     biz,
            "usage":        {"products": prod_count, "orders": ord_count, "customers": cust_count},
            "onboarding":   get_onboarding_status(business_id),
        }
    except HTTPException:
        raise
    except Exception as exc:
        log.error("saas_tenant_detail error: %s", exc)
        raise HTTPException(500, str(exc))


@router.patch("/admin/saas/tenants/{business_id}/tier")
def saas_set_tenant_tier(
    business_id: int,
    tier:         str,
    user=Depends(require_superadmin),
):
    """Manually override a business's subscription tier (admin use only)."""
    from billing.stripe_service import TIERS
    if tier not in TIERS:
        raise HTTPException(400, f"Invalid tier: {tier}. Valid: {list(TIERS)}")
    try:
        from core.db import supabase
        supabase.table("businesses").update({
            "subscription_tier": tier,
            "billing_status":    "active",
        }).eq("id", business_id).execute()
        log.info("Admin tier override  business=%s  tier=%s  by=%s", business_id, tier, user.get("username"))
        return {"ok": True, "business_id": business_id, "tier": tier}
    except Exception as exc:
        raise HTTPException(500, str(exc))


# ─────────────────────────────────────────────────────────────────────────────
# Demo seeder endpoints
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/admin/saas/seed-demos")
def seed_demos(force: bool = False, _user=Depends(require_superadmin)):
    """Create or reset demo businesses for the marketplace directory."""
    try:
        from saas.demo_seeder import seed_demo_businesses
        result = seed_demo_businesses(force=force)
        return result
    except Exception as exc:
        raise HTTPException(500, str(exc))


@router.delete("/admin/saas/seed-demos")
def clear_demos(_user=Depends(require_superadmin)):
    """Remove all demo businesses."""
    try:
        from saas.demo_seeder import clear_demo_businesses
        result = clear_demo_businesses()
        return result
    except Exception as exc:
        raise HTTPException(500, str(exc))


# ─────────────────────────────────────────────────────────────────────────────
# Schema SQL endpoint — returns the SQL to run in Supabase SQL Editor
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/admin/saas/schema-sql")
def schema_sql(_user=Depends(require_superadmin)):
    """Return the full schema SQL for all SaaS extension columns."""
    sql = """
-- WaziBot SaaS Extension Schema
-- Run this in Supabase SQL Editor (all statements are safe to re-run)

-- Stripe billing columns
ALTER TABLE businesses
  ADD COLUMN IF NOT EXISTS subscription_tier      TEXT DEFAULT 'free',
  ADD COLUMN IF NOT EXISTS billing_status         TEXT DEFAULT 'trialing',
  ADD COLUMN IF NOT EXISTS trial_ends_at          TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS stripe_customer_id     TEXT,
  ADD COLUMN IF NOT EXISTS stripe_subscription_id TEXT,
  ADD COLUMN IF NOT EXISTS features_json          JSONB;

-- Onboarding wizard columns
ALTER TABLE businesses
  ADD COLUMN IF NOT EXISTS onboarding_step        INTEGER DEFAULT 1,
  ADD COLUMN IF NOT EXISTS onboarding_completed   BOOLEAN DEFAULT FALSE;

-- AI role and branding
ALTER TABLE businesses
  ADD COLUMN IF NOT EXISTS ai_role     TEXT DEFAULT 'general',
  ADD COLUMN IF NOT EXISTS tagline     TEXT,
  ADD COLUMN IF NOT EXISTS logo_url    TEXT,
  ADD COLUMN IF NOT EXISTS theme_colour TEXT DEFAULT '#00c853';

-- Cash & Currency settings panel (Store Settings → Payments tab)
ALTER TABLE businesses
  ADD COLUMN IF NOT EXISTS cash_enabled    BOOLEAN DEFAULT TRUE,
  ADD COLUMN IF NOT EXISTS pickup_enabled  BOOLEAN DEFAULT TRUE;

-- Multi-language support
ALTER TABLE user_memory
  ADD COLUMN IF NOT EXISTS preferred_language TEXT DEFAULT 'en';

-- Product descriptions (public store / AI website / marketplace pages)
ALTER TABLE products
  ADD COLUMN IF NOT EXISTS description TEXT;

-- Agent identity (if not already done)
ALTER TABLE messages
  ADD COLUMN IF NOT EXISTS agent_id TEXT;
"""
    return {"sql": sql.strip()}
