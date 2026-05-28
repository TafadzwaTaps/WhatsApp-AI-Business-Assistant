"""
services/campaign_service.py — Campaign & Broadcast Engine (Phase 4)

PURPOSE
───────
Adds audience targeting on top of the existing broadcast system.
Instead of "send to all", you can now send to:
  • inactive customers (win-back)
  • VIP / loyal customers (retention)
  • new customers (onboarding)
  • high spenders (upsell)
  • customers with unpaid orders (payment nudge)

Uses ONLY the existing send_whatsapp() sender and crud helpers.
Does NOT modify WhatsApp core logic.

USAGE
─────
From main.py endpoints:

    from services.campaign_service import CampaignService

    result = await CampaignService.run(
        business_id = 3,
        audience    = "inactive_30d",
        message     = "Hey! We miss you at Flavoury Foods. Come back for 10% off today!",
        dry_run     = False,
    )

AUDIENCES
─────────
  "all"              — every customer with any interaction
  "vip"              — VIP segment (≥10 orders or ≥$50 spent)
  "loyal"            — Loyal segment (≥5 orders or ≥$20 spent)
  "regular"          — Regular segment (2-4 orders)
  "new"              — New customers (exactly 1 order)
  "inactive_30d"     — Not seen in 30 days, ordered at least once
  "inactive_14d"     — Not seen in 14 days
  "inactive_7d"      — Not seen in 7 days
  "high_spenders"    — total_spent ≥ $30
  "unpaid"           — customers with stale payment orders
  "custom"           — pass phone_list explicitly
"""

from __future__ import annotations

import logging
import time
from typing import Optional

log = logging.getLogger(__name__)

# Minimum gap between two campaigns sent to the same phone (minutes)
# Prevents accidental double-sending when endpoint is called twice quickly.
CAMPAIGN_COOLDOWN_MINUTES = 30

# In-process dedup: {(business_id, phone): last_sent_unix}
_campaign_sent: dict[tuple, float] = {}


def _on_cooldown(business_id: int, phone: str) -> bool:
    key  = (business_id, phone)
    last = _campaign_sent.get(key)
    if last is None:
        return False
    return (time.time() - last) < (CAMPAIGN_COOLDOWN_MINUTES * 60)


def _mark_sent(business_id: int, phone: str) -> None:
    _campaign_sent[(business_id, phone)] = time.time()


# ─────────────────────────────────────────────────────────────────────────────
# AUDIENCE RESOLVER
# ─────────────────────────────────────────────────────────────────────────────

def resolve_audience(
    business_id: int,
    audience:    str,
    phone_list:  list[str] | None = None,
) -> list[dict]:
    """
    Return list of {phone, customer_name, ...} for the given audience.
    Each dict is a user_memory row.

    For "custom" audience, pass phone_list explicitly.
    """
    import crud

    if audience == "custom":
        if not phone_list:
            return []
        # Fetch memory rows for given phones
        result = []
        for phone in phone_list:
            mem = crud.get_user_memory(phone, business_id) or {}
            mem["phone"] = phone
            result.append(mem)
        return result

    if audience == "all":
        return crud.get_customers_by_segment(business_id, "all")

    if audience in ("vip", "loyal", "regular", "new", "prospect"):
        return crud.get_customers_by_segment(business_id, audience)

    if audience.startswith("inactive_"):
        # e.g. "inactive_30d" → 30 days
        try:
            days = int(audience.replace("inactive_", "").replace("d", ""))
        except ValueError:
            days = 30
        return crud.get_inactive_customers(business_id, inactive_days=days)

    if audience == "high_spenders":
        rows = crud.get_customers_by_segment(business_id, "all")
        return [r for r in rows if float(r.get("total_spent") or 0) >= 30]

    if audience == "unpaid":
        orders = crud.get_stale_payment_orders(business_id, older_than_hours=0.5)
        seen   = set()
        result = []
        for o in orders:
            phone = o.get("customer_phone", "")
            if phone and phone not in seen:
                seen.add(phone)
                mem = crud.get_user_memory(phone, business_id) or {}
                mem["phone"] = phone
                result.append(mem)
        return result

    log.warning("campaign: unknown audience=%r", audience)
    return []


# ─────────────────────────────────────────────────────────────────────────────
# MESSAGE PERSONALISER
# ─────────────────────────────────────────────────────────────────────────────

def personalise(message: str, memory: dict, business_name: str = "") -> str:
    """
    Replace template variables in a campaign message.

    Supported variables:
      {name}     — customer_name or "there"
      {business} — business name
      {orders}   — order_count
      {spent}    — total_spent formatted

    Example:
      "Hi {name}, thank you for your {orders} orders at {business}!"
      → "Hi Tafadzwa, thank you for your 7 orders at Flavoury Foods!"
    """
    name   = (memory.get("customer_name") or "").strip() or "there"
    orders = int(memory.get("order_count") or 0)
    spent  = float(memory.get("total_spent") or 0)

    return (
        message
        .replace("{name}",     name)
        .replace("{business}", business_name)
        .replace("{orders}",   str(orders))
        .replace("{spent}",    f"${spent:.2f}")
    )


# ─────────────────────────────────────────────────────────────────────────────
# CAMPAIGN RUNNER
# ─────────────────────────────────────────────────────────────────────────────

