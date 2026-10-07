"""
services/weekly_report_service.py
══════════════════════════════════
Feature 1 — Performance Summary Emails (now MONTHLY; filename/function names
kept as "weekly_*" so existing imports, main.py and the admin route keep working).

PLACEMENT: backend/services/weekly_report_service.py

Sends each business owner one summary email on the 1st of every month covering
the previous calendar month, unless they opted out (Profile → Preferences →
reports toggle, or the unsubscribe link in the email):
  • Total orders
  • Revenue
  • New customers
  • Repeat customers count
  • Top product by order volume

SAFETY:
  • All DB errors are caught — the loop continues if one business fails.
  • Email failures are logged but never crash the system.
  • Relies on existing email_service._send and analytics patterns.
  • No new database tables required.

SCHEDULING:
  Intended to be called from main.py startup via a simple background thread.
  See attach_weekly_report_scheduler() below.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

log = logging.getLogger("wazibot.weekly_report")

_BASE_URL = os.getenv("WAZIBOT_URL", "https://wazibot-api-assistant.onrender.com")


# ─────────────────────────────────────────────────────────────────────────────
# Data gathering
# ─────────────────────────────────────────────────────────────────────────────

def _month_window(now: Optional[datetime] = None):
    """(start, end, label) of the previous calendar month, in UTC."""
    now = now or datetime.now(timezone.utc)
    end = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    start = (end - timedelta(days=1)).replace(day=1)
    return start, end, start.strftime("%B %Y")


def _next_monthly_run(now: Optional[datetime] = None) -> datetime:
    """Next 1st-of-month 08:00 UTC strictly after `now`."""
    now = now or datetime.now(timezone.utc)
    cand = now.replace(day=1, hour=8, minute=0, second=0, microsecond=0)
    if cand <= now:
        cand = (cand + timedelta(days=32)).replace(day=1)
    return cand


def _get_weekly_stats(business_id: int, since: Optional[datetime] = None,
                      until: Optional[datetime] = None) -> dict:
    """
    Pull stats for one business for [since, until) (default: last 30 days)
    using existing Supabase tables. Returns safe defaults on any error.
    NOTE: result keys keep their historical "_7d" names for compatibility;
    they hold the figures for the requested window.
    """
    default = {
        "orders_7d": 0, "revenue_7d": 0.0,
        "new_customers_7d": 0, "repeat_customers": 0,
        "top_product": None,
    }
    try:
        from core.db import supabase

        until_dt = until or datetime.now(timezone.utc)
        since_dt = since or (until_dt - timedelta(days=30))
        since = since_dt.isoformat()
        until_iso = until_dt.isoformat()

        # Orders last 7 days
        orders_res = (
            supabase.table("orders")
            .select("id, total_price, payment_status, items, customer_phone, created_at")
            .eq("business_id", business_id)
            .gte("created_at", since)
            .lt("created_at", until_iso)
            .execute()
        )
        orders = orders_res.data or []
        total_orders  = len(orders)
        total_revenue = sum(
            float(o.get("total_price") or 0)
            for o in orders
            if o.get("payment_status") in ("paid", "confirmed")
        )

        # New customers last 7 days (first seen in this window)
        new_cust_res = (
            supabase.table("user_memory")
            .select("phone")
            .eq("business_id", business_id)
            .gte("created_at", since)
            .lt("created_at", until_iso)
            .execute()
        )
        new_customers = len(new_cust_res.data or [])

        # Repeat customers (order_count > 1 in user_memory)
        repeat_res = (
            supabase.table("user_memory")
            .select("phone, order_count")
            .eq("business_id", business_id)
            .gt("order_count", 1)
            .execute()
        )
        repeat_customers = len(repeat_res.data or [])

        # Top product by name frequency in order items
        product_counts: dict[str, int] = {}
        for order in orders:
            items = order.get("items") or []
            if isinstance(items, list):
                for item in items:
                    name = item.get("name") or item.get("product_name") or ""
                    if name:
                        product_counts[name] = product_counts.get(name, 0) + 1
        top_product = max(product_counts, key=product_counts.get) if product_counts else None

        return {
            "orders_7d":        total_orders,
            "revenue_7d":       round(total_revenue, 2),
            "new_customers_7d": new_customers,
            "repeat_customers": repeat_customers,
            "top_product":      top_product,
        }
    except Exception as exc:
        log.warning("weekly_report: stats fetch failed for biz %s: %s", business_id, exc)
        return default


def _build_report_html(business_name: str, stats: dict, dash_url: str,
                       period_label: str = "", unsubscribe_link: str = "") -> str:
    """Build the monthly report HTML body."""
    period_label = period_label or _month_window()[2]
    top_product_line = (
        f"<p>⭐ <strong style='color:#e8f5e9'>Top product:</strong> "
        f"{stats['top_product']}</p>"
        if stats.get("top_product") else ""
    )
    repeat_pct = ""
    if stats.get("repeat_customers") and stats.get("repeat_customers", 0) > 0:
        repeat_pct = f" (customers who ordered more than once)"

    return f"""
