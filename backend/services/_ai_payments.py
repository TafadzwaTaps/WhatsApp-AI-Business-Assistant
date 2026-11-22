"""
services/_ai_payments.py — Payment processing, order status display, PayPal
confirmation handling, payment instructions, and PDF invoice dispatch.

Imported by ai.py. Do not import ai.py from here (circular import).
"""

from services.money import format_money as _money
from services.money import order_money as _order_money
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
    except Exception as _swallowed_exc:
        from core.safelog import swallow as _swallow; _swallow(__name__, _swallowed_exc)

    order = {
        "id":              order_id,
        "business_id":     business_id,     # lets the payment code look up the business currency
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
        elif method == "banktransfer":
            from services.payment_service import generate_bank_transfer_instructions
            pay = generate_bank_transfer_instructions(order)
        elif method == "blik":
            from services.payment_service import generate_blik_instructions
            pay = generate_blik_instructions(order)
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


# ── Order progress (fulfilment only; payment is shown separately) ───────────────
# Pickup and delivery have different last two steps. Payment never decides which step an order is on, except that a
# PAID order is at least "Confirmed" (payment is frequently recorded before the owner moves the status).
_PROGRESS_LABELS = {
    "pickup":   ["Received", "Confirmed", "Preparing", "Ready for pickup", "Collected"],
    "delivery": ["Received", "Confirmed", "Preparing", "Out for delivery", "Delivered"],
    "generic":  ["Received", "Confirmed", "Preparing", "Ready", "Complete"],
    "service":  ["Received", "Confirmed", "Getting ready", "Ready for you", "Completed"],
}
_PROGRESS_STAGE = {
    "pending": 0, "pending_cash": 0, "awaiting_payment": 0, "awaiting_confirmation": 0, "payment_review": 0,
    "payment_pending": 0,
    "confirmed": 1, "paid": 1,
    "preparing": 2,
    "ready": 3, "out_for_delivery": 3,
    "delivered": 4, "completed": 4,
}
_PROGRESS_ETA = {1: "Preparing shortly", 2: "10–15 minutes"}     # shown only BEFORE the order is ready


def _progress_kind(fulfillment_method, flavor: str) -> str:
    if flavor == "service":
        return "service"
    fm = str(fulfillment_method or "").strip().lower()
    return fm if fm in ("pickup", "delivery") else "generic"


def build_order_progress(status, payment_status, fulfillment_method=None, flavor: str = "food") -> dict:
    """Pure: everything the status message shows about progress.

    Returns {"closed": None|"cancelled"|"refunded", "kind", "stage", "labels", "header": (icon, text),
             "progress": "✅ ✅ 🔵 ⬜ ⬜  Preparing", "legend": "Received → ...", "eta": str,
             "payment": (icon, key)}.
    The header and the dots come from the FULFILMENT status; the payment is a separate value."""
    s = str(status or "pending").strip().lower()
    p = str(payment_status or "pending").strip().lower()
    kind = _progress_kind(fulfillment_method, flavor)
    labels = _PROGRESS_LABELS[kind]
    pay_icon = ("✅" if p in ("paid", "confirmed") else "↩️" if p == "refunded"
                else "❌" if p in ("cancelled", "payment_failed") else "⏳")
    out = {"kind": kind, "labels": labels, "payment": (pay_icon, p), "eta": "", "closed": None,
           "legend": " → ".join(labels)}
    if s in ("cancelled", "refunded"):
        out.update(closed=s, stage=-1, header=("❌", "Order cancelled") if s == "cancelled" else ("↩️", "Order refunded"),
                   progress="❌ Cancelled" if s == "cancelled" else "↩️ Refunded", legend="")
        return out

    stage = _PROGRESS_STAGE.get(s, 0)
    if p in ("paid", "confirmed"):
        stage = max(stage, 1)                       # paid pulls a still-"pending" order to Confirmed, never beyond
    last = len(labels) - 1
    marks = []
    for i in range(len(labels)):
        if stage >= last or i < stage:
            marks.append("✅")
        elif i == stage:
            marks.append("🔵")
        else:
            marks.append("⬜")
    out["stage"] = stage
    out["progress"] = " ".join(marks) + "  " + labels[stage]
    if stage in _PROGRESS_ETA:
        out["eta"] = _PROGRESS_ETA[stage]

    if s == "out_for_delivery":
        header = ("🛵", "Out for delivery — on the way!")
    elif s in ("delivered", "completed"):
        header = (("🎉", "Collected — thank you!") if kind == "pickup" else
                  ("📦", "Delivered — thank you!") if kind == "delivery" else
                  ("✅", "Completed — thank you!") if kind == "service" else ("🎉", "Order completed"))
    elif s == "ready":
        header = (("🎉", "Ready for pickup!") if kind == "pickup" else
                  ("📦", "Ready — it will go out for delivery soon") if kind == "delivery" else
                  ("🎉", "Ready for you!") if kind == "service" else ("🎉", "Your order is ready!"))
    elif s == "preparing":
        header = (("🛠️", "Getting everything ready for you") if flavor == "service" else
                  ("📦", "Your order is being packed") if flavor == "retail" else ("👨‍🍳", "Your order is being prepared"))
    elif s in ("confirmed", "paid") or stage >= 1:
        header = ("✅", "Order confirmed")
    else:
        header = ("🕐", "Order received")
    out["header"] = header
    return out


def _order_status_message(order_id: int, phone: str, business_id: int, currency_sym: str = "$", flavor: str = "food") -> str:
    """Look up an order and return a rich formatted status message."""
    try:
        from workflows.order_lifecycle import get_order
        _ref_word = "BOOKING" if flavor == "service" else "ORDER"
        order = get_order(order_id)
        if not order:
            return (
                f"❓ I couldn't find *{_ref_word}-{order_id}*.\n\n"
                "Please check the number and try again, "
                "or type *help* for assistance."
            )

        # Phase 11 (AI Safety): ownership check — an order must belong to
        # BOTH this business AND this customer's own phone number, or it is
        # treated as not found. The previous version of this check only
        # rejected when BOTH conditions failed at once (an AND instead of
        # an OR), which meant any customer of the SAME business could view
        # another customer's order — full status, payment status, total,
        # dates — just by guessing/incrementing the numeric order id
        # ("ORDER-42"). This is exactly the "expose another customer's
        # data" case Phase 11 explicitly prohibits, so it's fixed here
        # rather than only detected/refused at the prompt-injection layer.
        _order_phone_matches = (
            str(order.get("customer_phone", "")).replace("+", "") == str(phone).replace("+", "")
        )
        _order_business_matches = order.get("business_id") == business_id
        if not (_order_phone_matches and _order_business_matches):
            return f"❓ I couldn't find *{_ref_word}-{order_id}* for your account."

        status         = order.get("status", "pending")
        payment_status = order.get("payment_status", "pending")
        total          = float(order.get("total_price") or 0)
        biz = None
        try:
            import crud as _crud
            biz = _crud.get_business_by_id(business_id)
        except Exception as _bexc:                      # only the date/currency polish is lost
            log.debug("order status: business lookup skipped: %s", type(_bexc).__name__)
        from services.tz_display import format_order_datetime
        created = format_order_datetime(order.get("created_at"), biz)
        # Currency: the order's own symbol, then the caller's (business setting), then the business row.
        sym = order.get("currency_symbol") or currency_sym or "$"
        total_text = _order_money({**order, "currency_symbol": sym}, biz, amount=total)

        prog = build_order_progress(status, payment_status, order.get("fulfillment_method"), flavor)
        icon, label = prog["header"]
        pay_icon = prog["payment"][0]
        progress = prog["progress"]
        eta_line = f"\n⏱ Estimated: *{prog['eta']}*" if prog["eta"] else ""
        legend_line = f"\n_{prog['legend']}_" if prog["legend"] else ""
        s_lower = str(status).lower()
        p_lower = str(payment_status).lower()

        agent_note = ""
        if p_lower == "awaiting_confirmation":
            agent_note = "\n🔍 _A team member is reviewing your payment proof._"
        elif p_lower in ("awaiting_payment", "pending") and s_lower == "pending":
            agent_note = "\n⏳ _Waiting for your payment._"

        _status_title  = "Booking Status" if flavor == "service" else "Order Status"
        _status_label  = "Booking " if flavor == "service" else "Order   "
        _menu_hint     = "book again" if flavor == "service" else "place a new order"
        return (
            f"📋 *{_status_title}*\n"
            f"{'─' * 26}\n"
            f"  {_status_label}: *{_ref_word}-{order_id}*\n"
            f"  Date    : {created}\n"
            f"  Total   : *{total_text}*\n"
            f"{'─' * 26}\n"
            f"{icon} {label}\n"
            f"{pay_icon} Payment : *{_friendly_payment_status(payment_status)}*\n"
            f"{'─' * 26}\n"
            f"📊 {progress}"
            f"{eta_line}"
            f"{legend_line}"
            f"{agent_note}\n"
            f"{'─' * 26}\n"
            f"_Type *menu* to {_menu_hint}._"
        )
    except Exception as exc:
        log.error("_order_status_message error: %s", exc)
        _ref_word_err = "BOOKING" if flavor == "service" else "ORDER"
        return f"❓ Could not load *{_ref_word_err}-{order_id}* right now. Please try again."


# ── PayPal paid handler ───────────────────────────────────────────────────────

def _handle_paypal_paid_message(
    phone: str,
    business_id: int,
    business_name: str,
    order_id,
    reference: str,
    is_service_business: bool = False,
) -> str:
    """
    Called when a user says "paid" while awaiting a PayPal payment.
    Checks PayPal API, marks order if confirmed, falls back to proof flow.
    """
    from services.payment_service import get_paypal_order_details
    from services._ai_state import _read_state_data, _reset_state, _set_awaiting_proof

    state_data      = _read_state_data(phone, business_id)
    paypal_order_id = state_data.get("paypal_order_id", "")
    _ref_word = "Booking" if is_service_business else "Order"
    _proc_word = "booking" if is_service_business else "order"

    if not paypal_order_id:
        log.warning("_handle_paypal_paid_message: no paypal_order_id  phone=%s", phone)
        _set_awaiting_proof(phone, business_id,
                            order_id=order_id, method="paypal", reference=reference)
        return (
            f"✅ *Got it! Thank you for paying.*\n\n"
            f"To confirm your PayPal payment, please send your *transaction ID* "
            f"or a *screenshot* of the payment.\n\n"
            f"📦 {_ref_word}: *{reference}*\n\n"
            f"_This helps us verify and process your {_proc_word}. 🙏_"
        )

    try:
        details = get_paypal_order_details(paypal_order_id)
    except Exception as exc:
        log.error("PayPal status check failed: %s", exc)
        details = {"paid": False, "error": str(exc)}

    if details.get("paid") and not order_id:
        details = {"paid": False}               # nothing to verify against: never confirm
    if details.get("paid") and order_id:
        # "COMPLETED" alone is not proof: the PayPal order must be THIS order's, in the currency
        # and for the amount we asked for. Anything else is left for the webhook / the owner.
        try:
            from workflows.order_lifecycle import get_order as _get_order
            from services.payment_service import (
                paypal_charge_for as _charge_for, order_currency_code as _cur_of,
                paypal_amount_matches as _amt_ok)
            _order = _get_order(order_id)
            _verified = bool(_order and _order.get("business_id") == business_id)
            if _verified and _order.get("payment_status") == "refunded":
                _verified = False                    # a refunded order is never re-confirmed as paid
            if _verified and crud.payment_blocked(_order):
                crud.alert_owner_payment_on_closed_order(_order, "customer_paid_message")
                _verified = False                    # a cancelled order is never confirmed as paid
            if _verified:
                _internal = details.get("internal_order_id")
                if _internal is not None and int(_internal) != int(order_id):
                    _verified = False
                _stored = (_order.get("paypal_order_id") or "").strip()
                if _stored and _stored != paypal_order_id:
                    _verified = False
                if str(details.get("currency") or "").upper() != _cur_of(_order):
                    _verified = False
                _chg = _charge_for(_order)
                if _chg is not None and not _amt_ok(details.get("amount", 0), _chg[2]):
                    _verified = False
        except Exception as exc:
            log.error("PayPal paid-message verification failed: %s", type(exc).__name__)
            _verified = False
        if not _verified:
            log.error("PayPal 'paid' claim NOT verified  order=%s  (id/currency/amount mismatch)", order_id)
            details = {"paid": False}

    if details.get("paid"):
        try:
            if order_id:
                # Atomic "mark paid once": if the webhook already did it, no second side effects.
                crud.claim_order_paid(order_id, business_id, {"payment_reference": reference})
        except Exception as exc:
            log.warning("PayPal payment status update failed: %s", exc)

        _reset_state(phone, business_id)
        amount = details.get("amount", 0)
        _next_step = "We'll see you at your appointment!" if is_service_business else "We're now preparing your order. You'll hear from us shortly! 🙌"
        return (
            f"✅ *PayPal Payment Confirmed!*\n\n"
            f"Thank you! Your payment of *{_money(amount, currency=details.get('currency'))}* has been verified.\n\n"
            f"📦 {_ref_word} : *{reference}*\n"
            f"📍 Status: *CONFIRMED*\n\n"
            f"{_next_step}\n\n"
            f"_Thank you for choosing *{business_name}*!_"
        )

    return (
        f"⏳ *We're verifying your PayPal payment.*\n\n"
        f"📦 {_ref_word}: *{reference}*\n\n"
        f"This usually only takes a few seconds. You'll receive an automatic "
        f"confirmation message as soon as your payment clears.\n\n"
        f"_No action needed — just wait for our message! 😊_\n"
        f"_Type *cancel* if you want to cancel this {_proc_word}._"
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
            f"💰 Total: *{_money(total, symbol=currency_sym)}*\n"
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
                except Exception as _swallowed_exc:
                    from core.safelog import swallow as _swallow; _swallow(__name__, _swallowed_exc)
                return (
                    f"⚠️ Sorry, *{m.group(1)}* is no longer available and was your "
                    f"only cart item — your cart is now empty.\n\n"
                    f"Type *menu* to see what's available. 😊"
                )

        # Phase 11 (AI Safety): never echo the raw exception text to the
        # customer — order_lifecycle.py's own ValueError messages can
        # include the underlying Postgres/Supabase error (schema, column,
        # or constraint details), which is internal database information
        # and must never reach WhatsApp. The real exc_str is already
        # logged above (log.warning) for the business/developer to see.
        return (
            "⚠️ Couldn't place your order right now.\n\n"
            "Please adjust your cart and try *checkout* again, "
            "or type *agent* if this keeps happening."
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
        except Exception as _swallowed_exc:
            from core.safelog import swallow as _swallow; _swallow(__name__, _swallowed_exc)

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
                order_id=order.get("id"),
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
        elif method == "banktransfer":
            from services.payment_service import generate_bank_transfer_instructions
            pay = generate_bank_transfer_instructions(order)
        elif method == "blik":
            from services.payment_service import generate_blik_instructions
            pay = generate_blik_instructions(order)
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
                if method == "paypal":
                    # Remember the currency this payment was created in, so a later change of the
                    # business currency cannot make a genuine payment look mismatched (stored only
                    # when the orders table has a currency column).
                    try:
                        from services.payment_service import order_currency_code as _cur_of
                        update["currency"] = _cur_of({**order, "business_id": business_id,
                                                      "currency_symbol": currency_sym})
                    except Exception as _cexc:
                        log.debug("order currency not persisted: %s", type(_cexc).__name__)
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
    # Service businesses see "BOOKING-39", product businesses see "ORDER-39"
    # — same underlying orders-table row either way, just labeled to match
    # what the customer actually did (booked an appointment vs placed an
    # order). Fixed here at the source since every downstream message in
    # this function reads this one `ref` value.
    _ref_prefix = "BOOKING" if is_service_business else "ORDER"
    ref = pay.get("reference", f"{_ref_prefix}-{oid}")

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
            _ref_label = "Booking" if is_service_business else "Order"
            return (
                f"✅ *All set!*\n\n"
                f"📦 {_ref_label}       : *{ref}*\n"
                f"🗓️ Appointment : *{date_disp} at {time_disp}*\n"
                f"💰 Total       : *{_money(total, symbol=currency_sym)}*\n"
                f"💵 Payment     : *Cash on arrival*\n\n"
                f"We look forward to seeing you! 😊\n"
                f"_Type *{ref.lower()}* anytime to check your status._"
            )
        # Rare: the slot was taken by someone else in the few seconds
        # between confirmation and payment. The order/payment is still
        # valid — only the specific time wasn't secured — so this is
        # honest about that rather than silently pretending it worked.
        return (
            f"✅ *Booking confirmed!* (payment received)\n\n"
            f"📦 Booking : *{ref}*\n"
            f"💰 Total   : *{_money(total, symbol=currency_sym)}*\n"
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
            f"💰 Total   : *{_money(total, symbol=currency_sym)}*\n"
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
