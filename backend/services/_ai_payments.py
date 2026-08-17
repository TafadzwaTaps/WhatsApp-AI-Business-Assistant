"""
services/_ai_payments.py — Payment processing, order status display, PayPal
confirmation handling, payment instructions, and PDF invoice dispatch.

Imported by ai.py. Do not import ai.py from here (circular import).
"""

import logging

import crud
from services.whatsapp_catalog import send_text_message

log = logging.getLogger(__name__)


# ── Payment status labels ─────────────────────────────────────────────────────

def _friendly_payment_status(status: str) -> str:
    labels = {
        "pending":            "Pending",
        # Bug fix: "pending_cash" means the customer chose cash but hasn't
        # paid yet — it was mislabeled "Confirmed (Cash)", showing next to a
        # ⏳ (pending) icon, which contradicted itself. Only actually-received
        # payments (payment_status="paid") should ever say "Confirmed".
        "pending_cash":       "Pending (Cash on delivery/pickup)",
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

def _build_payment_instructions(pending: dict, business_id: int, business_name: str, currency_sym: str = "$") -> str:
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
        "id":              order_id,
        "total_price":     total,
        "business_name":   business_name,
        "currency_symbol": currency_sym,   # ensures payment instructions use correct currency
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

# Food-flavor is the default/original wording — used whenever a flavor
# isn't passed in, so any caller that hasn't been updated keeps working
# exactly as before.
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

# Overrides for the entries that were explicitly food-specific ("being
# prepared" / chef emoji, "enjoy your meal!"). Every other status (payment
# states, ready, out_for_delivery, cancelled) already reads fine for any
# business type, so only these are overridden per flavor.
_LIFECYCLE_OVERRIDES = {
    "retail": {
        "preparing": ("📦", "Your order is being packed"),
        "delivered": ("📦", "Delivered — thanks for your order!"),
        "completed": ("🎉", "Order completed"),
    },
    "service": {
        "preparing": ("🛠️", "Getting everything ready for you"),
        "ready":     ("🎉", "Ready for you!"),
        "delivered": ("✅", "Completed — thank you!"),
        "completed": ("🎉", "Booking completed"),
    },
}


# Bug fix: the compact 5-dot progress bar previously computed its fill level
# from an incomplete set of if/elif branches that never matched "ready" or
# "out_for_delivery" — both silently fell through to the same "Order received"
# (1/5) branch as a brand-new order, making the bar visually go BACKWARDS as
# an order actually progressed. This ordered stage list replaces that logic
# with a single lookup, so every status maps to a correct, forward-moving
# position — and doubles as the source for a short ETA hint per stage,
# fulfilling the "we'll notify you with an ETA" promise made when an address
# is saved (see the awaiting_address handler in ai.py).
_DOT_STAGE_ORDER = [
    "pending", "pending_cash", "awaiting_payment",   # 0: Order received
    "awaiting_confirmation", "payment_review",         # 1: Verifying payment
    "confirmed", "paid",                               # 2: Payment confirmed
    "preparing",                                        # 3: Preparing
    "ready", "out_for_delivery",                        # 4: Ready / on the way
    "delivered", "completed",                           # 5: Complete
]
_DOT_STAGE_INDEX = {
    "pending": 0, "pending_cash": 0, "awaiting_payment": 0,
    "awaiting_confirmation": 1, "payment_review": 1,
    "confirmed": 2, "paid": 2,
    "preparing": 3,
    "ready": 4, "out_for_delivery": 4,
    "delivered": 5, "completed": 5,
}
_DOT_STAGE_LABELS = [
    "Order received", "Verifying payment", "Payment confirmed",
    "Preparing", "Ready / On the way", "Complete!",
]
_DOT_ETA_HINTS = {
    0: "Awaiting payment",
    1: "5–15 minutes",
    2: "Preparing shortly",
    3: "10–15 minutes",
    4: "Ready now / on the way",
    5: "Delivered",
}


def _order_status_message(order_id: int, phone: str, business_id: int, currency_sym: str = "$", flavor: str = "food") -> str:
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
        # Currency: prefer whatever is stored on the order itself (set when
        # the order was created), then fall back to the symbol passed in by
        # the caller (the business's current setting).
        sym = order.get("currency_symbol") or currency_sym or "$"

        effective_key = payment_status if payment_status in _LIFECYCLE_ICONS else status
        icon, label   = _LIFECYCLE_ICONS.get(
            effective_key,
            _LIFECYCLE_ICONS.get(status, ("📋", status.upper()))
        )
        # Apply flavor-specific wording override (e.g. "being packed" instead
        # of "being prepared" for retail, "getting ready for you" for a
        # salon/service business).
        _flavor_overrides = _LIFECYCLE_OVERRIDES.get(flavor, {})
        if effective_key in _flavor_overrides:
            icon, label = _flavor_overrides[effective_key]
        elif status in _flavor_overrides:
            icon, label = _flavor_overrides[status]

        pay_icon = "✅" if payment_status in ("paid", "confirmed") else "⏳"

        s_lower = status.lower()
        p_lower = payment_status.lower()

        if s_lower == "cancelled":
            progress  = "❌ Cancelled"
            eta_line  = ""
        else:
            # Status usually drives the stage; payment_status can pull it
            # forward (e.g. "paid" while status is still "pending") but never
            # backward — a delivered order stays at stage 5 even if payment
            # bookkeeping shows something odd.
            stage_from_status  = _DOT_STAGE_INDEX.get(s_lower, 0)
            stage_from_payment = _DOT_STAGE_INDEX.get(p_lower, 0)
            stage = max(stage_from_status, stage_from_payment)
            stage = min(stage, 5)

            dots = "".join("✅ " if i < stage else ("⏳ " if i == stage else "⬜ ") for i in range(5))
            progress = f"{dots.strip()}  {_DOT_STAGE_LABELS[stage]}"

            eta = _DOT_ETA_HINTS.get(stage, "")
            eta_line = f"\n⏱ Estimated: *{eta}*" if eta else ""

        agent_note = ""
        if p_lower == "awaiting_confirmation":
            agent_note = "\n🔍 _A team member is reviewing your payment proof._"
        elif p_lower in ("awaiting_payment", "pending") and s_lower == "pending":
            agent_note = "\n⏳ _Waiting for your payment._"

        # Short legend so first-time customers know what the dots mean —
        # omitted for cancelled orders since the dot bar itself is replaced
        # by a single "❌ Cancelled" line in that case.
        legend_line = (
            "\n_Received → Verifying → Confirmed → Preparing → Complete_"
            if s_lower != "cancelled" else ""
        )

        return (
            f"📋 *Order Status*\n"
            f"{'─' * 26}\n"
            f"  Order   : *ORDER-{order_id}*\n"
            f"  Date    : {created}\n"
            f"  Total   : *{sym}{total:.2f}*\n"
            f"{'─' * 26}\n"
            f"{icon} {label}\n"
            f"{pay_icon} Payment : *{_friendly_payment_status(payment_status)}*\n"
            f"{'─' * 26}\n"
            f"📊 {progress}"
            f"{eta_line}"
            f"{legend_line}"
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

def _notify_business_new_order(
    business_id: int,
    order: dict,
    customer_phone: str,
    cart: list,
    currency_sym: str,
    phone_number_id: str,
    wa_token: str,
) -> None:
    """
    Send an instant WhatsApp notification to the business owner the moment a
    new order is placed — previously orders were created completely silently
    and the only way to find out was to manually check the Orders page.

    Fire-and-forget: never raises, never blocks or delays the customer's
    checkout flow, and simply does nothing if the business has no contact
    number or WhatsApp credentials configured.
    """
    try:
        biz = crud.get_business_by_id(business_id)
        if not biz:
            return
        owner_phone = (biz.get("contact_phone") or "").strip()
        if not owner_phone:
            log.debug("order notification skipped — no contact_phone set  biz=%s", business_id)
            return

        order_id  = order.get("id")
        ref       = f"ORDER-{order_id}" if order_id else "New order"
        total     = float(order.get("total_price") or 0)
        items_txt = order.get("product_name") or ", ".join(
            f"{i.get('name','item')} ×{i.get('qty',1)}" for i in (cart or [])
        )

        message = (
            f"🔔 *New Order Received!*\n\n"
            f"📦 {ref}\n"
            f"🛍️ {items_txt}\n"
            f"💰 Total: *{currency_sym}{total:.2f}*\n"
            f"📱 Customer: {customer_phone}\n\n"
            f"_Check your WaziBot dashboard for full details._"
        )

        send_text_message(phone_number_id, wa_token, owner_phone, message)
        log.info("order notification sent  biz=%s  order=%s  to=%s", business_id, order_id, owner_phone)
    except Exception as exc:
        # Never let a notification failure affect the customer's order.
        log.warning("order notification failed (non-critical): %s", exc)


def _process_payment(
    method: str,
    cart: list,
    phone: str,
    business_id: int,
    business_name: str,
    currency_sym: str = "$",
    phone_number_id: str = "",
    wa_token: str = "",
    is_service_business: bool = False,
) -> str:
    from workflows.order_lifecycle import create_order_supabase
    from services.payment_service import (
        generate_ecocash_instructions,
        paypal_payment,
        generate_cash_instructions,
    )
    from services._ai_state import (
        _set_awaiting_payment, _set_awaiting_fulfillment, _write_state_data,
        _get_session, _reset_state, _set_human_handoff,
    )
    from services._ai_memory import _update_order_history

    # For service businesses, the appointment slot is chosen and validated
    # BEFORE reaching payment (see the awaiting_booking_date/_time/
    # booking_confirm states in ai.py) — read it now, before the state
    # transitions below move away from "checkout" and this session data
    # becomes unreachable.
    _booking_date = _booking_time = None
    if is_service_business:
        _pre_payment_session = _get_session(phone, business_id)
        _booking_date = _pre_payment_session.get("booking_date")
        _booking_time = _pre_payment_session.get("booking_time")

    # 1. Create order
    # Fix: order notification and pay-settings injection previously ran
    # INSIDE this same try block. If either of those non-critical side
    # effects raised anything unexpected, it was caught by the generic
    # `except Exception` below and told the customer "Something went wrong
    # saving your order" — even when create_order_supabase() had already
    # succeeded and the order genuinely existed in the database. Both are
    # now moved outside this try block, each with its own isolated
    # try/except, so a side-effect failure can never masquerade as an
    # order-creation failure again.
    try:
        log.info("checkout  method=%s  phone=%s  items=%d", method, phone, len(cart))
        order = create_order_supabase(
            business_id=business_id,
            customer_phone=phone,
            cart=cart,
            payment_method=method,
        )
        order["business_name"]   = business_name
        order["currency_symbol"] = currency_sym  # ensures EcoCash/PayPal instructions use correct currency
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

    # 1b. Non-critical side effects — order already exists at this point, so
    # neither of these can ever cause the customer to see an "order failed"
    # message. Each is independently isolated; a failure in one doesn't
    # skip the other or affect anything below.
    try:
        pay_settings = crud.get_business_payment_settings(business_id)
        order.update(pay_settings)
    except Exception as exc:
        log.warning("payment settings injection failed (non-critical): %s", exc)

    try:
        # Instant order notification to the business owner — fire-and-forget.
        _notify_business_new_order(
            business_id, order, phone, cart, currency_sym, phone_number_id, wa_token,
        )
    except Exception as exc:
        log.warning("order notification failed (non-critical): %s", exc)

    # 1c. Create the actual appointment booking, using the slot the customer
    # already picked and had validated during checkout. Unlike the two
    # side-effects above, whether this succeeds genuinely matters to the
    # customer (they need to know if their time was secured) — but it still
    # can't turn into a false "order failed" message, since the order is
    # already committed by this point regardless of what happens here. The
    # rare failure case (someone else took the slot in the few seconds
    # between confirmation and payment) is surfaced honestly in the reply
    # built below, not silently swallowed.
    _booking_created = None
    if is_service_business and _booking_date and _booking_time:
        try:
            from services.booking_service import create_booking
            product_name = cart[0].get("name", "") if cart else ""
            _booking_created = create_booking(
                business_id=business_id, customer_phone=phone,
                booking_date=_booking_date, start_time=_booking_time,
                duration_hrs=1.0, service_name=product_name,
                notes=f"Linked to ORDER-{order.get('id', '')}",
            )
            if _booking_created:
                log.info("booking created  id=%s  order=%s  date=%s  time=%s",
                         _booking_created.get("id"), order.get("id"), _booking_date, _booking_time)
            else:
                log.warning("booking creation lost race at payment time  order=%s  date=%s  time=%s",
                            order.get("id"), _booking_date, _booking_time)
        except Exception as exc:
            log.warning("booking creation failed: %s", exc)

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

    if method == "cash" and is_service_business:
        if _booking_created or not (_booking_date and _booking_time):
            # Nothing further to ask — the appointment slot was already
            # chosen and validated before payment.
            _reset_state(phone, business_id)
        else:
            # Rare conflict case — hand off to a human rather than trying
            # to route the customer's next free-text reply through a new
            # retry state (the booking states all lead toward payment,
            # which has already happened here — building a separate
            # "already-paid retry" sub-flow for an edge case this rare
            # isn't worth the added complexity).
            _set_human_handoff(phone, business_id)
    elif method == "cash":
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
    if method == "cash" and is_service_business:
        total = float(order.get("total_price") or 0)
        if _booking_created:
            from services.booking_service import _format_date, _format_time
            from datetime import date as _date_cls
            try:
                date_disp = _format_date(_date_cls.fromisoformat(_booking_date))
            except Exception:
                date_disp = _booking_date
            time_disp = _format_time(_booking_time)
            return (
                f"✅ *All set!*\n\n"
                f"📦 Order       : *{ref}*\n"
                f"🗓️ Appointment : *{date_disp} at {time_disp}*\n"
                f"💰 Total       : *{currency_sym}{total:.2f}*\n"
                f"💵 Payment     : *Cash on arrival*\n\n"
                f"We look forward to seeing you! 😊\n"
                f"_Type *{ref.lower()}* anytime to check your status._"
            )
        # Rare: the slot was taken by someone else in the few seconds
        # between confirmation and payment. The order/payment is still
        # valid — only the specific time wasn't secured — so this is
        # honest about that rather than silently pretending it worked.
        return (
            f"✅ *Order confirmed!* (payment received)\n\n"
            f"📦 Order   : *{ref}*\n"
            f"💰 Total   : *{currency_sym}{total:.2f}*\n"
            f"💵 Payment : *Cash on arrival*\n\n"
            f"😔 Unfortunately your chosen time was just taken by someone else.\n"
            f"Please reply with another day/time and we'll lock it in — "
            f"e.g. *\"Friday 2pm\"*."
        )
    if method == "cash":
        total = float(order.get("total_price") or 0)
        return (
            f"✅ *Order confirmed!*\n\n"
            f"📦 Order   : *{ref}*\n"
            f"💰 Total   : *{currency_sym}{total:.2f}*\n"
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
