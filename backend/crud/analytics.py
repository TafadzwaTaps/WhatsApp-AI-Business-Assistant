"""
crud/analytics.py — Analytics stats, CRM segmentation, and payment reminder queries.
"""

from __future__ import annotations

import logging

from core.db import supabase
from crud._helpers import _now
from crud.businesses import get_all_businesses

log = logging.getLogger(__name__)

# ── Sprint 4: Simple in-memory TTL cache ──────────────────────────────────────
# No Redis, no new dependencies. stdlib only.
# Cache is keyed by (function_name, business_id).
# Expires after TTL_SECONDS. Thread-safe for typical single-worker Render deploys.
import time as _time
from typing import Any as _Any

_CACHE_TTL = 60   # seconds
_cache: dict[tuple, tuple[float, _Any]] = {}


def _cache_get(key: tuple) -> tuple[bool, _Any]:
    """Return (hit, value). hit=False if expired or missing."""
    entry = _cache.get(key)
    if entry is None:
        return False, None
    ts, val = entry
    if _time.monotonic() - ts > _CACHE_TTL:
        del _cache[key]
        return False, None
    return True, val


def _cache_set(key: tuple, value: _Any) -> None:
    _cache[key] = (_time.monotonic(), value)


def _cache_invalidate_business(business_id: int) -> None:
    """Call after writes (order/product changes) to clear stale data."""
    stale = [k for k in _cache if len(k) >= 2 and k[1] == business_id]
    for k in stale:
        _cache.pop(k, None)




# ── Admin stats ───────────────────────────────────────────────────────────────

def get_admin_stats() -> dict:
    businesses = get_all_businesses()
    orders_res = supabase.table("orders").select("total_price").execute()
    orders = orders_res.data or []
    return {
        "businesses":        len(businesses),
        "active_businesses": sum(1 for b in businesses if b.get("is_active")),
        "total_orders":      len(orders),
        "total_revenue":     round(sum(float(o.get("total_price") or 0) for o in orders), 2),
    }


# ── Business analytics ────────────────────────────────────────────────────────

def get_top_customers(business_id: int, limit: int = 10) -> list[dict]:
    """
    Return top customers by order count from user_memory.
    Falls back gracefully if user_memory lacks order_count.
    """
    try:
        res = (
            supabase.table("user_memory")
            .select("phone, customer_name, total_spent, order_count, last_seen")
            .eq("business_id", business_id)
            .order("order_count", desc=True)
            .limit(limit)
            .execute()
        )
        return res.data or []
    except Exception as exc:
        log.warning("get_top_customers error: %s", exc)
        return []


def get_low_stock_products(business_id: int) -> list[dict]:
    """
    Return products where stock <= low_stock_threshold.
    Used for dashboard alerts and business-owner WhatsApp notifications.
    """
    try:
        res = (
            supabase.table("products")
            .select("id, name, stock, low_stock_threshold")
            .eq("business_id", business_id)
            .execute()
        )
        products = res.data or []
        low = []
        for p in products:
            stock     = p.get("stock")
            threshold = p.get("low_stock_threshold") or 5
            if stock is not None and stock <= threshold:
                low.append(p)
        return sorted(low, key=lambda x: x.get("stock", 0))
    except Exception as exc:
        log.warning("get_low_stock_products error: %s", exc)
        return []


def get_business_stats(business_id: int) -> dict:
    """
    Lightweight stats aggregation for the analytics dashboard card.
    Returns: total_orders, paid_orders, total_revenue, active_customers,
             pending_orders, ai_handled, human_handled.
    """
    try:
        orders_res = (
            supabase.table("orders")
            .select("id, total_price, payment_status, status")
            .eq("business_id", business_id)
            .execute()
        )
        orders = orders_res.data or []

        total_orders   = len(orders)
        paid_orders    = sum(1 for o in orders if o.get("payment_status") == "paid")
        total_revenue  = sum(float(o.get("total_price") or 0)
                             for o in orders if o.get("payment_status") == "paid")
        pending_orders = sum(1 for o in orders
                             if o.get("status") in ("pending", "confirmed", "pending_cash"))

        cust_res = (
            supabase.table("customers")
            .select("id")
            .eq("business_id", business_id)
            .execute()
        )
        active_customers = len(cust_res.data or [])

        msgs_res = (
            supabase.table("messages")
            .select("sender_type")
            .eq("business_id", business_id)
            .eq("direction", "outgoing")
            .execute()
        )
        msgs          = msgs_res.data or []
        ai_handled    = sum(1 for m in msgs if m.get("sender_type") == "ai")
        human_handled = sum(1 for m in msgs if m.get("sender_type") == "agent")

        return {
            "total_orders":     total_orders,
            "paid_orders":      paid_orders,
            "total_revenue":    round(total_revenue, 2),
            "pending_orders":   pending_orders,
            "active_customers": active_customers,
            "ai_handled":       ai_handled,
            "human_handled":    human_handled,
        }
    except Exception as exc:
        log.warning("get_business_stats error: %s", exc)
    
    return {
            "total_orders": 0, "paid_orders": 0, "total_revenue": 0.0,
            "pending_orders": 0, "active_customers": 0,
            "ai_handled": 0, "human_handled": 0,
        }


