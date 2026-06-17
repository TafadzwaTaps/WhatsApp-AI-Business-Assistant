"""
services/customer_retention.py — Customer Retention Engine

PURPOSE
───────
Identifies reorder opportunities, churn-risk customers, and win-back
targets using ONLY existing user_memory data. No ML — simple, reliable
business logic that works on the data already collected.

KEY FUNCTIONS
─────────────
  predict_reorders(business_id)        → customers likely to reorder soon
  get_churn_risk_customers(business_id) → VIP/loyal customers going quiet
  generate_reorder_message(memory, biz) → personalised nudge message
  get_win_back_suggestions(business_id) → ready-to-send campaign payloads
  get_retention_summary(business_id)   → dashboard card data

DESIGN RULES
────────────
- Read-only: never writes to DB
- Never raises: all functions return safe defaults on error
- Uses only crud.get_user_memory-style data (user_memory rows)
- No external HTTP calls
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Optional

log = logging.getLogger(__name__)


# ── Reorder interval heuristics ───────────────────────────────────────────────
# Maps product-name keywords → typical reorder interval in days.
# Conservative estimates — better to nudge a day early than a day late.

_REORDER_INTERVALS: list[tuple[re.Pattern, int]] = [
    # Daily staples
    (re.compile(r"bread|roll|bun|loaf",                  re.I), 7),
    (re.compile(r"milk|dairy",                            re.I), 7),
    (re.compile(r"egg",                                   re.I), 7),

    # Weekly
    (re.compile(r"sadza|mealie|maize|flour",              re.I), 14),
    (re.compile(r"rice|pasta|noodle|spaghetti",           re.I), 14),

    # Bi-weekly / monthly grocery
    (re.compile(r"oil|cooking.oil|vegetable.oil",         re.I), 30),
    (re.compile(r"sugar|salt|spice|condiment",            re.I), 30),
    (re.compile(r"soap|detergent|washing|cleaning",       re.I), 30),

    # Monthly / refills
    (re.compile(r"medicine|tablet|vitamin|supplement|pill|capsule|syrup", re.I), 30),
    (re.compile(r"baby|nappy|diaper|formula",             re.I), 30),
    (re.compile(r"shampoo|toothpaste|lotion|cream|gel",  re.I), 30),

    # Fast food / restaurant — short cycle
    (re.compile(r"pizza|burger|chicken|fish|chips|fries", re.I), 7),
    (re.compile(r"drink|juice|water|soda|cola|beer",      re.I), 7),
    (re.compile(r"coffee|tea|hot.drink",                  re.I), 3),
]

# Default fallback if no keyword matches (days)
_DEFAULT_INTERVAL = 21


def _guess_interval(product_name: str) -> int:
    """Return the expected reorder interval (days) for a product name."""
    for pattern, days in _REORDER_INTERVALS:
        if pattern.search(product_name):
            return days
    return _DEFAULT_INTERVAL


def _parse_last_seen(raw: str | None) -> Optional[datetime]:
    """Safely parse an ISO timestamp. Returns None on any error."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None


def _days_since(dt: datetime) -> float:
    return (datetime.now(timezone.utc) - dt).total_seconds() / 86400


# ── Core functions ─────────────────────────────────────────────────────────────

def predict_reorders(business_id: int) -> list[dict]:
    """
    Return customers who are likely due to reorder based on their
    purchase frequency and time since last order.

    Each result dict:
      {
        phone, customer_name, order_count, total_spent, last_seen,
        likely_products: [str],      # product names they usually order
        days_overdue: float,         # positive = overdue, negative = not yet
        urgency: "overdue"|"due_soon"|"upcoming",
        suggested_message: str,
      }

    Never raises — returns [] on any error.
    """
    import crud

    try:
        rows = crud.get_customers_by_segment(business_id, "all")
        biz  = crud.get_business_by_id(business_id)
        biz_name = biz.get("name", "our store") if biz else "our store"
    except Exception as exc:
        # M4: [Errno 11] "Resource temporarily unavailable" is a transient
        # connection-pool exhaustion — log at DEBUG to reduce noise.
        # All other unexpected errors stay at WARNING level.
        _msg = str(exc)
        if "temporarily unavailable" in _msg or "Errno 11" in _msg or "EAGAIN" in _msg:
            log.debug("predict_reorders: transient db error (pool busy): %s", exc)
        else:
            log.warning("predict_reorders: db error: %s", exc)
        return []

    results = []

    for row in rows:
        phone       = row.get("phone", "")
        order_count = int(row.get("order_count") or 0)
        last_seen_r = row.get("last_seen")

        # Need at least 2 orders and a known last_seen
        if order_count < 2 or not last_seen_r:
            continue

        last_seen = _parse_last_seen(last_seen_r)
        if not last_seen:
            continue

        days_elapsed = _days_since(last_seen)

        # Get their frequent items
        try:
            mem     = crud.get_user_memory(phone, business_id)
            freq    = mem.get("frequent_items", {}) or {}
            # Sort by frequency descending
            items   = sorted(freq.items(), key=lambda x: x[1], reverse=True)
            top_items = [name for name, _ in items[:3]]
        except Exception:
            top_items = []

        if not top_items:
            continue

        # Compute shortest reorder interval among top items
        interval = min(_guess_interval(name) for name in top_items)

        days_overdue = days_elapsed - interval

        # Only surface customers within ±5 days of their expected reorder window
        if days_overdue < -5:
            continue  # Too early — not yet approaching window

        urgency = (
            "overdue"   if days_overdue > 2 else
            "due_soon"  if days_overdue >= -1 else
            "upcoming"
        )

        msg = generate_reorder_message(row, biz_name, top_items, days_overdue)

        results.append({
            "phone":             phone,
            "customer_name":     row.get("customer_name", ""),
            "order_count":       order_count,
            "total_spent":       float(row.get("total_spent") or 0),
            "last_seen":         last_seen_r,
            "days_since_order":  round(days_elapsed, 1),
            "expected_interval": interval,
            "days_overdue":      round(days_overdue, 1),
            "likely_products":   top_items,
            "urgency":           urgency,
            "suggested_message": msg,
        })

    # Sort: most overdue first
    results.sort(key=lambda r: r["days_overdue"], reverse=True)
    return results


