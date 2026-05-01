# invoice.py
"""
Invoice generation.

generate_invoice()       — builds a WhatsApp-friendly text invoice from an order dict or ORM object.
generate_invoice_text()  — alias for the AI/webhook flow (accepts dict).
mark_order_paid()        — SQLAlchemy helper to flip order status.

PAYMENT DETAILS:
  The invoice includes the business's registered payment number (from Supabase) and a
  verified business profile line so customers know who they're paying and feel safe.

  The business row is looked up via crud.get_business_by_id(business_id).
  Businesses should set their payment_number and payment_name in their settings.
  Falls back to generic EcoCash instructions if not set.
"""

import json
import logging
import os
from datetime import datetime

log = logging.getLogger(__name__)

# Fallback payment details (set these in .env or Render environment variables
# as a global default, overridden per-business from the DB when available)
DEFAULT_PAYMENT_NUMBER = os.getenv("DEFAULT_PAYMENT_NUMBER", "")
DEFAULT_PAYMENT_NAME   = os.getenv("DEFAULT_PAYMENT_NAME", "")


def _get_business_payment(business_id) -> dict:
    """
    Fetch the business payment details from the DB.
    Returns a dict with: payment_number, payment_name, business_name.
    Never raises — returns empty strings on any failure.
    """
    if not business_id:
        return {}
    try:
        import crud
        biz = crud.get_business_by_id(int(business_id))
        if not biz:
            return {}
        return {
            "payment_number": biz.get("payment_number") or DEFAULT_PAYMENT_NUMBER,
            "payment_name":   biz.get("payment_name")   or DEFAULT_PAYMENT_NAME,
            "business_name":  biz.get("name", ""),
        }
    except Exception as exc:
        log.warning("_get_business_payment failed: %s", exc)
        return {}


def generate_invoice(order, business_id=None) -> str:
    """
    Build a WhatsApp-friendly invoice string.

    Accepts either:
      • A plain dict  (from Supabase / order_lifecycle_supabase)
      • An SQLAlchemy Order ORM instance

    The invoice includes:
      - Order items with line totals
      - Payment instructions with business phone number + registered name
      - A trust/verification line so customers know exactly who to pay
    """
    # ── Normalise to dict-like access ────────────────────────────────────────
    if isinstance(order, dict):
        order_id       = order.get("id", "N/A")
        items_raw      = order.get("items", "")
        total          = order.get("total_price", 0)
        status         = order.get("status", "pending")
        payment_status = order.get("payment_status", "pending")
        customer_phone = order.get("customer_phone", "")
        biz_id         = business_id or order.get("business_id")
        biz_name_order = order.get("business_name", "")
    else:
        order_id       = getattr(order, "id", "N/A")
        items_raw      = getattr(order, "items", "") or ""
        total          = getattr(order, "total_price", None) or getattr(order, "total", 0)
        status         = getattr(order, "status", "pending")
        payment_status = getattr(order, "payment_status", "pending")
        customer_phone = getattr(order, "customer_phone", "")
        biz_id         = business_id or getattr(order, "business_id", None)
        biz_name_order = ""

    # ── Parse items ───────────────────────────────────────────────────────────
    items_text = ""
    if items_raw:
        try:
            items = json.loads(items_raw) if isinstance(items_raw, str) else items_raw
            lines = []
            for item in items:
                name     = item.get("name", "?")
                qty      = item.get("qty", item.get("quantity", 1))
                price    = float(item.get("price", 0))
                subtotal = float(item.get("subtotal", price * qty))
                lines.append(f"  • {name} ×{qty}  —  ${subtotal:.2f}")
            items_text = "\n".join(lines)
        except Exception:
            items_text = str(items_raw)
    else:
        items_text = "  (no item detail)"

    # ── Payment status ────────────────────────────────────────────────────────
    pay_icon  = "✅" if payment_status == "paid" else "⏳"
    pay_label = "PAID" if payment_status == "paid" else "PENDING"

    # ── Business payment details ──────────────────────────────────────────────
    biz_info = _get_business_payment(biz_id)
    biz_name     = biz_info.get("business_name") or biz_name_order or "WaziBot Business"
    pay_number   = biz_info.get("payment_number") or DEFAULT_PAYMENT_NUMBER
    pay_name     = biz_info.get("payment_name")   or DEFAULT_PAYMENT_NAME

    # Build payment section based on available info
    if pay_number:
        # Full verified payment block
        pay_section = (
            f"💳 *How to Pay:*\n"
            f"{'─' * 28}\n"
            f"  Method     : EcoCash / Mobile Money\n"
            f"  Pay to     : *{pay_number}*\n"
            f"  Name       : *{pay_name or biz_name}*\n"
            f"  Reference  : *ORDER-{order_id}*\n"
            f"  Amount     : *${float(total):.2f}*\n"
            f"{'─' * 28}\n"
            f"🔒 *Verified Business:*\n"
            f"  {biz_name}\n"
            f"  This payment request was generated\n"
            f"  by {biz_name}'s official ordering system.\n"
            f"  Always verify the number above\n"
            f"  before sending money.\n"
        )
    else:
        # Minimal fallback — no number configured yet
        pay_section = (
            f"💳 *How to Pay:*\n"
            f"{'─' * 28}\n"
            f"  Method     : EcoCash / Mobile Money\n"
            f"  Reference  : *ORDER-{order_id}*\n"
            f"  Amount     : *${float(total):.2f}*\n"
            f"{'─' * 28}\n"
            f"  ℹ Contact *{biz_name}* directly\n"
            f"  for payment number details.\n"
        )

    # ── Full invoice ──────────────────────────────────────────────────────────
    invoice = (
        f"🧾 *INVOICE — {biz_name}*\n"
        f"{'─' * 28}\n"
        f"Order ID : *#{order_id}*\n"
        f"Date     : {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        f"{'─' * 28}\n"
        f"Items:\n{items_text}\n"
        f"{'─' * 28}\n"
        f"💰 *Total: ${float(total):.2f}*\n"
        f"Status   : {status.upper()}\n"
        f"{pay_icon} Payment : {pay_label}\n"
        f"{'─' * 28}\n"
        f"{pay_section}"
        f"{'─' * 28}\n"
        f"Thank you for your order! 🙏\n"
        f"_Reply *menu* to keep shopping._"
    )

    log.info("🧾 Invoice generated  order_id=%s  total=%.2f  biz=%s", order_id, float(total), biz_name)
    return invoice


# Alias for clarity in the AI flow
generate_invoice_text = generate_invoice


def mark_order_paid(db, order):
    """SQLAlchemy helper — flip order to paid."""
    order.status = "paid"
    order.payment_status = "paid"
    db.commit()
    log.info("💳 Order %s marked paid", order.id)
    return order