# ── Payment reminder helpers ──────────────────────────────────────────────────

def get_stale_payment_orders(
    business_id: int,
    older_than_hours: float = 1.0,
    statuses: list[str] | None = None,
) -> list[dict]:
    """
    Return orders stuck in awaiting_payment / payment_review longer than
    `older_than_hours` hours, so the business can send reminder nudges.
    Never raises — returns [] on any error.
    """
    from datetime import datetime, timezone, timedelta

    if statuses is None:
        statuses = ["awaiting_payment", "payment_review"]

    try:
        res = (
            supabase.table("orders")
            .select(
                "id, business_id, customer_phone, total_price, "
                "payment_method, payment_status, payment_reference, "
                "status, created_at, items"
            )
            .eq("business_id", business_id)
            .in_("payment_status", statuses)
            .order("created_at", desc=False)
            .execute()
        )
        rows = res.data or []

        cutoff = datetime.now(timezone.utc) - timedelta(hours=older_than_hours)
        stale  = []
        for row in rows:
            created_raw = row.get("created_at") or ""
            try:
                created_raw = created_raw.replace("Z", "+00:00")
                created_at  = datetime.fromisoformat(created_raw)
                if created_at <= cutoff:
                    stale.append(row)
            except (ValueError, TypeError):
                stale.append(row)

        log.debug(
            "get_stale_payment_orders  biz=%s  checked=%d  stale=%d  cutoff_h=%.1f",
            business_id, len(rows), len(stale), older_than_hours,
        )
        return stale
    except Exception as exc:
        log.error("get_stale_payment_orders error: %s", exc)
        return []


def get_stale_payment_orders_all_businesses(
    older_than_hours: float = 1.0,
    statuses: list[str] | None = None,
) -> list[dict]:
    """
    Platform-wide version — used by the super-admin reminder endpoint.
    Returns stale orders across ALL active businesses.
    """
    from datetime import datetime, timezone, timedelta

    if statuses is None:
        statuses = ["awaiting_payment", "payment_review"]

    try:
        res = (
            supabase.table("orders")
            .select(
                "id, business_id, customer_phone, total_price, "
                "payment_method, payment_status, payment_reference, "
                "status, created_at"
            )
            .in_("payment_status", statuses)
            .order("created_at", desc=False)
            .execute()
        )
        rows = res.data or []

        cutoff = datetime.now(timezone.utc) - timedelta(hours=older_than_hours)
        stale  = []
        for row in rows:
            created_raw = (row.get("created_at") or "").replace("Z", "+00:00")
            try:
                if datetime.fromisoformat(created_raw) <= cutoff:
                    stale.append(row)
            except (ValueError, TypeError):
                stale.append(row)

        return stale
    except Exception as exc:
        log.error("get_stale_payment_orders_all_businesses error: %s", exc)
        return []


# ── CRM segmentation ──────────────────────────────────────────────────────────

def get_customer_segment(memory: dict) -> str:
    """
    Classify a customer into a segment based on their memory profile.

    Segments
    ────────
    "vip"      — order_count ≥ 10  OR  total_spent ≥ 50
    "loyal"    — order_count ≥ 5   OR  total_spent ≥ 20
    "regular"  — order_count ≥ 2
    "new"      — order_count == 1
    "prospect" — order_count == 0

    Pure function — no DB calls.
    """
    count = int(memory.get("order_count", 0) or 0)
    spent = float(memory.get("total_spent", 0) or 0)

    if count >= 10 or spent >= 50:
        return "vip"
    if count >= 5 or spent >= 20:
        return "loyal"
    if count >= 2:
        return "regular"
    if count == 1:
        return "new"
    return "prospect"


def get_segment_label(segment: str) -> str:
    """Human-readable label for a segment (used in dashboard/messages)."""
    return {
        "vip":      "⭐ VIP Customer",
        "loyal":    "💚 Loyal Customer",
        "regular":  "👍 Regular Customer",
        "new":      "👋 New Customer",
        "prospect": "🔍 Prospect",
    }.get(segment, "Customer")


