# invoice.py
"""
Invoice generation.

generate_invoice()       — builds a WhatsApp-friendly text invoice from an order dict or ORM object.
generate_invoice_text()  — alias for the AI/webhook flow (accepts dict).
mark_order_paid()        — SQLAlchemy helper to flip order status.
"""

import json
import logging
from datetime import datetime

log = logging.getLogger(__name__)


def generate_invoice(order) -> str:
    """
    Build a WhatsApp-friendly invoice string.

    Accepts either:
      • An SQLAlchemy Order ORM instance  (has .id, .items, .total_price, .status, .customer_phone)
      • A plain dict with the same keys   (from Supabase / order_lifecycle_supabase)
    """
    # Normalise to dict-like access
    if isinstance(order, dict):
        order_id       = order.get("id", "N/A")
        items_raw      = order.get("items", "")
        total          = order.get("total_price", 0)
        status         = order.get("status", "pending")
        customer_phone = order.get("customer_phone", "")
    else:
        order_id       = getattr(order, "id", "N/A")
        items_raw      = getattr(order, "items", "") or ""
        total          = getattr(order, "total_price", None) or getattr(order, "total", 0)
        status         = getattr(order, "status", "pending")
        customer_phone = getattr(order, "customer_phone", "")

    # Parse items JSON if present
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
                lines.append(f"  • {name} x{qty}  — ${subtotal:.2f}")
            items_text = "\n".join(lines)
        except Exception:
            items_text = str(items_raw)
    else:
        items_text = "  (no item detail)"

    invoice = (
        f"🧾 *INVOICE*\n"
        f"{'─' * 28}\n"
        f"Order ID : #{order_id}\n"
        f"Date     : {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        f"{'─' * 28}\n"
        f"Items:\n{items_text}\n"
        f"{'─' * 28}\n"
        f"💰 *Total: ${float(total):.2f}*\n"
        f"Status   : {status.upper()}\n"
        f"{'─' * 28}\n"
        f"💳 Payment: EcoCash / Mobile Money\n"
        f"Reference: ORDER-{order_id}\n"
        f"{'─' * 28}\n"
        f"Thank you for your order! 🙏"
    )

    log.info("🧾 Invoice generated  order_id=%s  total=%.2f", order_id, float(total))
    return invoice


# Alias for clarity in the AI flow
generate_invoice_text = generate_invoice


def mark_order_paid(db, order):
    """SQLAlchemy helper — flip order to paid."""
    order.status = "paid"
    db.commit()
    log.info("💳 Order %s marked paid", order.id)
    return order
