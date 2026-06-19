"""
services/_ai_payments.py — Payment processing, order status display, PayPal
confirmation handling, payment instructions, and PDF invoice dispatch.

Imported by ai.py. Do not import ai.py from here (circular import).
"""

import logging

import crud

log = logging.getLogger(__name__)


# ── Payment status labels ─────────────────────────────────────────────────────

def _friendly_payment_status(status: str) -> str:
    labels = {
        "pending":            "Pending",
        "pending_cash":       "Confirmed (Cash)",
        "awaiting_payment":   "Awaiting Payment",
        "awaiting_proof":     "Awaiting Proof",
        "payment_review":     "Under Review",
        "paid":               "Paid ✅",
        "confirmed":          "Confirmed",
        "cancelled":          "Cancelled",
        "refunded":           "Refunded",
        "payment_error":      "Payment Error",
    }
    return labels.get(status, status.replace("_", " ").title())


# ── Payment instructions ──────────────────────────────────────────────────────

def _build_payment_instructions(pending: dict, business_id: int, business_name: str) -> str:
    """Re-generate payment instructions from stored pending_payment session."""
    from services.payment_service import (
        generate_ecocash_instructions,
        paypal_payment,
        generate_cash_instructions,
    )

    method    = pending.get("method", "cash")
    reference = pending.get("reference", "")
    order_id  = pending.get("order_id")

    try:
        pay_settings = crud.get_business_payment_settings(business_id)
    except Exception:
        pay_settings = {}

    total = 0.0
    try:
        from workflows.order_lifecycle import get_order
        ord_row = get_order(order_id)
        if ord_row:
            total = float(ord_row.get("total_price") or 0)
    except Exception:
        pass

    order = {
        "id":            order_id,
        "total_price":   total,
        "business_name": business_name,
        **pay_settings,
    }

    try:
        if method == "ecocash":
            pay = generate_ecocash_instructions(order)
        elif method == "paypal":
            pay = paypal_payment(order)
        else:
            pay = generate_cash_instructions(order)
        return pay.get("message", f"Please complete payment for *{reference}*.")
    except Exception as exc:
        log.error("_build_payment_instructions error: %s", exc)
        return (
            f"💳 Please complete payment for *{reference}*.\n"
            "Contact us if you need the payment details again."
        )


# ── Order status message ──────────────────────────────────────────────────────

_LIFECYCLE_ICONS = {
    "pending":               ("🕐", "Order received — awaiting payment"),
    "awaiting_payment":      ("⏳", "Awaiting payment"),
    "payment_pending":       ("⏳", "Payment pending"),
    "awaiting_confirmation": ("🔍", "Payment under review by our team"),
    "confirmed":             ("✅", "Payment confirmed"),
    "paid":                  ("✅", "Payment confirmed"),
    "preparing":             ("👨‍🍳", "Your order is being prepared"),
    "ready":                 ("🎉", "Ready for pickup!"),
    "out_for_delivery":      ("🛵", "Out for delivery — on the way!"),
    "delivered":             ("📦", "Delivered — enjoy your meal!"),
    "completed":             ("🎉", "Order completed"),
    "cancelled":             ("❌", "Order cancelled"),
}


