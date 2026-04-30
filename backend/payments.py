# payments.py
"""
Payment confirmation logic.

confirm_payment() — validates a payment reference + amount and marks the order as paid.
Returns a result dict with success/error info and the updated order.
"""

import logging
from db import supabase
from order_lifecycle import confirm_payment_supabase, get_order

log = logging.getLogger(__name__)


def confirm_payment(reference: str, amount: float) -> dict:
    """
    Confirm a payment by reference code.

    reference format: ORDER-{order_id}  e.g. ORDER-12

    Steps:
      1. Parse order_id from reference
      2. Fetch order from Supabase
      3. Validate amount matches order total_price
      4. Mark order.status = "paid", order.payment_status = "paid"
      5. Return result dict

    Returns dict with keys:
      - success: bool
      - error: str (only on failure)
      - message: str (only on success)
      - order_id: int
      - order: dict (the updated order)
    """
    if not reference or not isinstance(reference, str):
        return {"success": False, "error": "Reference is required."}

    reference = reference.strip().upper()

    if not reference.startswith("ORDER-"):
        return {
            "success": False,
            "error": f"Invalid reference format '{reference}'. Expected: ORDER-{{id}}",
        }

    try:
        order_id = int(reference.split("-")[1])
    except (IndexError, ValueError):
        return {
            "success": False,
            "error": f"Could not parse order ID from reference '{reference}'.",
        }

    order = get_order(order_id)
    if not order:
        return {
            "success": False,
            "error": f"Order #{order_id} not found.",
            "order_id": order_id,
        }

    # Check if already paid
    if order.get("payment_status") == "paid":
        return {
            "success": False,
            "error": f"Order #{order_id} has already been paid.",
            "order_id": order_id,
            "order": order,
        }

    # Validate amount
    order_total = float(order.get("total_price") or order.get("total") or 0)
    paid_amount = float(amount)

    if round(paid_amount, 2) != round(order_total, 2):
        return {
            "success": False,
            "error": (
                f"Amount mismatch for ORDER-{order_id}: "
                f"expected ${order_total:.2f}, received ${paid_amount:.2f}."
            ),
            "order_id": order_id,
        }

    try:
        updated_order = confirm_payment_supabase(order_id, reference)
    except ValueError as exc:
        return {"success": False, "error": str(exc), "order_id": order_id}
    except Exception as exc:
        log.exception("confirm_payment: unexpected error for order %s: %s", order_id, exc)
        return {"success": False, "error": "Internal error confirming payment.", "order_id": order_id}

    log.info("💳 Payment confirmed  order=%s  amount=%.2f  ref=%s", order_id, paid_amount, reference)

    return {
        "success":  True,
        "message":  f"Payment of ${paid_amount:.2f} confirmed for ORDER-{order_id}. ✅",
        "order_id": order_id,
        "order":    updated_order,
    }