def get_reorder_candidates(
    business_id: int,
    urgency_filter: str | None = None,   # "overdue" | "due_soon" | "upcoming" | None
    limit: int = 20,
) -> list[dict]:
    """
    Filtered view of predict_reorders().
    urgency_filter=None → all urgency levels.
    """
    candidates = predict_reorders(business_id)
    if urgency_filter:
        candidates = [c for c in candidates if c["urgency"] == urgency_filter]
    return candidates[:limit]


def generate_reorder_message(
    memory: dict,
    business_name: str,
    top_items: list[str] | None = None,
    days_overdue: float = 0,
) -> str:
    """
    Generate a personalised reorder nudge message for one customer.
    Safe to call even if top_items is empty.
    """
    name     = (memory.get("customer_name") or "").strip() or "there"
    greeting = f"Hey *{name}*!" if name != "there" else "Hey there!"

    if top_items:
        items_str = ", ".join(f"*{i}*" for i in top_items[:2])
        product_line = f"Looks like you might be running low on {items_str}."
    else:
        product_line = "It's been a while since your last order."

    if days_overdue > 5:
        urgency_line = "We noticed it's been a bit — we've got you covered! 😊"
    elif days_overdue > 0:
        urgency_line = "Your usual items are ready and waiting! 🛒"
    else:
        urgency_line = "Just a heads-up — your next order window is coming up."

    return (
        f"{greeting} {product_line}\n\n"
        f"{urgency_line}\n\n"
        f"Type *menu* to browse and order from *{business_name}* today! 🙏"
    )


def get_churn_risk_customers(
    business_id: int,
    risk_days: int = 21,
) -> list[dict]:
    """
    Return VIP and Loyal customers who haven't ordered in `risk_days` days.
    These are high-value customers worth targeting with win-back campaigns.

    Each result:
      {
        phone, customer_name, order_count, total_spent, last_seen,
        segment: "vip"|"loyal",
        days_inactive: float,
        risk_level: "high"|"medium",
        revenue_at_risk: float,   # estimated monthly value
      }
    """
    import crud

    try:
        vip   = crud.get_customers_by_segment(business_id, "vip")
        loyal = crud.get_customers_by_segment(business_id, "loyal")
        at_risk_rows = vip + loyal
    except Exception as exc:
        _msg = str(exc)
        if "temporarily unavailable" in _msg or "Errno 11" in _msg or "EAGAIN" in _msg:
            log.debug("get_churn_risk_customers: transient db error (pool busy): %s", exc)
        else:
            log.warning("get_churn_risk_customers: db error: %s", exc)
        return []

    results = []
    seen_phones: set[str] = set()

    for row in at_risk_rows:
        phone = row.get("phone", "")
        if not phone or phone in seen_phones:
            continue

        last_seen = _parse_last_seen(row.get("last_seen"))
        if not last_seen:
            continue

        days_inactive = _days_since(last_seen)
        if days_inactive < risk_days:
            continue

        seen_phones.add(phone)

        order_count = int(row.get("order_count") or 0)
        total_spent = float(row.get("total_spent") or 0)
        segment     = "vip" if order_count >= 10 or total_spent >= 50 else "loyal"

        # Estimate monthly revenue this customer represents
        months_active = max(1, days_inactive / 30)
        monthly_value = round(total_spent / months_active, 2)

        results.append({
            "phone":           phone,
            "customer_name":   row.get("customer_name", ""),
            "order_count":     order_count,
            "total_spent":     total_spent,
            "last_seen":       row.get("last_seen", ""),
            "segment":         segment,
            "days_inactive":   round(days_inactive, 1),
            "risk_level":      "high" if days_inactive > 45 else "medium",
            "revenue_at_risk": monthly_value,
        })

    # Most inactive first
    results.sort(key=lambda r: r["days_inactive"], reverse=True)
    return results


