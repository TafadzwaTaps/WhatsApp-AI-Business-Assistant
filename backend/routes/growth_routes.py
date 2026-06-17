"""
routes/growth_routes.py — Growth, retention, insights, and scheduled campaigns.

New endpoints (all additive — no existing endpoints modified):

  GET  /retention/summary          — dashboard retention card
  GET  /retention/reorders         — customers due to reorder
  GET  /retention/churn-risk       — VIP/loyal customers going quiet
  GET  /retention/win-back         — ready-to-fire campaign suggestions
  POST /retention/send-win-back    — fire a win-back suggestion campaign

  GET  /insights/growth            — actionable growth card data
  GET  /insights/opportunities     — revenue opportunities list

  GET  /campaigns/scheduled        — list scheduled campaigns
  POST /campaigns/scheduled        — create a scheduled campaign
  DELETE /campaigns/scheduled/{id} — cancel a scheduled campaign
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, validator

import crud
from core.auth import require_business

log = logging.getLogger(__name__)
router = APIRouter()


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 1 — RETENTION ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/retention/summary")
def retention_summary(user=Depends(require_business)):
    """
    All key retention metrics in one call for the dashboard card.
    Returns: reorder counts, churn risk, revenue at risk, win-back suggestions.
    """
    from services.customer_retention import get_retention_summary
    return get_retention_summary(user["business_id"])


@router.get("/retention/reorders")
def retention_reorders(
    urgency: Optional[str] = None,   # overdue | due_soon | upcoming | None=all
    limit:   int = 20,
    user=Depends(require_business),
):
    """
    Customers likely to reorder based on purchase frequency and last order.
    urgency filter: overdue (>2d past window), due_soon (within window), upcoming.
    """
    from services.customer_retention import get_reorder_candidates
    valid = {"overdue", "due_soon", "upcoming", None}
    if urgency not in valid:
        raise HTTPException(400, f"urgency must be one of: overdue, due_soon, upcoming")
    return get_reorder_candidates(user["business_id"], urgency_filter=urgency, limit=limit)


@router.get("/retention/churn-risk")
def retention_churn_risk(
    days: int = 21,   # inactive for this many days = at risk
    user=Depends(require_business),
):
    """
    VIP and Loyal customers who haven't ordered in `days` days.
    These represent real revenue at risk — high priority for win-back campaigns.
    """
    if days < 1 or days > 180:
        raise HTTPException(400, "days must be between 1 and 180")
    from services.customer_retention import get_churn_risk_customers
    return get_churn_risk_customers(user["business_id"], risk_days=days)


@router.get("/retention/win-back")
def retention_win_back(user=Depends(require_business)):
    """
    Ready-to-send campaign suggestions. Each item includes audience, message,
    potential revenue, and can be passed directly to POST /campaigns/send.
    """
    from services.customer_retention import get_win_back_suggestions
    return get_win_back_suggestions(user["business_id"])


class WinBackFireRequest(BaseModel):
    audience:  str
    message:   str
    dry_run:   bool = False


@router.post("/retention/send-win-back")
async def retention_send_win_back(body: WinBackFireRequest, user=Depends(require_business)):
    """
    Fire a win-back campaign suggested by GET /retention/win-back.
    Delegates to the existing CampaignService — no new send logic.
    """
    from services.campaign_service import CampaignService, AUDIENCE_INFO
    bid = user["business_id"]

    if body.audience not in AUDIENCE_INFO:
        raise HTTPException(400, f"Unknown audience: {list(AUDIENCE_INFO.keys())}")
    if len(body.message.strip()) < 5:
        raise HTTPException(400, "Message too short")
    if len(body.message) > 1024:
        raise HTTPException(400, "Message too long (max 1024 chars)")

    result = CampaignService.run(
        business_id=bid,
        audience=body.audience,
        message=body.message,
        personalise_msg=True,
        dry_run=body.dry_run,
    )
    log.info("win_back_sent  biz=%s  audience=%s  sent=%s  dry=%s",
             bid, body.audience, result.get("sent"), body.dry_run)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 5 — GROWTH INSIGHTS ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/insights/growth")
def insights_growth(user=Depends(require_business)):
    """
    Actionable growth card — what a business should focus on RIGHT NOW.
    Pulls from retention, payments, stock, and CRM in a single response.

    Returns:
      {
        reorder_opportunity:  {count, estimated_revenue}
        churn_alert:          {count, revenue_at_risk}
        pending_payments:     {count, total}
        low_stock_impact:     {count, affected_products}
        quick_wins:           [{title, action, value, priority}]
      }
    """
    from services.customer_retention import get_retention_summary

    bid = user["business_id"]

    # Collect all signals
    try:
        retention = get_retention_summary(bid)
    except Exception:
        retention = {}

    try:
        stale_orders = crud.get_stale_payment_orders(bid, older_than_hours=1)
        pending_total = sum(float(o.get("total_price") or 0) for o in stale_orders)
    except Exception:
        stale_orders, pending_total = [], 0.0

    try:
        low_stock = crud.get_low_stock_products(bid)
    except Exception:
        low_stock = []

    # Build quick wins — ordered by business impact
    quick_wins = []

    reorder_count = retention.get("reorder_overdue_count", 0)
    reorder_rev   = retention.get("potential_reorder_revenue", 0.0)
    if reorder_count > 0:
        quick_wins.append({
            "title":    f"{reorder_count} customer{'s' if reorder_count > 1 else ''} likely to reorder",
            "action":   "Send reorder nudge",
            "value":    f"~${reorder_rev:.0f} potential",
            "priority": "high",
            "type":     "reorder",
            "endpoint": "/retention/win-back",
        })

    churn_count = retention.get("churn_risk_count", 0)
    rev_at_risk = retention.get("revenue_at_risk", 0.0)
    if churn_count > 0:
        quick_wins.append({
            "title":    f"{churn_count} loyal customer{'s' if churn_count > 1 else ''} at risk of churning",
            "action":   "Send win-back campaign",
            "value":    f"${rev_at_risk:.0f}/mo at risk",
            "priority": "high",
            "type":     "churn",
            "endpoint": "/retention/churn-risk",
        })

    if stale_orders:
        quick_wins.append({
            "title":    f"{len(stale_orders)} unpaid order{'s' if len(stale_orders) > 1 else ''}",
            "action":   "Send payment reminders",
            "value":    f"${pending_total:.2f} pending",
            "priority": "high",
            "type":     "payments",
            "endpoint": "/payments/reminders/send",
        })

    if low_stock:
        quick_wins.append({
            "title":    f"{len(low_stock)} product{'s' if len(low_stock) > 1 else ''} running low",
            "action":   "Restock before losing sales",
            "value":    ", ".join(p.get("name", "") for p in low_stock[:3]),
            "priority": "medium",
            "type":     "stock",
            "endpoint": "/analytics/low-stock",
        })

    # Sort: high priority first
    quick_wins.sort(key=lambda w: 0 if w["priority"] == "high" else 1)

    return {
        "reorder_opportunity": {
            "count":              retention.get("reorder_overdue_count", 0)
                                + retention.get("reorder_soon_count", 0),
            "overdue_count":      retention.get("reorder_overdue_count", 0),
            "estimated_revenue":  reorder_rev,
        },
        "churn_alert": {
            "count":          churn_count,
            "vip_count":      retention.get("churn_risk_vip_count", 0),
            "revenue_at_risk": rev_at_risk,
        },
        "pending_payments": {
            "count": len(stale_orders),
            "total": round(pending_total, 2),
        },
        "low_stock_impact": {
            "count":             len(low_stock),
            "affected_products": [p.get("name", "") for p in low_stock],
        },
        "quick_wins": quick_wins,
    }


@router.get("/insights/opportunities")
def insights_opportunities(user=Depends(require_business)):
    """
    Detailed revenue opportunities list.
    Combines reorder predictions, payment reminders, and campaign targets.
    """
    from services.customer_retention import predict_reorders, get_churn_risk_customers

    bid = user["business_id"]
    opportunities = []

    # Reorder opportunities
    try:
        reorders = predict_reorders(bid)
        for r in reorders[:10]:
            opportunities.append({
                "type":       "reorder",
                "phone":      r["phone"],
                "name":       r.get("customer_name", ""),
                "description": f"Due for {', '.join(r['likely_products'][:2])}",
                "days_overdue": r["days_overdue"],
                "urgency":    r["urgency"],
                "message":    r["suggested_message"],
            })
    except Exception as exc:
        log.warning("insights_opportunities reorder error: %s", exc)

    # Churn risks
    try:
        churn = get_churn_risk_customers(bid, risk_days=21)
        for r in churn[:5]:
            opportunities.append({
                "type":        "churn",
                "phone":       r["phone"],
                "name":        r.get("customer_name", ""),
                "description": f"{r['segment'].upper()} customer — {r['days_inactive']:.0f} days inactive",
                "days_overdue": r["days_inactive"],
                "urgency":     r["risk_level"],
                "revenue":     r["revenue_at_risk"],
            })
    except Exception as exc:
        log.warning("insights_opportunities churn error: %s", exc)

    # Sort by urgency and days overdue
    opportunities.sort(key=lambda o: (
        0 if o.get("urgency") == "high" else 1,
        -(o.get("days_overdue") or 0)
    ))

    return {"count": len(opportunities), "opportunities": opportunities}


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 6 — SCHEDULED CAMPAIGNS
# ─────────────────────────────────────────────────────────────────────────────

# In-process scheduled campaign store (persisted to Supabase table)
# Table: scheduled_campaigns
#   id, business_id, audience, message, scheduled_at, status, created_at, sent_at


class ScheduledCampaignCreate(BaseModel):
    audience:     str
    message:      str
    scheduled_at: str  # ISO datetime string e.g. "2025-08-15T09:00:00"

    @validator("message")
    def msg_valid(cls, v):
        v = v.strip()
        if len(v) < 3:    raise ValueError("Message too short")
        if len(v) > 1024: raise ValueError("Message too long (max 1024 chars)")
        return v

    @validator("scheduled_at")
    def time_valid(cls, v):
        try:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
            if dt <= datetime.now(timezone.utc):
                raise ValueError("Scheduled time must be in the future")
        except ValueError:
            raise
        except Exception:
            raise ValueError("Invalid datetime format. Use ISO 8601 e.g. 2025-08-15T09:00:00")
        return v


def _get_scheduled_campaigns(business_id: int) -> list[dict]:
    """Fetch scheduled campaigns from Supabase. Returns [] on error."""
    try:
        from core.db import supabase
        res = (
            supabase.table("scheduled_campaigns")
            .select("*")
            .eq("business_id", business_id)
            .order("scheduled_at", desc=False)
            .execute()
        )
        return res.data or []
    except Exception as exc:
        log.warning("_get_scheduled_campaigns error: %s", exc)
        return []


@router.get("/campaigns/scheduled")
def list_scheduled_campaigns(user=Depends(require_business)):
    """
    List all scheduled campaigns for this business.
    Status: pending | sent | failed | cancelled
    """
    campaigns = _get_scheduled_campaigns(user["business_id"])
    return {"count": len(campaigns), "campaigns": campaigns}


@router.post("/campaigns/scheduled", status_code=201)
def create_scheduled_campaign(body: ScheduledCampaignCreate, user=Depends(require_business)):
    """
    Schedule a campaign to send at a future datetime.

    The Render cron job (POST /campaigns/scheduled/run) processes pending
    campaigns on a schedule. Set it up as:
      curl -X POST https://wazibot-api-assistant.onrender.com/campaigns/scheduled/run

    Stores in: scheduled_campaigns table (must exist — see MIGRATION below).
    """
    from services.campaign_service import AUDIENCE_INFO
    bid = user["business_id"]

    if body.audience not in AUDIENCE_INFO:
        raise HTTPException(400, f"Unknown audience. Valid: {list(AUDIENCE_INFO.keys())}")

    try:
        from core.db import supabase
        now = datetime.now(timezone.utc).isoformat()
        res = supabase.table("scheduled_campaigns").insert({
            "business_id":  bid,
            "audience":     body.audience,
            "message":      body.message,
            "scheduled_at": body.scheduled_at,
            "status":       "pending",
            "created_at":   now,
        }).execute()

        campaign = res.data[0] if res.data else {}
        log.info("scheduled_campaign created  id=%s  biz=%s  at=%s",
                 campaign.get("id"), bid, body.scheduled_at)
        return {"ok": True, "campaign": campaign}
    except Exception as exc:
        log.error("create_scheduled_campaign error: %s", exc)
        raise HTTPException(500, f"Failed to schedule campaign: {exc}")


@router.delete("/campaigns/scheduled/{campaign_id}")
def cancel_scheduled_campaign(campaign_id: int, user=Depends(require_business)):
    """Cancel a pending scheduled campaign."""
    bid = user["business_id"]
    try:
        from core.db import supabase
        # Verify ownership + pending status
        res = (
            supabase.table("scheduled_campaigns")
            .select("id, status, business_id")
            .eq("id", campaign_id)
            .eq("business_id", bid)
            .limit(1)
            .execute()
        )
        if not res.data:
            raise HTTPException(404, f"Campaign {campaign_id} not found")
        if res.data[0].get("status") != "pending":
            raise HTTPException(422, "Only pending campaigns can be cancelled")

        supabase.table("scheduled_campaigns") \
            .update({"status": "cancelled"}) \
            .eq("id", campaign_id) \
            .execute()

        return {"ok": True, "campaign_id": campaign_id, "status": "cancelled"}
    except HTTPException:
        raise
    except Exception as exc:
        log.error("cancel_scheduled_campaign error: %s", exc)
        raise HTTPException(500, str(exc))


@router.post("/campaigns/scheduled/run")
async def run_scheduled_campaigns():
    """
    Process all pending campaigns whose scheduled_at has passed.
    Call this from a Render cron job every 15 minutes:
      POST /campaigns/scheduled/run

    No auth required (designed for internal cron — protect with Render's
    cron auth header if exposed publicly).
    """
    from services.campaign_service import CampaignService
    from core.db import supabase

    now = datetime.now(timezone.utc).isoformat()
    processed, sent_total, failed_total = 0, 0, 0

    try:
        res = (
            supabase.table("scheduled_campaigns")
            .select("*")
            .eq("status", "pending")
            .lte("scheduled_at", now)
            .execute()
        )
        due = res.data or []
    except Exception as exc:
        log.error("run_scheduled_campaigns: fetch error: %s", exc)
        return {"ok": False, "error": str(exc)}

    for campaign in due:
        campaign_id = campaign["id"]
        bid         = campaign["business_id"]
        audience    = campaign["audience"]
        message     = campaign["message"]

        try:
            result = CampaignService.run(
                business_id=bid,
                audience=audience,
                message=message,
                personalise_msg=True,
                dry_run=False,
            )
            status     = "sent" if result.get("sent", 0) > 0 else "failed"
            sent_total += result.get("sent", 0)
            failed_total += result.get("failed", 0)
        except Exception as exc:
            log.error("scheduled campaign %s failed: %s", campaign_id, exc)
            status = "failed"

        try:
            supabase.table("scheduled_campaigns").update({
                "status":  status,
                "sent_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", campaign_id).execute()
        except Exception as exc:
            log.warning("scheduled campaign status update failed: %s", exc)

        processed += 1
        log.info("scheduled_campaign processed  id=%s  biz=%s  status=%s",
                 campaign_id, bid, status)

    return {
        "ok":        True,
        "processed": processed,
        "sent":      sent_total,
        "failed":    failed_total,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Feature 3 — GROWTH AUTOMATION STATUS
# Read-only. Does NOT modify cart_recovery.py or reengagement.py logic.
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/growth/status")
def growth_automation_status(user=Depends(require_business)):
    """
    Return the current on/off status and basic stats for growth automations.
    Reads features_json from the business record — no new tables.
    """
    bid = user["business_id"]
    try:
        from core.db import supabase as _sb
        res = (
            _sb.table("businesses")
            .select("features_json")
            .eq("id", bid)
            .limit(1)
            .execute()
        )
        features = (res.data or [{}])[0].get("features_json") or {}

        return {
            "cart_recovery": {
                "enabled":   bool(features.get("cart_recovery_enabled", False)),
                "last_run":  features.get("cart_recovery_last_run"),
                "msgs_sent": features.get("cart_recovery_msgs_sent", 0),
            },
            "reengagement": {
                "enabled":   bool(features.get("reengagement_enabled", False)),
                "last_run":  features.get("reengagement_last_run"),
                "msgs_sent": features.get("reengagement_msgs_sent", 0),
            },
        }
    except Exception as exc:
        import logging as _l
        _l.getLogger("wazibot").warning("growth_status error: %s", exc)
        return {
            "cart_recovery": {"enabled": False, "last_run": None, "msgs_sent": 0},
            "reengagement":  {"enabled": False, "last_run": None, "msgs_sent": 0},
        }


# Feature 1 — admin trigger (superadmin only, for testing)
@router.post("/growth/send-weekly-reports")
def trigger_weekly_reports(user=Depends(require_business)):
    """Admin-only endpoint to manually trigger weekly report emails."""
    try:
        from core.auth import require_superadmin
    except ImportError:
        pass
    try:
        from services.weekly_report_service import send_weekly_reports
        result = send_weekly_reports()
        return {"ok": True, **result}
    except Exception as exc:
        import logging as _l
        _l.getLogger("wazibot").error("manual weekly report error: %s", exc)
        raise