<h1>Your month at {business_name} 📊</h1>
<p style='color:#6b8f71;font-family:monospace;font-size:12px;'>
  {period_label}
</p>

<div style='background:#172010;border:1px solid #1f3025;border-radius:10px;
            padding:24px;margin:20px 0;display:grid;grid-template-columns:1fr 1fr;gap:16px;'>
  <div style='text-align:center;'>
    <div style='font-size:32px;font-weight:800;color:#22c55e;'>{stats['orders_7d']}</div>
    <div style='font-family:monospace;font-size:11px;color:#6b8f71;text-transform:uppercase;letter-spacing:1px;'>Orders</div>
  </div>
  <div style='text-align:center;'>
    <div style='font-size:32px;font-weight:800;color:#22c55e;'>${stats['revenue_7d']:.2f}</div>
    <div style='font-family:monospace;font-size:11px;color:#6b8f71;text-transform:uppercase;letter-spacing:1px;'>Revenue</div>
  </div>
  <div style='text-align:center;'>
    <div style='font-size:32px;font-weight:800;color:#e8f5e9;'>{stats['new_customers_7d']}</div>
    <div style='font-family:monospace;font-size:11px;color:#6b8f71;text-transform:uppercase;letter-spacing:1px;'>New Customers</div>
  </div>
  <div style='text-align:center;'>
    <div style='font-size:32px;font-weight:800;color:#e8f5e9;'>{stats['repeat_customers']}</div>
    <div style='font-family:monospace;font-size:11px;color:#6b8f71;text-transform:uppercase;letter-spacing:1px;'>Repeat Customers{repeat_pct}</div>
  </div>
</div>

{top_product_line}

<p style='margin-top:20px;'>
  <a href='{dash_url}' class='btn'>View Full Dashboard →</a>
</p>

{'<p style="font-family:monospace;font-size:12px;color:#6b8f71;">No orders last month — share your store link to get more customers: <a href="{_BASE_URL}/directory" style="color:#22c55e">{_BASE_URL}/directory</a></p>' if stats["orders_7d"] == 0 else ""}

<p style='font-family:monospace;font-size:11px;color:#6b8f71;margin-top:24px;'>
  You get this summary once a month.
  {f'<a href="{unsubscribe_link}" style="color:#22c55e">Stop these reports</a> or' if unsubscribe_link else ''}
  you can switch it off any time in Dashboard → Profile → Preferences.