def get_customers_by_segment(
    business_id: int,
    segment: str,
) -> list[dict]:
    """
    Return customers in a given segment.
    segment — one of: "vip", "loyal", "regular", "new", "prospect", "all"
    Returns list of dicts: {phone, customer_name, order_count, total_spent, last_seen}
    """
    try:
        res = (
            supabase.table("user_memory")
            .select("phone, customer_name, order_count, total_spent, last_seen")
            .eq("business_id", business_id)
            .execute()
        )
        rows = res.data or []

        if segment == "all":
            return rows

        result = []
        for row in rows:
            seg = get_customer_segment(row)
            if seg == segment:
                result.append(row)

        return sorted(result, key=lambda r: float(r.get("total_spent") or 0), reverse=True)
    except Exception as exc:
        log.warning("get_customers_by_segment error: %s", exc)
        return []


def get_inactive_customers(
    business_id: int,
    inactive_days: int = 30,
    min_order_count: int = 1,
) -> list[dict]:
    """
    Return customers who have not been seen in `inactive_days` days.
    Only returns customers who have placed at least `min_order_count` orders.
    Used by the campaign engine to target win-back messages.
    """
    from datetime import datetime, timezone, timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=inactive_days)).isoformat()
    try:
        res = (
            supabase.table("user_memory")
            .select("phone, customer_name, order_count, total_spent, last_seen")
            .eq("business_id", business_id)
            .lt("last_seen", cutoff)
            .gte("order_count", min_order_count)
            .order("last_seen", desc=False)
            .execute()
        )
        return res.data or []
    except Exception as exc:
        log.warning("get_inactive_customers error: %s", exc)
        return []


def get_segment_summary(business_id: int) -> dict:
    """
    Return a count breakdown of all customer segments for a business.
    Used by the dashboard CRM card.
    Returns: {vip: N, loyal: N, regular: N, new: N, prospect: N, total: N}
    """
    try:
        res = (
            supabase.table("user_memory")
            .select("order_count, total_spent")
            .eq("business_id", business_id)
            .execute()
        )
        rows   = res.data or []
        counts = {"vip": 0, "loyal": 0, "regular": 0, "new": 0, "prospect": 0}
        for row in rows:
            seg = get_customer_segment(row)
            counts[seg] = counts.get(seg, 0) + 1
        counts["total"] = len(rows)
        return counts
    except Exception as exc:
        log.warning("get_segment_summary error: %s", exc)
        return {"vip": 0, "loyal": 0, "regular": 0, "new": 0, "prospect": 0, "total": 0}

def get_business_stats_cached(business_id: int) -> dict:
    """
    Sprint 4: Cached version of get_business_stats.
    60-second TTL per business. Falls back to live query on any error.
    Called by routes/business_routes.py /analytics/stats endpoint.
    """
    _ck = ("get_business_stats", business_id)
    hit, cached = _cache_get(_ck)
    if hit:
        return cached
    result = get_business_stats(business_id)
    _cache_set(_ck, result)
    return result


def get_segment_summary_cached(business_id: int) -> dict:
    """
    Sprint 4: Cached version of get_segment_summary.
    """
    _ck = ("get_segment_summary", business_id)
    hit, cached = _cache_get(_ck)
    if hit:
        return cached
    result = get_segment_summary(business_id)
    _cache_set(_ck, result)
    return result

def get_satisfaction_score(business_id: int) -> dict:
    """
    Sprint 5 — Customer Satisfaction Score.
    Reads last_rating from user_memory. No new tables or surveys.
    Returns: {avg_rating, rated_count, total_customers}
    """
    try:
        res = (
            supabase.table("user_memory")
            .select("last_rating")
            .eq("business_id", business_id)
            .execute()
        )
        rows = res.data or []
        def _to_float(val) -> float | None:
            """Safely convert a rating value to float. Returns None if blank or non-numeric."""
            if val is None: return None
            s = str(val).strip()
            if not s: return None
            try: return float(s)
            except (ValueError, TypeError): return None

        ratings = [
            v for r in rows
            if (v := _to_float(r.get("last_rating"))) is not None
        ]
        avg = round(sum(ratings) / len(ratings), 1) if ratings else None
        return {
            "avg_rating":      avg,
            "rated_count":     len(ratings),
            "total_customers": len(rows),
        }
    except Exception as exc:
        log.warning("get_satisfaction_score error: %s", exc)
        return {"avg_rating": None, "rated_count": 0, "total_customers": 0}