def get_win_back_suggestions(business_id: int) -> list[dict]:
    """
    Return ready-to-fire campaign suggestions for inactive customers.
    Each suggestion can be passed directly to CampaignService.run().

    Returns a list of:
      {
        title: str,           # human label for dashboard
        audience: str,        # CampaignService audience key
        count: int,           # how many customers this would reach
        message: str,         # suggested message template
        potential_revenue: float,
        urgency: "high"|"medium"|"low",
      }
    """
    import crud

    suggestions = []

    try:
        # Overdue reorders
        reorder_candidates = get_reorder_candidates(business_id, urgency_filter="overdue", limit=100)
        if reorder_candidates:
            pot_rev = sum(r.get("total_spent", 0) * 0.3 for r in reorder_candidates)
            suggestions.append({
                "title":             "Reorder Nudge",
                "audience":          "inactive_7d",
                "count":             len(reorder_candidates),
                "message":           "Hey {name}! 🛒 It looks like you might be running low. Type *menu* to reorder from {business} today!",
                "potential_revenue": round(pot_rev, 2),
                "urgency":           "high",
            })

        # Inactive VIP/loyal — churn risk
        churn = get_churn_risk_customers(business_id, risk_days=21)
        if churn:
            pot_rev = sum(r.get("revenue_at_risk", 0) for r in churn)
            suggestions.append({
                "title":             "Win-Back VIP & Loyal",
                "audience":          "inactive_30d",
                "count":             len(churn),
                "message":           "Hey {name}! 😊 We miss you! You've ordered {orders} times with us — come back and enjoy our latest items. Type *menu* to browse {business}. 🙏",
                "potential_revenue": round(pot_rev, 2),
                "urgency":           "high",
            })

        # Inactive regulars (14 days)
        inactive_14 = crud.get_inactive_customers(business_id, inactive_days=14, min_order_count=2)
        if inactive_14:
            suggestions.append({
                "title":             "Re-engage Regulars",
                "audience":          "inactive_14d",
                "count":             len(inactive_14),
                "message":           "Hi {name}! 👋 It's been a little while. We'd love to see you back at {business}. Type *menu* to see what's new today!",
                "potential_revenue": 0.0,
                "urgency":           "medium",
            })

        # New customers who haven't ordered in 7 days (convert to repeat buyers)
        new_inactive = crud.get_inactive_customers(business_id, inactive_days=7, min_order_count=1)
        new_only = [r for r in new_inactive if int(r.get("order_count") or 0) == 1]
        if new_only:
            suggestions.append({
                "title":             "Convert New Customers",
                "audience":          "new",
                "count":             len(new_only),
                "message":           "Hey {name}! 🎉 Loved your first order? Come back and try something new at {business}! Type *menu* to browse today.",
                "potential_revenue": 0.0,
                "urgency":           "low",
            })

    except Exception as exc:
        log.warning("get_win_back_suggestions error: %s", exc)

    return suggestions


def get_retention_summary(business_id: int) -> dict:
    """
    Dashboard card data — all key retention metrics in one call.
    Safe fallback: returns zeros on any error.
    """
    try:
        reorder_due    = get_reorder_candidates(business_id, urgency_filter="overdue", limit=100)
        reorder_soon   = get_reorder_candidates(business_id, urgency_filter="due_soon", limit=100)
        churn          = get_churn_risk_customers(business_id, risk_days=21)
        win_back       = get_win_back_suggestions(business_id)

        potential_reorder_revenue = sum(
            float(r.get("total_spent") or 0) * 0.25 for r in reorder_due + reorder_soon
        )
        revenue_at_risk = sum(
            float(r.get("revenue_at_risk") or 0) for r in churn
        )

        return {
            "reorder_overdue_count":    len(reorder_due),
            "reorder_soon_count":       len(reorder_soon),
            "churn_risk_count":         len(churn),
            "churn_risk_vip_count":     sum(1 for r in churn if r["segment"] == "vip"),
            "potential_reorder_revenue": round(potential_reorder_revenue, 2),
            "revenue_at_risk":          round(revenue_at_risk, 2),
            "win_back_suggestions":     win_back,
            "top_reorder_candidates":   reorder_due[:5],
            "top_churn_risks":          churn[:5],
        }
    except Exception as exc:
        log.error("get_retention_summary error: %s", exc)
        return {
            "reorder_overdue_count": 0, "reorder_soon_count": 0,
            "churn_risk_count": 0, "churn_risk_vip_count": 0,
            "potential_reorder_revenue": 0.0, "revenue_at_risk": 0.0,
            "win_back_suggestions": [], "top_reorder_candidates": [],
            "top_churn_risks": [],
        }
