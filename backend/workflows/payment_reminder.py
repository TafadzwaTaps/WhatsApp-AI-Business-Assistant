"""
workflows/payment_reminder.py — Payment Reminder System (Phase 6)

PURPOSE
───────
Sends WhatsApp nudges to customers whose orders have been stuck in
awaiting_payment or payment_review for longer than a configurable threshold.

TRIGGER PATHS
─────────────
1. HTTP endpoint  POST /payments/reminders/send
   Business owner triggers manually from the dashboard
   (or a Render cron job hits it automatically).

2. Dashboard auto-check  GET /payments/reminders/pending
   Dashboard polls this on load to show the badge count of stale orders.

3. Individual nudge  POST /payments/reminders/{order_id}/nudge
   Re-send reminder for one specific order.

REMINDER BEHAVIOUR
──────────────────
• First reminder  after FIRST_REMINDER_HOURS  (default 1 h)
• Second reminder after SECOND_REMINDER_HOURS (default 3 h)
• Final reminder  after FINAL_REMINDER_HOURS  (default 6 h)
  — final message includes a "reply CANCEL to cancel your order" option

Each reminder is personalised to the payment method:
  EcoCash  → shows the EcoCash number and dial code
  PayPal   → shows the PayPal email / payment link
  Cash     → reminds them to confirm pickup/delivery intent

SAFETY
──────
• Idempotent — calling send multiple times in the same window
  won't re-send to customers who already got a reminder in the
  last COOLDOWN_MINUTES minutes.
• Stored in-process (dict). A future upgrade can use the
  payment_reminders table (added by MIGRATION section 20).
• Never touches the order status — read-only on orders.
• All errors logged, never raised to caller.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional
from services.order_money import order_amount_text as _order_amount

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────

FIRST_REMINDER_HOURS  = 1.0    # send first nudge after 1 hour
SECOND_REMINDER_HOURS = 3.0    # send second nudge after 3 hours
FINAL_REMINDER_HOURS  = 6.0    # send final warning after 6 hours

COOLDOWN_MINUTES      = 55     # minimum gap between reminders to the same order
                                # (prevents duplicate sends if endpoint called twice)

# ─────────────────────────────────────────────────────────────────────────────
# IN-PROCESS COOLDOWN TRACKER
# ─────────────────────────────────────────────────────────────────────────────
# key: order_id (int), value: unix timestamp of last reminder sent
_last_reminder_sent: dict[int, float] = {}


def _is_on_cooldown(order_id: int) -> bool:
    last = _last_reminder_sent.get(order_id)
    if last is None:
        return False
    return (time.time() - last) < (COOLDOWN_MINUTES * 60)


def _mark_reminded(order_id: int) -> None:
    _last_reminder_sent[order_id] = time.time()


# ─────────────────────────────────────────────────────────────────────────────
# REMINDER TIER DETECTION
# ─────────────────────────────────────────────────────────────────────────────

def _hours_since(created_at_str: str) -> float:
    """Return hours elapsed since created_at. Returns 0.0 on parse error."""
    try:
        raw = created_at_str.replace("Z", "+00:00")
        created = datetime.fromisoformat(raw)
        delta   = datetime.now(timezone.utc) - created
        return delta.total_seconds() / 3600
    except (ValueError, TypeError, AttributeError):
        return 0.0


def get_reminder_tier(order: dict) -> Optional[int]:
    """
    Return which reminder tier applies to this order:
      1 → first reminder   (≥ FIRST_REMINDER_HOURS)
      2 → second reminder  (≥ SECOND_REMINDER_HOURS)
      3 → final warning    (≥ FINAL_REMINDER_HOURS)
      None → not yet due

    Always returns the HIGHEST applicable tier so the message
    is appropriate for how overdue the order is.
    """
    hours = _hours_since(order.get("created_at", ""))

    if hours >= FINAL_REMINDER_HOURS:
        return 3
    if hours >= SECOND_REMINDER_HOURS:
        return 2
    if hours >= FIRST_REMINDER_HOURS:
        return 1
    return None


# ─────────────────────────────────────────────────────────────────────────────
# MESSAGE BUILDERS
# ─────────────────────────────────────────────────────────────────────────────

def build_reminder_message(
    order:         dict,
    business_name: str,
    tier:          int,
) -> str:
    """
    Build a WhatsApp reminder message appropriate for the payment method and tier.

    Parameters
    ──────────
    order          Full order dict from the DB
    business_name  Business name for personalisation
    tier           1 (gentle nudge) | 2 (firmer) | 3 (final warning)

    Returns a non-empty WhatsApp-formatted string.
    """
    method    = (order.get("payment_method") or "cash").lower()
    reference = order.get("payment_reference") or f"ORDER-{order.get('id', '?')}"
    total     = float(order.get("total_price") or 0)

    # ── Tier opener ──────────────────────────────────────────────────────────
    if tier == 1:
        opener = (
            f"👋 Hi! Just a friendly reminder about your order at *{business_name}*."
        )
    elif tier == 2:
        opener = (
            f"⏰ Your order at *{business_name}* is still waiting for payment."
        )
    else:  # tier 3
        opener = (
            f"⚠️ *Final reminder* — your order at *{business_name}* will be "
            f"cancelled soon if payment is not received."
        )

    # ── Order summary ────────────────────────────────────────────────────────
    order_block = (
        f"\n\n📦 Order   : *{reference}*"
        f"\n💰 Total   : *{_order_amount(order, total)}*"
    )

    # ── Payment-method-specific instructions ─────────────────────────────────
    if method == "ecocash":
        pay_instructions = _ecocash_reminder(order)
    elif method in ("paypal", "paypal_email"):
        pay_instructions = _paypal_reminder(order)
    else:
        pay_instructions = _cash_reminder(order)

    # ── Call to action ───────────────────────────────────────────────────────
    if tier < 3:
        cta = (
            f"\nOnce paid, reply *paid* and send your transaction ID or screenshot. 🙏"
        )
    else:
        cta = (
            f"\nPlease pay now to keep your order, or reply *cancel* to cancel it."
            f"\n\n_This is our last reminder for *{reference}*._"
        )

    return opener + order_block + pay_instructions + cta


def _ecocash_reminder(order: dict) -> str:
    """EcoCash-specific payment details block."""
    try:
        import crud
        pay = crud.get_business_payment_settings(order.get("business_id"))
        eco_number = pay.get("ecocash_number", "")
        eco_name   = pay.get("ecocash_name",   "")
        if eco_number:
            return (
                f"\n\n💚 *Pay via EcoCash:*"
                f"\n  Dial: *\\*151\\#*"
                f"\n  Send to: *{eco_number}*"
                + (f"\n  Name: _{eco_name}_" if eco_name else "")
            )
    except Exception as _swallowed_exc:
        from core.safelog import swallow as _swallow; _swallow(__name__, _swallowed_exc)
    return "\n\n💚 Please complete your *EcoCash* payment."


def _paypal_reminder(order: dict) -> str:
    """PayPal-specific payment details block."""
    # Check order row first — payment_url is stored on the order after creation
    pay_url = order.get("payment_url", "")
    if pay_url:
        return (
            f"\n\n🌍 *Pay via PayPal:*"
            f"\n  👉 {pay_url}"
        )
    # Fall back to business payment settings
    try:
        import crud
        pay = crud.get_business_payment_settings(order.get("business_id"))
        paypal_email = pay.get("paypal_email", "")
        if paypal_email:
            return (
                f"\n\n🌍 *Pay via PayPal:*"
                f"\n  Send to: *{paypal_email}*"
                f"\n  Amount: *{_order_amount(order)}*"
            )
    except Exception as _swallowed_exc:
        from core.safelog import swallow as _swallow; _swallow(__name__, _swallowed_exc)
    return "\n\n🌍 Please complete your *PayPal* payment."


def _cash_reminder(order: dict) -> str:
    """Cash / pending_cash reminder block."""
    fulfillment = (order.get("fulfillment_method") or "").lower()
    if fulfillment == "delivery":
        return "\n\n💵 *Cash on delivery* — please have the exact amount ready."
    if fulfillment == "pickup":
        return "\n\n💵 *Cash on pickup* — please collect and pay at our location."
    return "\n\n💵 *Cash payment* — please confirm your collection or delivery preference."


# ─────────────────────────────────────────────────────────────────────────────
# SEND HELPER
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_wa_credentials(business: dict) -> tuple[str, str]:
    """
    Return (phone_number_id, token) for a business.
    Falls back to shared platform number if business has no own credentials.
    """
    import os, crud
    phone_id = business.get("whatsapp_phone_id", "")
    token    = ""
    try:
        token = crud.get_decrypted_token(business)
    except Exception as _swallowed_exc:
        from core.safelog import swallow as _swallow; _swallow(__name__, _swallowed_exc)

    if not phone_id or not token:
        shared_pid   = os.getenv("SHARED_PHONE_NUMBER_ID", "").strip()
        shared_token = os.getenv("SHARED_WA_TOKEN", "").strip()
        if shared_pid and shared_token:
            return shared_pid, shared_token

    return phone_id, token


def send_reminder(
    order:         dict,
    business:      dict,
    tier:          int,
    dry_run:       bool = False,
    force:         bool = False,
) -> dict:
    """
    Send a single payment reminder WhatsApp message.

    Parameters
    ──────────
    order      Full order row from DB
    business   Full business row from DB
    tier       Reminder tier (1 | 2 | 3)
    dry_run    If True, build the message but do not send (for preview/testing)
    force      Owner-initiated re-send: ignores an existing once-per-tier claim (the claim is still
               recorded, so the bulk run will not repeat the tier afterwards)

    Returns dict with keys:
      ok          bool
      order_id    int
      phone       str
      tier        int
      message     str   (the message that was/would be sent)
      error       str   (present only on failure)
    """
    order_id    = order.get("id")
    phone       = order.get("customer_phone", "")
    biz_name    = business.get("name", "WaziBot")

    if not phone:
        return {"ok": False, "order_id": order_id, "tier": tier,
                "error": "no customer_phone on order"}

    if _is_on_cooldown(order_id) and not force:
        log.debug("reminder: on cooldown  order=%s", order_id)
        return {"ok": False, "order_id": order_id, "tier": tier, "phone": phone,
                "error": "cooldown — reminder sent recently"}

    message = build_reminder_message(order, biz_name, tier)

    if dry_run:
        log.info("reminder DRY RUN  order=%s  tier=%d  phone=%s", order_id, tier, phone)
        return {"ok": True, "order_id": order_id, "tier": tier,
                "phone": phone, "message": message, "dry_run": True}

    phone_id, token = _resolve_wa_credentials(business)
    if not phone_id or not token:
        return {"ok": False, "order_id": order_id, "tier": tier, "phone": phone,
                "error": "no WhatsApp credentials configured for this business"}

    # Persistent once-per-tier guard (survives restarts and deploys, unlike the in-process cooldown above):
    # each order gets each reminder tier at most once. Claimed BEFORE sending, released when nothing was sent.
    # If the ledger table is missing the claim is not recorded and the in-process cooldown still applies.
    biz_id = order.get("business_id") or business.get("id")
    claim_key = f"{order_id}:t{tier}"
    try:
        from crud import notification_ledger as _ledger
        won = _ledger.claim(biz_id, "payment_reminder", claim_key, table_only=True)
        if not won and not force:
            return {"ok": False, "order_id": order_id, "tier": tier, "phone": phone,
                    "error": "cooldown — this reminder tier was already sent for this order"}
    except Exception as exc:                       # fail closed: never risk a duplicate customer message
        log.warning("reminder claim failed order=%s: %s", order_id, type(exc).__name__)
        return {"ok": False, "order_id": order_id, "tier": tier, "phone": phone,
                "error": "cooldown — could not verify reminder history, try again shortly"}
    result = _deliver_reminder(order, business, tier, message, phone, phone_id, token, biz_name, order_id)
    if not result.get("ok") and won:
        try:
            _ledger.release(biz_id, "payment_reminder", claim_key)      # nothing went out: allow a retry
        except Exception as exc:
            log.warning("reminder release failed order=%s: %s", order_id, type(exc).__name__)
    return result


def _deliver_reminder(order, business, tier, message, phone, phone_id, token, biz_name, order_id) -> dict:
    # 24-hour window: a customer who last wrote more than a day ago cannot be sent free text.
    try:
        from services.wa_window import closed_phones
        if phone in closed_phones(business.get("id"), [phone]):
            # Window closed: only an approved UTILITY template may be sent (a payment reminder is transactional).
            from services.wa_outbound import template_for_closed_window
            _amount = order.get("total_price") or order.get("total") or ""
            tr = template_for_closed_window(
                business, business.get("id"), phone, "payment_reminder",
                {"business_name": biz_name, "order_id": f"ORDER-{order_id}", "amount": _order_amount(order, _amount or None)},
                ref=f"order:{order_id}:tier{tier}")
            if tr.get("ok"):
                _mark_reminded(order_id)
                return {"ok": True, "order_id": order_id, "tier": tier, "phone": phone, "message": message,
                        "sent_as_template": True}
            return {"ok": False, "order_id": order_id, "tier": tier, "phone": phone, "skipped_window": True,
                    "error": "outside WhatsApp's 24-hour window: an approved message template is required",
                    "message": message}
    except Exception as exc:
        log.debug("reminder window check skipped: %s", type(exc).__name__)

    try:
        # Import send_whatsapp from main without creating a circular import.
        # We call it through a lazy import at call time.
        from main import send_whatsapp
        result = send_whatsapp(phone_id, token, phone, message)

        if "error" in result:
            log.error(
                "reminder send failed  order=%s  phone=%s  error=%s",
                order_id, phone, result["error"],
            )
            return {"ok": False, "order_id": order_id, "tier": tier,
                    "phone": phone, "error": result["error"], "message": message}

        _mark_reminded(order_id)
        log.info(
            "reminder sent  order=%s  tier=%d  phone=%s  biz=%s",
            order_id, tier, phone, business.get("id"),
        )
        return {"ok": True, "order_id": order_id, "tier": tier,
                "phone": phone, "message": message}

    except Exception as exc:
        log.error("reminder exception  order=%s  exc=%s", order_id, exc)
        return {"ok": False, "order_id": order_id, "tier": tier,
                "phone": phone, "error": str(exc), "message": message}


# ─────────────────────────────────────────────────────────────────────────────
# BULK REMINDER RUN
# ─────────────────────────────────────────────────────────────────────────────

def run_reminders_for_business(
    business_id: int,
    dry_run:     bool = False,
) -> dict:
    """
    Find all stale payment orders for a business and send appropriate reminders.

    Called by:
      POST /payments/reminders/send   (business owner triggers from dashboard)

    Returns a summary dict with counts and per-order results.
    """
    import crud

    business = crud.get_business_by_id(business_id)
    if not business:
        return {"ok": False, "error": f"Business {business_id} not found"}

    # Fetch orders stale for at least FIRST_REMINDER_HOURS
    stale = crud.get_stale_payment_orders(
        business_id,
        older_than_hours=FIRST_REMINDER_HOURS,
    )

    results   = []
    sent      = 0
    skipped   = 0
    failed    = 0

    for order in stale:
        tier = get_reminder_tier(order)
        if tier is None:
            skipped += 1
            continue

        result = send_reminder(order, business, tier, dry_run=dry_run)
        results.append(result)

        if result["ok"]:
            sent += 1
        elif "cooldown" in result.get("error", ""):
            skipped += 1
        else:
            failed += 1

    log.info(
        "run_reminders_for_business  biz=%s  stale=%d  sent=%d  skipped=%d  failed=%d  dry=%s",
        business_id, len(stale), sent, skipped, failed, dry_run,
    )

    return {
        "ok":          True,
        "business_id": business_id,
        "stale_count": len(stale),
        "sent":        sent,
        "skipped":     skipped,
        "failed":      failed,
        "dry_run":     dry_run,
        "results":     results,
    }