</p>
"""


# ─────────────────────────────────────────────────────────────────────────────
# Main send function
# ─────────────────────────────────────────────────────────────────────────────

def send_weekly_reports() -> dict:
    """
    Send the monthly report email to all active businesses that have an email
    and have not opted out (name kept for backward compatibility).

    Returns a summary dict: {sent, skipped, errors}
    Called by the scheduler on the 1st of each month. Each business is sent at
    most once per month (features_json.last_monthly_report guards restarts /
    multiple instances).
    """
    sent = skipped = errors = 0

    try:
        from core.db import supabase
        from services.email_service import _send, _base_template, report_unsubscribe_url

        # Fetch all active businesses with an email address
        res = (
            supabase.table("businesses")
            .select("id, name, owner_email, owner_username, features_json")
            .eq("is_active", True)
            .execute()
        )
        businesses = res.data or []
        log.info("weekly_report: processing %d businesses", len(businesses))

        for biz in businesses:
            biz_id    = biz.get("id")
            biz_name  = biz.get("name") or "Your Business"
            email     = (biz.get("owner_email") or "").strip()

            fj        = biz.get("features_json") or {}
            if not isinstance(fj, dict):
                fj = {}

            if not email or "@" not in email:
                skipped += 1
                continue

            # Opt-out: the Preferences toggle and the email's unsubscribe link
            # both store pref_weekly_reports=False. Absent = opted in (default).
            if fj.get("pref_weekly_reports") is False:
                skipped += 1
                continue

            start, end, period = _month_window()
            period_key = start.strftime("%Y-%m")
            if fj.get("last_monthly_report") == period_key:
                skipped += 1          # already sent for this month
                continue

            try:
                stats    = _get_weekly_stats(biz_id, start, end)
                dash_url = f"{_BASE_URL}/dashboard"
                body     = _build_report_html(biz_name, stats, dash_url, period,
                                              report_unsubscribe_url(biz_id))
                html     = _base_template(f"Monthly Report — {biz_name}", body)

                ok = _send(
                    to      = email,
                    subject = f"{biz_name} — Monthly Report ({period}) 📊",
                    html    = html,
                )
                if ok:
                    sent += 1
                    try:
                        supabase.table("businesses").update(
                            {"features_json": {**fj, "last_monthly_report": period_key}}
                        ).eq("id", biz_id).execute()
                    except Exception as _exc:
                        log.warning("weekly_report: could not record send for biz=%s: %s", biz_id, _exc)
                    log.info("weekly_report: sent to biz=%s email=%s", biz_id, email)
                else:
                    errors += 1
            except Exception as exc:
                errors += 1
                log.warning("weekly_report: failed for biz=%s: %s", biz_id, exc)

    except Exception as exc:
        log.error("weekly_report: top-level error: %s", exc)

    result = {"sent": sent, "skipped": skipped, "errors": errors}
    log.info("weekly_report: complete %s", result)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Scheduler attachment — called from main.py startup
# ─────────────────────────────────────────────────────────────────────────────

def attach_weekly_report_scheduler(app) -> None:
    """
    Attach a simple background thread to FastAPI that fires the report
    on the 1st of every month at 08:00 UTC.

    Uses only stdlib threading + time — no new dependencies.
    Called once from main.py with attach_weekly_report_scheduler(app).
    Non-blocking — runs in daemon thread, never affects request handling.
    """
    import threading
    import time as _time

    def _loop():
        log.info("report_scheduler: background thread started (monthly)")
        while True:
            try:
                now = datetime.now(timezone.utc)
                next_run = _next_monthly_run(now)
                wait_seconds = (next_run - now).total_seconds()
                log.info(
                    "report_scheduler: next run in %.1f hours at %s",
                    wait_seconds / 3600, next_run.strftime("%Y-%m-%d %H:%M UTC"),
                )
                # sleep in <=1 day chunks so very long waits survive clock drift
                _time.sleep(min(max(wait_seconds, 60), 86400))
                if datetime.now(timezone.utc) >= next_run:
                    send_weekly_reports()
            except Exception as exc:
                log.warning("report_scheduler: loop error: %s", exc)
                _time.sleep(3600)   # wait an hour and retry

    t = threading.Thread(target=_loop, daemon=True, name="weekly-report-scheduler")
    t.start()
    log.info("weekly_report_scheduler: daemon thread launched")


# Clearer aliases for the monthly cadence (old names stay valid).
send_monthly_reports = send_weekly_reports
attach_monthly_report_scheduler = attach_weekly_report_scheduler