class CampaignService:
    """
    Executes targeted WhatsApp campaigns against a resolved audience.
    Uses the existing send_whatsapp() from main.py — no new HTTP logic.
    """

    @staticmethod
    def run(
        business_id:  int,
        audience:     str,
        message:      str,
        *,
        phone_list:   list[str] | None = None,
        personalise_msg: bool = True,
        dry_run:      bool = False,
    ) -> dict:
        """
        Send a campaign message to a resolved audience.

        Parameters
        ──────────
        business_id      Tenant
        audience         Audience key (see module docstring)
        message          Message text — supports {name}, {business}, {orders}, {spent}
        phone_list       Explicit phones for "custom" audience
        personalise_msg  If True, replaces {name} etc. in the message per recipient
        dry_run          If True, resolve audience and preview but do NOT send

        Returns
        ───────
        {
          audience:   str,
          total:      int,   # audience size
          sent:       int,
          skipped:    int,   # cooldown / no phone
          failed:     int,
          dry_run:    bool,
          previews:   list   # first 3 personalised messages (always shown)
        }
        """
        import crud

        business = crud.get_business_by_id(business_id)
        if not business:
            return {"ok": False, "error": f"Business {business_id} not found"}

        biz_name  = business.get("name", "")
        recipients = resolve_audience(business_id, audience, phone_list)

        if not recipients:
            return {
                "ok": True, "audience": audience, "total": 0,
                "sent": 0, "skipped": 0, "failed": 0,
                "dry_run": dry_run,
                "message": f"No customers found for audience '{audience}'",
            }

        # Build previews (first 3 personalised messages)
        previews = []
        for r in recipients[:3]:
            phone = r.get("phone", "")
            msg   = personalise(message, r, biz_name) if personalise_msg else message
            previews.append({"phone": phone, "message": msg})

        if dry_run:
            log.info(
                "campaign DRY RUN  biz=%s  audience=%s  recipients=%d",
                business_id, audience, len(recipients),
            )
            return {
                "ok": True, "audience": audience,
                "total": len(recipients),
                "sent": 0, "skipped": 0, "failed": 0,
                "dry_run": True, "previews": previews,
            }

        # Resolve WhatsApp credentials
        try:
            token = crud.get_decrypted_token(business)
        except Exception:
            token = ""

        phone_id = business.get("whatsapp_phone_id", "")

        # Fall back to shared number
        if not phone_id or not token:
            import os
            phone_id = os.getenv("SHARED_PHONE_NUMBER_ID", "").strip()
            token    = os.getenv("SHARED_WA_TOKEN", "").strip()

        if not phone_id or not token:
            return {"ok": False, "error": "No WhatsApp credentials configured"}

        # Send
        sent    = 0
        skipped = 0
        failed  = 0

        try:
            from main import send_whatsapp as _send
        except ImportError:
            return {"ok": False, "error": "Cannot import send_whatsapp from main"}

        for recipient in recipients:
            phone = (recipient.get("phone") or "").strip()
            if not phone:
                skipped += 1
                continue

            if _on_cooldown(business_id, phone):
                log.debug("campaign: cooldown  phone=%s", phone)
                skipped += 1
                continue

            msg = personalise(message, recipient, biz_name) if personalise_msg else message

            try:
                result = _send(phone_id, token, phone, msg)
                if "error" in result:
                    raise RuntimeError(result["error"])
                _mark_sent(business_id, phone)
                sent += 1
            except Exception as exc:
                log.warning("campaign: send failed  phone=%s  exc=%s", phone, exc)
                failed += 1

        log.info(
            "campaign done  biz=%s  audience=%s  total=%d  sent=%d  skipped=%d  failed=%d",
            business_id, audience, len(recipients), sent, skipped, failed,
        )
        return {
            "ok":       True,
            "audience": audience,
            "total":    len(recipients),
            "sent":     sent,
            "skipped":  skipped,
            "failed":   failed,
            "dry_run":  False,
            "previews": previews,
        }


# ─────────────────────────────────────────────────────────────────────────────
# AUDIENCE DESCRIPTIONS (for dashboard display)
# ─────────────────────────────────────────────────────────────────────────────

AUDIENCE_INFO: dict[str, dict] = {
    "all":           {"label": "All Customers",           "icon": "📱", "desc": "Every customer who has ever messaged"},
    "vip":           {"label": "VIP Customers",           "icon": "⭐", "desc": "10+ orders or $50+ spent"},
    "loyal":         {"label": "Loyal Customers",         "icon": "💚", "desc": "5+ orders or $20+ spent"},
    "regular":       {"label": "Regular Customers",       "icon": "👍", "desc": "2–4 orders placed"},
    "new":           {"label": "New Customers",           "icon": "👋", "desc": "Exactly 1 order placed"},
    "inactive_7d":   {"label": "Inactive (7 days)",       "icon": "😴", "desc": "No activity in last 7 days"},
    "inactive_14d":  {"label": "Inactive (14 days)",      "icon": "😴", "desc": "No activity in last 14 days"},
    "inactive_30d":  {"label": "Inactive (30 days)",      "icon": "🔴", "desc": "No activity in last 30 days"},
    "high_spenders": {"label": "High Spenders",           "icon": "💰", "desc": "Customers with $30+ total spend"},
    "unpaid":        {"label": "Unpaid Orders",           "icon": "⏳", "desc": "Customers with pending payments"},
    "custom":        {"label": "Custom Selection",        "icon": "✏️",  "desc": "Manually selected phones"},
}
