"""
services/acquisition_service.py — Customer Acquisition Analytics
════════════════════════════════════════════════════════════════

Tracks how customers discover a business through the shared WhatsApp number:
  - QR code scans      (via /qr/{slug} redirect)
  - WhatsApp link clicks (via /go/{slug} redirect)
  - Conversations started (first message per customer, detected in webhook)

Design principles:
  - NEVER crash the calling code. All functions are fire-and-forget.
  - Synchronous, lightweight — no background workers, no Redis.
  - Reuses existing analytics cache from crud.analytics.
  - Single table: acquisition_events (business_id, event_type, created_at, metadata)

Called by:
  routes/marketing_routes.py  → record_qr_scan, record_whatsapp_click
  routes/webhook_routes.py    → record_conversation_started
  routes/growth_routes.py     → get_acquisition_stats (new endpoint)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

log = logging.getLogger("wazibot")

# ── Safe event recorder ───────────────────────────────────────────────────────

def _record_event(business_id: int, event_type: str, metadata: dict | None = None) -> None:
    """
    Insert one row into acquisition_events.
    Never raises — analytics must never break business operations.
    """
    if not business_id or business_id < 1:
        return
    try:
        from core.db import supabase
        supabase.table("acquisition_events").insert({
            "business_id": business_id,
            "event_type":  event_type,
            "metadata":    metadata or {},
        }).execute()
    except Exception as exc:
        # Log at debug — don't even warn, this is non-critical
        log.debug("acquisition_events insert failed  biz=%s  type=%s  err=%s",
                  business_id, event_type, exc)


def record_qr_scan(business_id: int) -> None:
    """Call when a customer opens /qr/{slug} (before redirect to WhatsApp)."""
    _record_event(business_id, "qr_scan")


def record_whatsapp_click(business_id: int) -> None:
    """Call when a customer opens /go/{slug} (before redirect to WhatsApp)."""
    _record_event(business_id, "whatsapp_click")


def record_conversation_started(business_id: int, phone: str) -> None:
    """
    Call when the FIRST message from a customer arrives in the webhook.
    Only records once per customer — checks if this phone has any prior
    acquisition_events of type conversation_started for this business.
    """
    if not business_id or not phone:
        return
    try:
        from core.db import supabase
        # Check if we've already recorded this customer's first conversation
        existing = (
            supabase.table("acquisition_events")
            .select("id")
            .eq("business_id", business_id)
            .eq("event_type", "conversation_started")
            .eq("metadata->>phone", phone)
            .limit(1)
            .execute()
        )
        if existing.data:
            return  # Already counted — don't double-count
        _record_event(business_id, "conversation_started", {"phone": phone})
    except Exception as exc:
        log.debug("record_conversation_started failed  biz=%s  err=%s", business_id, exc)


# ── Stats query ───────────────────────────────────────────────────────────────

def get_acquisition_stats(business_id: int) -> dict:
    """
    Return acquisition funnel stats for a business.
    Reuses existing order count from analytics cache where possible.
    Returns safe zeros on any error.
    """
    empty = {
        "qr_scans":             0,
        "whatsapp_clicks":      0,
        "conversations_started": 0,
        "orders":               0,
        "conversion_rate":      0,
        "today": {
            "qr_scans": 0, "whatsapp_clicks": 0, "conversations_started": 0
        },
        "this_month": {
            "qr_scans": 0, "whatsapp_clicks": 0, "conversations_started": 0
        },
    }
    try:
        from core.db import supabase

        # Fetch all acquisition events for this business in one query
        events_res = (
            supabase.table("acquisition_events")
            .select("event_type, created_at")
            .eq("business_id", business_id)
            .execute()
        )
        events = events_res.data or []

        now       = datetime.now(timezone.utc)
        today_str = now.date().isoformat()
        month_str = now.strftime("%Y-%m")

        # Aggregate counts
        totals = {"qr_scan": 0, "whatsapp_click": 0, "conversation_started": 0}
        today  = {"qr_scan": 0, "whatsapp_click": 0, "conversation_started": 0}
        month  = {"qr_scan": 0, "whatsapp_click": 0, "conversation_started": 0}

        for e in events:
            et = e.get("event_type", "")
            if et not in totals:
                continue
            totals[et] += 1
            ts = e.get("created_at", "")
            if ts:
                if ts[:10] == today_str:
                    today[et] += 1
                if ts[:7] == month_str:
                    month[et] += 1

        # Reuse existing order count — avoids a duplicate query
        try:
            from crud.analytics import get_business_stats_cached
            stats  = get_business_stats_cached(business_id)
            orders = stats.get("total_orders", 0)
        except Exception:
            orders_res = (
                supabase.table("orders")
                .select("id", count="exact")
                .eq("business_id", business_id)
                .execute()
            )
            orders = orders_res.count or 0

        qr_scans = totals["qr_scan"]
        conversations = totals["conversation_started"]
        # Conversion: orders / qr_scans. Guard against zero.
        conversion = round((orders / qr_scans * 100), 1) if qr_scans > 0 else 0

        return {
            "qr_scans":              qr_scans,
            "whatsapp_clicks":       totals["whatsapp_click"],
            "conversations_started": conversations,
            "orders":                orders,
            "conversion_rate":       conversion,
            "today": {
                "qr_scans":              today["qr_scan"],
                "whatsapp_clicks":       today["whatsapp_click"],
                "conversations_started": today["conversation_started"],
            },
            "this_month": {
                "qr_scans":              month["qr_scan"],
                "whatsapp_clicks":       month["whatsapp_click"],
                "conversations_started": month["conversation_started"],
            },
        }
    except Exception as exc:
        log.warning("get_acquisition_stats error  biz=%s  err=%s", business_id, exc)
        return empty
