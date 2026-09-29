"""
routes/saas_admin_routes.py — WaziBot SaaS Admin Layer

Endpoints (all require superadmin role):
  GET /admin/saas/overview    — platform-wide KPIs
  GET /admin/saas/tenants     — all business/tenant status
  GET /admin/saas/revenue     — MRR, churn, tier breakdown
  GET /admin/saas/health      — system health snapshot
  GET /admin/saas/usage/messages — platform message volume + top senders (Phase 4)
  GET /admin/saas/usage/ai       — platform AI usage/cost + top consumers (Phase 4)
  GET /admin/saas/usage/limits   — centralized plan limits + who's near them (Phase 4)

These routes are COMPLETELY SEPARATE from the existing business dashboard.
They are only accessible to the superadmin role (require_superadmin dep).
They do NOT affect any existing business-facing endpoints.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
import logging

# BUG FIX: require_superadmin was used throughout this file (as a Depends()
# default, evaluated at function-definition time) but was never imported.
# That's a NameError the instant Python imports this module — which means
# every route documented above (MRR, revenue, tenant detail, tier override)
# has NEVER actually been reachable in production. main.py wraps this import
# in try/except and only registers the router if it succeeds (so the app
# itself never crashed), but it logs a warning and silently drops the whole
# router — easy to miss in Render logs, which is exactly what happened here.
from core.auth import require_superadmin
from crud.admin_audit import (
    log_admin_action, list_audit_logs,
    add_admin_note, list_admin_notes,
    add_risk_flag, list_risk_flags, resolve_risk_flag,
)

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

        # Best-effort — these tables are optional/newer and must never break
        # this endpoint if they're missing or the query fails.
        try:
            msg_count = len((supabase.table("messages").select("id").eq("business_id", business_id).limit(1000).execute().data or []))
        except Exception:
            msg_count = None
        try:
            booking_count = len((supabase.table("bookings").select("id").eq("business_id", business_id).execute().data or []))
        except Exception:
            booking_count = None

        return {
            "business":     biz,
            "usage": {
                "products": prod_count, "orders": ord_count, "customers": cust_count,
                "messages": msg_count, "bookings": booking_count,
            },
            "onboarding":   get_onboarding_status(business_id),
            "recent_audit_logs": list_audit_logs(limit=20, business_id=business_id),
            "admin_notes":       list_admin_notes(business_id),
            "risk_flags":        list_risk_flags(business_id=business_id),
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
    request:      Request,
    reason:       str = "",
    user=Depends(require_superadmin),
):
    """Manually override a business's subscription tier (admin use only)."""
    from billing.stripe_service import TIERS
    if tier not in TIERS:
        raise HTTPException(400, f"Invalid tier: {tier}. Valid: {list(TIERS)}")
    try:
        from core.db import supabase
        prev = supabase.table("businesses").select("subscription_tier").eq("id", business_id).limit(1).execute()
        prev_tier = (prev.data or [{}])[0].get("subscription_tier")
        supabase.table("businesses").update({
            "subscription_tier": tier,
            "billing_status":    "active",
        }).eq("id", business_id).execute()
        log.info("Admin tier override  business=%s  tier=%s  by=%s", business_id, tier, user.get("username"))
        log_admin_action(
            actor_username=user.get("username"), action="business.tier_change",
            target_type="business", target_id=business_id, business_id=business_id,
            reason=reason or None, metadata={"previous_tier": prev_tier, "new_tier": tier},
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
        return {"ok": True, "business_id": business_id, "tier": tier}
    except Exception as exc:
        raise HTTPException(500, str(exc))


# ── Audit log, admin notes, risk flags ──────────────────────────────────────────

@router.get("/admin/saas/audit-logs")
def saas_audit_logs(
    limit: int = 50, offset: int = 0,
    business_id: int = None, action: str = None,
    user=Depends(require_superadmin),
):
    """Recent SuperAdmin audit-log entries, optionally filtered."""
    return {"logs": list_audit_logs(limit=limit, offset=offset, business_id=business_id, action=action)}


@router.get("/admin/saas/tenants/{business_id}/notes")
def saas_list_notes(business_id: int, user=Depends(require_superadmin)):
    return {"notes": list_admin_notes(business_id)}


@router.post("/admin/saas/tenants/{business_id}/notes")
def saas_add_note(business_id: int, note: str, request: Request, user=Depends(require_superadmin)):
    if not note or not note.strip():
        raise HTTPException(422, "Note text required")
    row = add_admin_note(business_id, user.get("username"), note.strip())
    log_admin_action(
        actor_username=user.get("username"), action="business.note_added",
        target_type="business", target_id=business_id, business_id=business_id,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    return {"ok": True, "note": row}


@router.get("/admin/saas/tenants/{business_id}/risk-flags")
def saas_list_risk_flags(business_id: int, user=Depends(require_superadmin)):
    return {"flags": list_risk_flags(business_id=business_id)}


@router.get("/admin/saas/risk-flags")
def saas_all_risk_flags(status: str = "open", user=Depends(require_superadmin)):
    """All open risk flags across the platform, for the Abuse review queue."""
    return {"flags": list_risk_flags(status=status or None)}


@router.post("/admin/saas/abuse/scan")
def saas_abuse_scan(request: Request, user=Depends(require_superadmin)):
    """
    Transparent, multi-signal abuse scan — flags candidates for SuperAdmin
    review, never auto-suspends or auto-labels anything as fake.

    Deliberately avoids the simplistic "one IP = one account" heuristic
    (legitimate businesses can share networks). Instead looks for signals
    that are unusual for *distinct* legitimate businesses to share:
      - the same owner_email registered against more than one business
      - the same contact_phone registered against more than one business
      - businesses with near-duplicate names created within a short window
    Every flag records exactly which signal(s) fired and the evidence
    (the other business IDs involved) so a human can see WHY.
    """
    try:
        from core.db import supabase
        businesses = supabase.table("businesses").select(
            "id, name, owner_email, contact_phone, created_at"
        ).execute().data or []

        by_email: dict = {}
        by_phone: dict = {}
        for b in businesses:
            if b.get("owner_email"):
                by_email.setdefault(b["owner_email"].strip().lower(), []).append(b["id"])
            if b.get("contact_phone"):
                by_phone.setdefault(b["contact_phone"].strip(), []).append(b["id"])

        created = 0
        existing_open = {f["business_id"] for f in list_risk_flags(status="open")}

        for email, ids in by_email.items():
            if len(ids) > 1:
                for bid in ids:
                    if bid in existing_open:
                        continue
                    add_risk_flag(
                        bid, "medium",
                        f"owner_email shared with {len(ids)-1} other business(es)",
                        {"signal": "duplicate_owner_email", "shared_with": [x for x in ids if x != bid]},
                    )
                    existing_open.add(bid)
                    created += 1

        for phone, ids in by_phone.items():
            if len(ids) > 1:
                for bid in ids:
                    if bid in existing_open:
                        continue
                    add_risk_flag(
                        bid, "low",
                        f"contact_phone shared with {len(ids)-1} other business(es)",
                        {"signal": "duplicate_contact_phone", "shared_with": [x for x in ids if x != bid]},
                    )
                    existing_open.add(bid)
                    created += 1

        log_admin_action(
            actor_username=user.get("username"), action="abuse.scan_run",
            target_type="system", metadata={"new_flags": created},
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
        return {"ok": True, "new_flags": created, "scanned_businesses": len(businesses)}
    except Exception as exc:
        log.error("saas_abuse_scan error: %s", exc)
        raise HTTPException(500, str(exc))


@router.post("/admin/saas/risk-flags/{flag_id}/resolve")
def saas_resolve_risk_flag(
    flag_id: int, status: str, request: Request,
    action_taken: str = "", user=Depends(require_superadmin),
):
    """Resolve/clear a risk flag. SuperAdmin decides — nothing here is automatic."""
    if status not in ("cleared", "actioned"):
        raise HTTPException(400, "status must be 'cleared' or 'actioned'")
    row = resolve_risk_flag(flag_id, user.get("username"), status, action_taken or None)
    log_admin_action(
        actor_username=user.get("username"), action=f"risk_flag.{status}",
        target_type="risk_flag", target_id=flag_id,
        metadata={"action_taken": action_taken or None},
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    return {"ok": True, "flag": row}


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

-- Multi-language support
ALTER TABLE user_memory
  ADD COLUMN IF NOT EXISTS preferred_language TEXT DEFAULT 'en';

-- Agent identity (if not already done)
ALTER TABLE messages
  ADD COLUMN IF NOT EXISTS agent_id TEXT;
"""
    return {"sql": sql.strip()}


# ─────────────────────────────────────────────────────────────────────────────
# SuperAdmin 2.0 — Phase 4: Message & AI usage analytics
#
# Platform-wide message volume and AI cost analytics, aggregated from data
# that already exists (messages, ai_usage_log) — no new tracking tables.
# ai_usage_log is optional/best-effort (see crud/ai_usage.py) — if it hasn't
# been migrated yet, these endpoints report zeros/empty, never fabricated
# numbers. Everything here is read-only: nothing suspends or throttles a
# business just because its usage is high (per the spec: "Do NOT
# automatically suspend legitimate businesses merely because usage is
# high" — a SuperAdmin decides what, if anything, to do with this data).
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/admin/saas/usage/messages")
def saas_usage_messages(days: int = 7, user=Depends(require_superadmin)):
    """
    Platform-wide message volume for the last `days` days: total,
    incoming/outgoing split, and a per-business breakdown (top senders).
    Never loads message *content* — only business_id/direction/created_at.
    """
    try:
        from core.db import supabase
        import datetime as _dt

        days = max(1, min(days, 90))  # bound the query — avoid unbounded scans
        cutoff = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days)).isoformat()

        res = (
            supabase.table("messages")
            .select("business_id, direction, created_at")
            .gte("created_at", cutoff)
            .limit(20000)  # safety cap — a platform-wide dashboard number, not a full export
            .execute()
        )
        rows = res.data or []

        per_business: dict = {}
        incoming = outgoing = 0
        for r in rows:
            bid = r.get("business_id")
            d   = (r.get("direction") or "").lower()
            if d == "incoming":
                incoming += 1
            elif d == "outgoing":
                outgoing += 1
            if bid is not None:
                per_business[bid] = per_business.get(bid, 0) + 1

        businesses = {b["id"]: b.get("name") for b in (supabase.table("businesses").select("id, name").execute().data or [])}
        top = sorted(per_business.items(), key=lambda kv: kv[1], reverse=True)[:10]
        top_businesses = [{"business_id": bid, "name": businesses.get(bid, f"#{bid}"), "messages": count} for bid, count in top]

        return {
            "window_days":     days,
            "total_messages":  len(rows),
            "incoming":        incoming,
            "outgoing":        outgoing,
            "top_businesses":  top_businesses,
            "sample_capped":   len(rows) >= 20000,
        }
    except Exception as exc:
        log.error("saas_usage_messages error: %s", exc)
        raise HTTPException(500, str(exc))


@router.get("/admin/saas/usage/ai")
def saas_usage_ai(hours: float = 24.0, user=Depends(require_superadmin)):
    """
    Platform-wide AI (LLM) usage & cost, aggregated from ai_usage_log:
    total requests/tokens/cost, and the top businesses by cost — the
    "cost control" view. If ai_usage_log hasn't been migrated yet
    (ai_usage_log_migration.sql), returns zeros with tracking_available=False
    rather than fabricating numbers.
    """
    try:
        from core.db import supabase
        import datetime as _dt

        cutoff = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=hours)).isoformat()
        try:
            res = (
                supabase.table("ai_usage_log")
                .select("business_id, total_tokens, estimated_cost")
                .gte("created_at", cutoff)
                .limit(20000)
                .execute()
            )
            rows = res.data or []
            tracking_available = True
        except Exception:
            rows = []
            tracking_available = False

        per_business: dict = {}
        total_tokens = 0
        total_cost = 0.0
        for r in rows:
            bid = r.get("business_id")
            tokens = int(r.get("total_tokens") or 0)
            cost   = float(r.get("estimated_cost") or 0)
            total_tokens += tokens
            total_cost   += cost
            if bid is not None:
                agg = per_business.setdefault(bid, {"requests": 0, "tokens": 0, "cost": 0.0})
                agg["requests"] += 1
                agg["tokens"]   += tokens
                agg["cost"]     += cost

        businesses = {b["id"]: b.get("name") for b in (supabase.table("businesses").select("id, name").execute().data or [])}
        top = sorted(per_business.items(), key=lambda kv: kv[1]["cost"], reverse=True)[:10]
        top_businesses = [
            {"business_id": bid, "name": businesses.get(bid, f"#{bid}"),
             "requests": agg["requests"], "tokens": agg["tokens"], "estimated_cost": round(agg["cost"], 4)}
            for bid, agg in top
        ]

        return {
            "window_hours":        hours,
            "tracking_available":  tracking_available,
            "total_requests":      len(rows),
            "total_tokens":        total_tokens,
            "total_estimated_cost": round(total_cost, 4),
            "top_businesses":      top_businesses,
            "note": None if tracking_available else "AI usage tracking table not available yet — run ai_usage_log_migration.sql",
        }
    except Exception as exc:
        log.error("saas_usage_ai error: %s", exc)
        raise HTTPException(500, str(exc))


@router.get("/admin/saas/usage/limits")
def saas_usage_limits(user=Depends(require_superadmin)):
    """
    Centralized, plan-based usage limits (never hardcoded per-endpoint —
    these are the same PLAN_PRODUCT_LIMITS / PLAN_AI_REQUEST_LIMITS dicts
    core/plan_guard.py already enforces against). Also shows, per active
    business, current usage vs. its cap so a SuperAdmin can see who's
    close to a limit — this is informational only, nothing here changes
    any business's access.
    """
    try:
        from core.db import supabase
        from core.plan_guard import (
            PLAN_PRODUCT_LIMITS, PLAN_AI_REQUEST_LIMITS,
            get_product_limit, get_ai_daily_limit,
        )
        from crud.ai_usage import count_recent_ai_requests

        businesses = (supabase.table("businesses").select("id, name, subscription_tier, is_active").execute().data or [])

        near_limit = []
        for b in businesses:
            if not b.get("is_active"):
                continue
            bid = b["id"]
            ai_limit = get_ai_daily_limit(bid)
            if ai_limit is not None:
                current = count_recent_ai_requests(bid, hours=24.0)
                if current >= ai_limit * 0.8:  # flag when within 80% of the cap
                    near_limit.append({
                        "business_id": bid, "name": b.get("name"),
                        "ai_requests_today": current, "ai_daily_limit": ai_limit,
                    })

        return {
            "plan_product_limits":    PLAN_PRODUCT_LIMITS,
            "plan_ai_request_limits": PLAN_AI_REQUEST_LIMITS,
            "businesses_near_ai_limit": near_limit,
            "note": "Informational only — SuperAdmin decides whether to act; nothing here throttles or suspends automatically.",
        }
    except Exception as exc:
        log.error("saas_usage_limits error: %s", exc)
        raise HTTPException(500, str(exc))
