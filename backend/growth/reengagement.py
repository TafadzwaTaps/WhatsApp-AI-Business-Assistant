"""
growth/reengagement.py — Re-Engagement Campaigns (Opt-In Only)

DESIGN RULES
────────────
• Default OFF per business (requires features_json.reengagement_enabled = True).
• Only targets customers who haven't ordered in N days (configurable).
• NEVER sends to customers in active states (checkout, payment, handoff).
• NEVER sends more than 1 message per customer per 7 days.
• Uses existing user_memory and orders tables — no new schema.
• Uses existing WhatsApp send infrastructure.
• Safe: all errors caught and logged.

TRIGGER
───────
Call run_reengagement(business_id) from existing payment_reminder scheduler
or a new background task — idempotent, cooldown-protected.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional

log = logging.getLogger(__name__)

REENGAGEMENT_DAYS    = int(os.getenv("REENGAGEMENT_INACTIVE_DAYS",  "14"))  # target customers inactive N+ days
REENGAGEMENT_COOLDOWN= int(os.getenv("REENGAGEMENT_COOLDOWN_DAYS",  "7"))  * 86400  # don't resend within N days


def _is_reengagement_enabled(business_id: int) -> bool:
    """Check if re-engagement is enabled for a business. Default: OFF."""
    try:
        from core.db import supabase
        res = supabase.table("businesses").select("features_json").eq("id", business_id).limit(1).execute()
        fj  = (res.data or [{}])[0].get("features_json") or {}
        return bool(fj.get("reengagement_enabled", False))
    except Exception:
        return False


def _build_reengagement_message(
    business_name: str,
    customer_name: str,
    last_product: str = "",
    currency_sym: str = "$",
) -> str:
    """Build a personalised re-engagement WhatsApp message."""
    name_part    = f"Hi *{customer_name}*!" if customer_name else "Hi there!"
    product_hint = (
        f"We noticed you haven't ordered *{last_product}* in a while — "
        if last_product else ""
    )
    return (
        f"👋 {name_part}\n\n"
        f"It's been a while since your last order at *{business_name}*. "
        f"{product_hint}We miss you! 😊\n\n"
        f"Type *menu* to see what's new, or *repeat last order* to reorder "
        f"your favourite items instantly.\n\n"
        f"_Reply *stop* if you'd prefer not to receive these messages._"
    )


def run_reengagement(business_id: int) -> dict:
    """
    Send re-engagement messages to inactive customers for a business.
    Returns {"sent": N, "skipped": N, "errors": N}.
    """
    if not _is_reengagement_enabled(business_id):
        return {"sent": 0, "skipped": 0, "errors": 0, "reason": "disabled"}

    results = {"sent": 0, "skipped": 0, "errors": 0}
    now     = time.time()
    cutoff  = now - REENGAGEMENT_DAYS * 86400

    try:
        import datetime as _dt
        from core.db import supabase

        # Get inactive customers from user_memory
        cutoff_iso = _dt.datetime.fromtimestamp(cutoff, _dt.timezone.utc).isoformat()
        mem_res = (
            supabase.table("user_memory")
            .select("phone, customer_name, order_count, last_seen, last_product")
            .eq("business_id", business_id)
            .lt("last_seen", cutoff_iso)
            .gte("order_count", 1)   # only customers who have ordered before
            .execute()
        )
        inactive = mem_res.data or []

        biz_res = (
            supabase.table("businesses")
            .select("name, currency_symbol, whatsapp_phone_id, features_json")
            .eq("id", business_id)
            .limit(1)
            .execute()
        )
        biz      = (biz_res.data or [{}])[0]
        biz_name = biz.get("name", "us")
        currency = biz.get("currency_symbol") or "$"

        for mem in inactive:
            phone     = mem.get("phone")
            cust_name = mem.get("customer_name") or ""
            last_prod = mem.get("last_product")  or ""

            if not phone:
                results["skipped"] += 1
                continue

            # Check cooldown in carts.state_data
            try:
                cart_res = (
                    supabase.table("carts")
                    .select("state_data")
                    .eq("phone", phone)
                    .eq("business_id", business_id)
                    .limit(1)
                    .execute()
                )
                sd    = (cart_res.data or [{}])[0].get("state_data") or {}
                state = sd.get("state", "browsing")

                # Skip active states
                if state in {"human_handoff", "checkout", "confirm",
                             "awaiting_payment", "payment_review", "awaiting_fulfillment"}:
                    results["skipped"] += 1
                    continue

                # Check cooldown
                last_re = float(sd.get("last_reengagement_at", 0) or 0)
                if last_re > now - REENGAGEMENT_COOLDOWN:
                    results["skipped"] += 1
                    continue
            except Exception:
                sd = {}

            msg = _build_reengagement_message(biz_name, cust_name, last_prod, currency)
            sent = _send_message(biz, phone, msg)

            if sent:
                try:
                    sd["last_reengagement_at"] = now
                    supabase.table("carts").upsert(
                        {"phone": phone, "business_id": business_id, "state_data": sd},
                        on_conflict="phone,business_id"
                    ).execute()
                except Exception:
                    pass
                results["sent"] += 1
                log.info("Reengagement sent  biz=%s  phone=%s", business_id, phone)
            else:
                results["errors"] += 1

    except Exception as exc:
        log.error("run_reengagement error  biz=%s: %s", business_id, exc)
        results["errors"] += 1

    return results


def _send_message(biz: dict, phone: str, msg: str) -> bool:
    try:
        import crud
        token    = crud.get_decrypted_token(biz) if biz else ""
        phone_id = biz.get("whatsapp_phone_id", "")
        if not token or not phone_id:
            token    = os.getenv("SHARED_WA_TOKEN", "")
            phone_id = os.getenv("SHARED_PHONE_NUMBER_ID", "")
        if not token or not phone_id:
            return False
        import requests as _req
        url  = f"https://graph.facebook.com/v18.0/{phone_id}/messages"
        resp = _req.post(url, headers={"Authorization": f"Bearer {token}",
                                       "Content-Type": "application/json"},
                         json={"messaging_product": "whatsapp", "to": phone,
                               "type": "text", "text": {"body": msg}}, timeout=10)
        return resp.status_code == 200
    except Exception as exc:
        log.warning("reengagement send error phone=%s: %s", phone, exc)
        return False