def _order_status_message(order_id: int, phone: str, business_id: int) -> str:
    """Look up an order and return a rich formatted status message."""
    try:
        from workflows.order_lifecycle import get_order
        order = get_order(order_id)
        if not order:
            return (
                f"❓ I couldn't find *ORDER-{order_id}*.\n\n"
                "Please check the order number and try again, "
                "or type *help* for assistance."
            )

        if str(order.get("customer_phone", "")).replace("+", "") != str(phone).replace("+", ""):
            if order.get("business_id") != business_id:
                return f"❓ I couldn't find *ORDER-{order_id}* for your account."

        status         = order.get("status", "pending")
        payment_status = order.get("payment_status", "pending")
        total          = float(order.get("total_price") or 0)
        created        = (order.get("created_at") or "")[:16].replace("T", " ")

        effective_key = payment_status if payment_status in _LIFECYCLE_ICONS else status
        icon, label   = _LIFECYCLE_ICONS.get(
            effective_key,
            _LIFECYCLE_ICONS.get(status, ("📋", status.upper()))
        )

        pay_icon = "✅" if payment_status in ("paid", "confirmed") else "⏳"

        s_lower = status.lower()
        p_lower = payment_status.lower()

        if s_lower == "cancelled":
            progress = "❌ Cancelled"
        elif s_lower in ("delivered", "completed"):
            progress = "✅ ✅ ✅ ✅ ✅  Complete!"
        elif s_lower == "preparing":
            progress = "✅ ✅ ✅ ⬜ ⬜  Preparing"
        elif s_lower in ("paid", "confirmed") or p_lower == "paid":
            progress = "✅ ✅ ⬜ ⬜ ⬜  Preparing soon"
        elif p_lower in ("awaiting_confirmation", "awaiting_payment"):
            progress = "✅ ⏳ ⬜ ⬜ ⬜  Verifying payment"
        else:
            progress = "✅ ⬜ ⬜ ⬜ ⬜  Order received"

        agent_note = ""
        if p_lower == "awaiting_confirmation":
            agent_note = "\n🔍 _A team member is reviewing your payment proof._"
        elif p_lower in ("awaiting_payment", "pending") and s_lower == "pending":
            agent_note = "\n⏳ _Waiting for your payment._"

        return (
            f"📋 *Order Status*\n"
            f"{'─' * 26}\n"
            f"  Order   : *ORDER-{order_id}*\n"
            f"  Date    : {created}\n"
            f"  Total   : *${total:.2f}*\n"
            f"{'─' * 26}\n"
            f"{icon} {label}\n"
            f"{pay_icon} Payment : *{_friendly_payment_status(payment_status)}*\n"
            f"{'─' * 26}\n"
            f"📊 {progress}"
            f"{agent_note}\n"
            f"{'─' * 26}\n"
            f"_Type *menu* to place a new order._"
        )
    except Exception as exc:
        log.error("_order_status_message error: %s", exc)
        return f"❓ Could not load order *ORDER-{order_id}* right now. Please try again."


# ── PayPal paid handler ───────────────────────────────────────────────────────

def _handle_paypal_paid_message(
    phone: str,
    business_id: int,
    business_name: str,
    order_id,
    reference: str,
) -> str:
    """
    Called when a user says "paid" while awaiting a PayPal payment.
    Checks PayPal API, marks order if confirmed, falls back to proof flow.
    """
    from services.payment_service import get_paypal_order_details
    from services._ai_state import _read_state_data, _reset_state, _set_awaiting_proof

    state_data      = _read_state_data(phone, business_id)
    paypal_order_id = state_data.get("paypal_order_id", "")

    if not paypal_order_id:
        log.warning("_handle_paypal_paid_message: no paypal_order_id  phone=%s", phone)
        _set_awaiting_proof(phone, business_id,
                            order_id=order_id, method="paypal", reference=reference)
        return (
            f"✅ *Got it! Thank you for paying.*\n\n"
            f"To confirm your PayPal payment, please send your *transaction ID* "
            f"or a *screenshot* of the payment.\n\n"
            f"📦 Order: *{reference}*\n\n"
            f"_This helps us verify and process your order. 🙏_"
        )

    try:
        details = get_paypal_order_details(paypal_order_id)
    except Exception as exc:
        log.error("PayPal status check failed: %s", exc)
        details = {"paid": False, "error": str(exc)}

    if details.get("paid"):
        try:
            if order_id:
                crud.update_order_payment(order_id, business_id, {
                    "payment_status":    "paid",
                    "payment_reference": reference,
                })
        except Exception as exc:
            log.warning("PayPal payment status update failed: %s", exc)

        _reset_state(phone, business_id)
        amount = details.get("amount", 0)
        return (
            f"✅ *PayPal Payment Confirmed!*\n\n"
            f"Thank you! Your payment of *${amount:.2f} USD* has been verified.\n\n"
            f"📦 Order : *{reference}*\n"
            f"📍 Status: *CONFIRMED*\n\n"
            f"We're now preparing your order. You'll hear from us shortly! 🙌\n\n"
            f"_Thank you for choosing *{business_name}*!_"
        )

    return (
        f"⏳ *We're verifying your PayPal payment.*\n\n"
        f"📦 Order: *{reference}*\n\n"
        f"This usually only takes a few seconds. You'll receive an automatic "
        f"confirmation message as soon as your payment clears.\n\n"
        f"_No action needed — just wait for our message! 😊_\n"
        f"_Type *cancel* if you want to cancel this order._"
    )


# ── Process payment (checkout pipeline) ──────────────────────────────────────

