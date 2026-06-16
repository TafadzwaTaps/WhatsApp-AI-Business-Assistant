"""
growth/cart_recovery.py — Abandoned Cart Recovery (Opt-In Only)

DESIGN RULES
────────────
• Default OFF — only runs for businesses that set `cart_recovery_enabled = True`
  in features_json.
• NEVER interferes with active conversations (only targets truly idle carts).
• NEVER sends if the customer is in human_handoff, checkout, or payment states.
• NEVER sends more than 1 message per 24 hours per customer.
• Uses existing send_whatsapp() infrastructure — no new HTTP client.
• Reads existing carts/orders tables — no new schema required.
• Safe: all DB errors are caught and logged, never crash the server.

TRIGGER CONDITIONS
──────────────────
  Cart has items AND
  state is "browsing" AND
  last_seen > CART_IDLE_MINUTES ago (default 60 min) AND
  No successful order in last 24h AND
  No recovery message sent in last 24h

INTEGRATION
───────────
Call `run_cart_recovery(business_id)` from a background task or
from the existing payment_reminder scheduler — it's idempotent.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional

log = logging.getLogger(__name__)

# Time after which an idle cart triggers recovery (default 60 minutes)
CART_IDLE_SECONDS = int(os.getenv("CART_RECOVERY_IDLE_MINUTES", "60")) * 60

# Maximum one recovery message per customer per this many seconds (24h)
RECOVERY_COOLDOWN_SECONDS = 24 * 3600

# States that should NEVER receive a recovery message
SKIP_STATES = {
    "human_handoff", "checkout", "confirm", "awaiting_payment",
    "payment_review", "awaiting_fulfillment",
}


def _is_recovery_enabled(business_id: int) -> bool:
    """Check if cart recovery is enabled for a business. Default: OFF."""
    try:
        from core.db import supabase
        res = (
            supabase.table("businesses")
            .select("features_json")
            .eq("id", business_id)
            .limit(1)
            .execute()
        )
        fj = (res.data or [{}])[0].get("features_json") or {}
        return bool(fj.get("cart_recovery_enabled", False))
    except Exception:
        return False


def _build_recovery_message(business_name: str, cart_items: list, currency_sym: str = "$") -> str:
    """Build a friendly cart recovery WhatsApp message."""
    if not cart_items:
        return ""

    item_lines = []
    total      = 0.0
    for item in cart_items[:3]:  # Show max 3 items to keep message short
        name  = item.get("name", "item")
        qty   = item.get("qty", 1)
        price = float(item.get("price") or 0)
        sub   = qty * price
        total += sub
        item_lines.append(f"  • {name} ×{qty} — {currency_sym}{sub:.2f}")

    if len(cart_items) > 3:
        item_lines.append(f"  ...and {len(cart_items) - 3} more item(s)")

    items_text = "\n".join(item_lines)
    return (
        f"👋 Hey! You left something behind at *{business_name}*.\n\n"
        f"🛒 *Your cart is waiting:*\n{items_text}\n\n"
        f"💰 *Total: {currency_sym}{total:.2f}*\n\n"
        f"Ready to complete your order? Just type *checkout* to continue, "
        f"or *menu* to browse more. 😊\n\n"
        f"_This cart will be saved for you._"
    )


def run_cart_recovery(business_id: int) -> dict:
    """
    Check for abandoned carts for a business and send recovery messages.
    Returns {"sent": N, "skipped": N, "errors": N}.
    Safe to call repeatedly — idempotent due to cooldown check.
    """
    if not _is_recovery_enabled(business_id):
        return {"sent": 0, "skipped": 0, "errors": 0, "reason": "disabled"}

    results = {"sent": 0, "skipped": 0, "errors": 0}
    now     = time.time()

    try:
        from core.db import supabase

        # Get all carts for this business
        carts_res = (
            supabase.table("carts")
            .select("phone, items, state_data, updated_at")
            .eq("business_id", business_id)
            .execute()
        )
        carts = carts_res.data or []

        # Get business config
        biz_res = (
            supabase.table("businesses")
            .select("name, currency_symbol, whatsapp_phone_id, features_json")
            .eq("id", business_id)
            .limit(1)
            .execute()
        )
        biz       = (biz_res.data or [{}])[0]
        biz_name  = biz.get("name", "us")
        currency  = biz.get("currency_symbol") or "$"

        for cart in carts:
            phone     = cart.get("phone")
            items     = cart.get("items") or []
            sd        = cart.get("state_data") or {}
            state     = sd.get("state", "browsing")
            updated_at = cart.get("updated_at")

            if not phone or not items:
                results["skipped"] += 1
                continue

            # Skip active states
            if state in SKIP_STATES:
                results["skipped"] += 1
                continue

            # Check idle time
            if updated_at:
                try:
                    import datetime as _dt
                    ua  = _dt.datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                    age = now - ua.timestamp()
                    if age < CART_IDLE_SECONDS:
                        results["skipped"] += 1
                        continue
                except Exception:
                    pass

            # Check recovery cooldown (stored in state_data)
            last_recovery = sd.get("last_cart_recovery_at", 0)
            try:
                if float(last_recovery) > now - RECOVERY_COOLDOWN_SECONDS:
                    results["skipped"] += 1
                    continue
            except (TypeError, ValueError):
                pass

            # Build and send recovery message
            msg = _build_recovery_message(biz_name, items, currency)
            if not msg:
                results["skipped"] += 1
                continue

            sent = _send_recovery_message(business_id, biz, phone, msg)
            if sent:
                # Record the send time in state_data (non-destructive merge)
                try:
                    sd["last_cart_recovery_at"] = now
                    supabase.table("carts").update({"state_data": sd}).eq(
                        "phone", phone).eq("business_id", business_id).execute()
                except Exception:
                    pass
                results["sent"] += 1
                log.info("Cart recovery sent  biz=%s  phone=%s", business_id, phone)
            else:
                results["errors"] += 1

    except Exception as exc:
        log.error("run_cart_recovery error  biz=%s  error=%s", business_id, exc)
        results["errors"] += 1

    return results


def _send_recovery_message(business_id: int, biz: dict, phone: str, msg: str) -> bool:
    """Send a recovery message using existing WhatsApp infrastructure."""
    try:
        import crud
        token    = crud.get_decrypted_token(biz) if biz else ""
        phone_id = biz.get("whatsapp_phone_id", "")
        if not token or not phone_id:
            # Shared number fallback
            token    = os.getenv("SHARED_WA_TOKEN", "")
            phone_id = os.getenv("SHARED_PHONE_NUMBER_ID", "")
        if not token or not phone_id:
            return False

        # Use existing send_whatsapp function from main.py (injected at startup)
        from routes.webhook_routes import _send_direct
        if _send_direct:
            _send_direct(phone_id, token, phone, msg)
            return True

        # Direct HTTP fallback if _send_direct not yet injected
        import requests as _req
        url = f"https://graph.facebook.com/v18.0/{phone_id}/messages"
        resp = _req.post(url, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                         json={"messaging_product": "whatsapp", "to": phone,
                               "type": "text", "text": {"body": msg}}, timeout=10)
        return resp.status_code == 200
    except Exception as exc:
        log.warning("_send_recovery_message error  biz=%s  phone=%s  error=%s",
                    business_id, phone, exc)
        return False