def _process_payment(
    method: str,
    cart: list,
    phone: str,
    business_id: int,
    business_name: str,
) -> str:
    from workflows.order_lifecycle import create_order_supabase
    from services.payment_service import (
        generate_ecocash_instructions,
        paypal_payment,
        generate_cash_instructions,
    )
    from services._ai_state import (
        _set_awaiting_payment, _set_awaiting_fulfillment, _write_state_data,
        _get_session,
    )
    from services._ai_memory import _update_order_history

    # 1. Create order
    try:
        log.info("checkout  method=%s  phone=%s  items=%d", method, phone, len(cart))
        order = create_order_supabase(
            business_id=business_id,
            customer_phone=phone,
            cart=cart,
            payment_method=method,
        )
        order["business_name"] = business_name
        try:
            pay_settings = crud.get_business_payment_settings(business_id)
            order.update(pay_settings)
        except Exception as exc:
            log.warning("payment settings injection failed: %s", exc)
        log.info("order created  id=%s  method=%s", order.get("id", "?"), method)
    except ValueError as exc:
        log.warning("order blocked: %s", exc)
        exc_str = str(exc)

        # Fix: "Product 'X' not found in business id=N" was repeating forever
        # because the bad item stayed in the cart and the customer kept
        # retrying the same broken checkout. Self-heal by removing the
        # specific item that failed lookup, so the *next* checkout attempt
        # (with the remaining valid items) can actually succeed.
        import re as _re
        m = _re.search(r"Product '([^']+)' not found", exc_str)
        if m:
            bad_name = m.group(1).strip().lower()
            try:
                from services._ai_state import _write_state_data, _get_session
                remaining = [
                    item for item in cart
                    if (item.get("name") or "").strip().lower() != bad_name
                ]
                _write_state_data(phone, business_id, {"cart": remaining, "cart_snapshot": remaining})
            except Exception as cleanup_exc:
                log.warning("cart auto-cleanup failed: %s", cleanup_exc)
                remaining = []

            if remaining:
                items_left = "\n".join(
                    f"  • {i.get('name','?')} ×{i.get('qty',1)} — ${float(i.get('price',0))*int(i.get('qty',1)):.2f}"
                    for i in remaining
                )
                return (
                    f"⚠️ Sorry, *{m.group(1)}* is no longer available and has been "
                    f"removed from your cart.\n\n"
                    f"🛒 *Updated Cart:*\n{items_left}\n\n"
                    f"Type *checkout* to try again, or *menu* to add something else."
                )
            else:
                try:
                    from services._ai_state import _reset_state as _rs
                    _rs(phone, business_id)
                except Exception:
                    pass
                return (
                    f"⚠️ Sorry, *{m.group(1)}* is no longer available and was your "
                    f"only cart item — your cart is now empty.\n\n"
                    f"Type *menu* to see what's available. 😊"
                )

        return (
            f"⚠️ Couldn't place your order:\n_{exc_str}_\n\n"
            "Please adjust your cart and try *checkout* again."
        )
    except Exception as exc:
        log.exception("order creation error: %s", exc)

        # Fix: track consecutive checkout failures so the customer isn't
        # stuck retrying forever — escalate to a human after repeated errors.
        fail_count = 1
        try:
            session = _get_session(phone, business_id)
            fail_count = int(session.get("checkout_fail_count", 0)) + 1
            _write_state_data(phone, business_id, {
                "session": {**session, "checkout_fail_count": fail_count}
            })
        except Exception:
            pass

        if fail_count >= 2:
            # Use the same handoff pattern as the existing "talk to a human"
            # request flow in ai_new.py (P-2.5) — there is no standalone
            # trigger_handoff() helper; handoff is initiated by setting state
            # directly and notifying the dashboard.
            try:
                from services._ai_state import _set_human_handoff
                from services.whatsapp_catalog import generate_ticket_number
                from workflows import human_handoff as _hh

                _set_human_handoff(phone, business_id)
                customer = crud.get_or_create_customer(phone, business_id)
                cust_id  = customer.get("id") if customer else 0
                ticket   = generate_ticket_number(cust_id, business_id)
                _write_state_data(phone, business_id, {
                    "state": "human_handoff",
                    "session": {"ticket": ticket, "handoff_reason": "Repeated checkout failures"},
                })
                _hh.notify_dashboard(phone, business_id, business_name)
            except Exception as handoff_exc:
                log.warning("auto-escalation to human handoff failed: %s", handoff_exc)

            return (
                "❌ We're having trouble processing your order right now.\n\n"
                "I've notified the business owner to help you directly — "
                "they'll be with you shortly. 🙏\n\n"
                "Your cart is saved, so nothing is lost."
            )

        return (
            "❌ Something went wrong saving your order.\n\n"
            "Your cart is still saved — please try *checkout* again in a moment."
        )

    # 2. Call payment gateway
    try:
        if method == "ecocash":
            pay = generate_ecocash_instructions(order)
        elif method == "paypal":
            pay = paypal_payment(order)
        else:
            pay = generate_cash_instructions(order)
    except Exception as exc:
        log.exception("payment gateway error  method=%s: %s", method, exc)
        pay = {
            "message": (
                f"⚠️ Payment details couldn't load right now.\n"
                f"Your order *ORDER-{order.get('id', '?')}* is saved.\n"
                "Please contact us to complete payment."
            ),
            "reference": f"ORDER-{order.get('id', '?')}",
            "error":     str(exc),
        }

    # 3. Persist payment fields to DB
    try:
        oid = order.get("id")
        if oid:
            if method == "cash":
                update = {
                    "payment_method":    "cash",
                    "payment_status":    "pending_cash",
                    "payment_reference": pay.get("reference", f"ORDER-{oid}"),
                }
            else:
                update = {
                    "payment_method":    method,
                    "payment_status":    "awaiting_payment" if not pay.get("error") else "payment_error",
                    "payment_reference": pay.get("reference", f"ORDER-{oid}"),
                }
                if pay.get("url"):
                    update["payment_url"] = pay["url"]
                if pay.get("paypal_order_id"):
                    update["paypal_order_id"] = pay["paypal_order_id"]
            crud.update_order_payment(oid, business_id, update)
            log.info("payment persisted  order=%s  method=%s  status=%s",
                     oid, method, update["payment_status"])
    except Exception as exc:
        log.warning("update payment details failed: %s", exc)

    # 4. Update order status for cash (confirmed immediately)
    if method == "cash":
        try:
            from workflows.order_lifecycle import update_order_status_supabase
            update_order_status_supabase(order.get("id"), "pending_cash")
            log.info("cash order confirmed immediately  order=%s", order.get("id"))
        except Exception as exc:
            log.warning("cash order status update failed: %s", exc)

    # 5. Set conversation state
    auto_verified = pay.get("auto_verified", False)
    oid = order.get("id")
    ref = pay.get("reference", f"ORDER-{oid}")

    if method == "cash":
        _set_awaiting_fulfillment(phone, business_id, order_id=oid, reference=ref)
    elif auto_verified:
        _set_awaiting_payment(phone, business_id, order_id=oid, method=method, reference=ref)
        _write_state_data(phone, business_id, {"paypal_order_id": pay.get("paypal_order_id", "")})
    else:
        _set_awaiting_payment(phone, business_id, order_id=oid, method=method, reference=ref)

    # 6. Clear cart items (preserves state_data via UPSERT)
    _update_order_history(phone, business_id, cart)
    crud.clear_cart(phone, business_id)

    # 7. PDF invoice (non-blocking)
    _send_pdf_invoice(order, phone, business_id)

    # 8. Return payment message
    if method == "cash":
        total = float(order.get("total_price") or 0)
        return (
            f"✅ *Order confirmed!*\n\n"
            f"📦 Order   : *{ref}*\n"
            f"💰 Total   : *${total:.2f}*\n"
            f"💵 Payment : *Cash on delivery/pickup*\n\n"
            f"{'─' * 28}\n"
            f"🚚 *How would you like to receive your order?*\n\n"
            f"  1️⃣  *Delivery* — we bring it to you\n"
            f"  2️⃣  *Pickup* — collect from us\n\n"
            f"_Reply with *1* or *delivery* / *2* or *pickup*_"
        )
    return pay.get("message", "Order placed! We'll be in touch. 🙏")


# ── PDF invoice dispatch ──────────────────────────────────────────────────────

def _send_pdf_invoice(order: dict, phone: str, business_id: int) -> None:
    try:
        from services.pdf_invoice import generate_pdf_invoice
        pdf_path = generate_pdf_invoice(order)
    except Exception as exc:
        log.error("PDF generation failed: %s", exc)
        return
    try:
        biz      = crud.get_business_by_id(business_id)
        token    = crud.get_decrypted_token(biz) if biz else ""
        phone_id = biz.get("whatsapp_phone_id", "") if biz else ""
        if not token or not phone_id:
            return
        from integrations.whatsapp import send_whatsapp_document
        result = send_whatsapp_document(
            phone=phone, file_path=pdf_path,
            access_token=token, phone_number_id=phone_id,
            caption=f"📄 Invoice for ORDER-{order.get('id', '?')}",
        )
        if "error" not in result:
            log.info("PDF invoice sent  order=%s", order.get("id"))
    except Exception as exc:
        log.exception("_send_pdf_invoice error: %s", exc)
