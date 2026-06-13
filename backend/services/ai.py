"""
ai.py — WaziBot Ordering Engine  v7  (refactored)

Single public entry point: generate_reply()

Internal helpers have been moved to sibling modules to reduce file size:
  _ai_lazy.py     — lazy module accessors (_states, _fuzzy, _order_parser, etc.)
  _ai_state.py    — conversation state read/write (carts.state_data JSONB)
  _ai_memory.py   — customer memory, cart I/O, order history
  _ai_intent.py   — all intent detection functions (pure text classifiers)
  _ai_products.py — product matching, quantity parsing, recommendations, formatters
  _ai_payments.py — payment processing, order status, PayPal handler, PDF invoice

The import contract is unchanged:
  from services.ai import generate_reply          ← still works
  from services import ai; ai.generate_reply(...) ← still works

═══════════════════════════════════════════════════════════════════════════════
CONVERSATION STATE MACHINE
═══════════════════════════════════════════════════════════════════════════════
  browsing        → normal shopping
  confirm_order   → double-confirmation before order is placed
  checkout        → waiting for payment method selection
  awaiting_payment→ order placed, waiting for "paid" reply
  awaiting_proof  → "paid" received, waiting for proof (txn ID / image)
  awaiting_fulfillment → proof received, asking delivery vs pickup
  awaiting_address → delivery chosen, waiting for address
  order_preview   → smart parser shown preview, waiting for "yes"
  human_handoff   → AI paused, human agent handling
  survey          → post-order satisfaction rating
  completed / cancelled

═══════════════════════════════════════════════════════════════════════════════
INTENT PRIORITY (do not reorder)
═══════════════════════════════════════════════════════════════════════════════
  P-3.5 Agent-echo suppression
  P-3   Human handoff mode
  P-2.5 Human handoff request detection
  P-2   Agent message detection (silence bot)
  P-1   Survey state
  P0    Global cancel
  P0.5  Refund / dispute request
  P0.7  Conversation completion detection
  P0.8  Urgency / delivery follow-up
  P0.2  Order preview state
  P0.3  Awaiting fulfillment
  P0.4  Awaiting address
  P1    Awaiting proof
  P2    Awaiting payment
  P3    Confirm order state
  P4    Checkout state
  P4.5  Reorder
  P5    Checkout trigger
  P6    Remove item
  P7    Add to cart (order parser → multi-item → single item)
  P8    Cart view
  P9    Browse menu
  P10   Order reference lookup
  P11   Help / greeting
  P12   Fallback
"""

import re
import logging
import crud

# ── Import all helpers ────────────────────────────────────────────────────────
# Lazy accessors (avoid circular imports at module level)
from services._ai_lazy import (
    _states, _fuzzy, _order_parser, _sales_ai, _handoff_mod,
)

# State management
from services._ai_state import (
    _read_state_data, _write_state_data,
    _get_state, _get_session, _get_pending_payment, _get_pending_proof,
    _set_state, _set_checkout_state, _set_confirm_state,
    _set_awaiting_payment, _set_awaiting_proof, _reset_state,
    _set_survey_state, _set_order_preview_state,
    _set_awaiting_fulfillment, _set_awaiting_address, _set_human_handoff,
    _in_survey_state, _check_rate_limit, _rate_limit_message,
    _get_active_order,
)

# Memory and cart
from services._ai_memory import (
    _get_memory, _update_order_history, _load_cart, _save_cart,
)

# Intent detection
from services._ai_intent import (
    _is_cancel, _is_refund_request, _is_reorder_request,
    _detect_fulfillment, _is_status_query,
    _extract_name, _is_conversation_done,
    _is_survey_response, _parse_survey_rating,
    _is_urgency_message, _is_agent_message, _is_human_request,
    _is_payment_confirmation, _extract_order_id,
    _is_yes, _is_no, _detect_payment_method, _intent,
    _is_proof_submission, _looks_like_txn_id,
    _is_introduction,
    _is_booking_intent, _is_cancel_booking,
    _is_reschedule_booking, _is_my_bookings_query,
    _is_abusive_message,
)

# Visual catalog intents — try/except guards against old _ai_intent.py on server
try:
    from services._ai_intent import (
        _is_catalog_request, _is_show_image_request,
        _extract_show_target, _is_more_products_request,
        _extract_show_category,
    )
except ImportError:
    def _is_catalog_request(t):       return False
    def _is_show_image_request(t):    return False
    def _extract_show_target(t):      return t
    def _is_more_products_request(t): return False
    def _extract_show_category(t):    return ""

# Products, formatters, multi-item
from services._ai_products import (
    _find_product, _qty, _recommend,
    _format_cart, _build_confirm_prompt, _build_payment_menu,
    _parse_multi_items,
)

# Payments, order status, PDF invoice
from services._ai_payments import (
    _friendly_payment_status,
    _build_payment_instructions, _order_status_message,
    _handle_paypal_paid_message, _process_payment, _send_pdf_invoice,
)

log = logging.getLogger(__name__)


# ═════════════════════════════════════════════════════════════════════════════
# MAIN ENGINE — generate_reply() is the only public function
# ═════════════════════════════════════════════════════════════════════════════

def generate_reply(
    message: str,
    phone: str,
    business_id: int,
    business_name: str,
    products: list,
    message_has_image: bool = False,
    message_is_from_agent: bool = False,
    voice_transcript: str | None = None,
    business_config: dict | None = None,
) -> str:
    """
    Single entry point called by the webhook for every incoming message.
    message_has_image=True signals that the customer sent a photo (payment proof).
    Returns a WhatsApp-formatted reply string.

    business_config (optional) — extra per-business settings from the businesses table:
      welcome_message (str)   — custom greeting on "hi" / first contact
      currency_symbol (str)   — e.g. "$", "R", "ZWL$"  (default "$")
      category (str)          — e.g. "restaurant", "pharmacy"
      menu_header (str)       — custom text shown above menu items
    All keys optional — missing keys fall back to defaults.
    """
    # ── Resolve per-business config ─────────────────────────────────────────
    _cfg                = business_config or {}
    _currency_sym       = (_cfg.get("currency_symbol") or "$").strip() or "$"
    _welcome_msg        = (_cfg.get("welcome_message") or "").strip()
    _menu_header        = (_cfg.get("menu_header") or "").strip()
    _biz_category       = (_cfg.get("category") or "").lower().strip()
    _is_service_biz     = bool(_cfg.get("is_service_business", False))
    _default_slot_mins  = int(_cfg.get("default_slot_mins", 60) or 60)
    # Catalog credentials — always defined, falls back to env vars
    import os as _os_cfg
    _phone_number_id    = (_cfg.get("phone_number_id") or "").strip()                           or _os_cfg.getenv("SHARED_PHONE_NUMBER_ID", "")
    _wa_token           = (_cfg.get("wa_token") or "").strip()                           or _os_cfg.getenv("SHARED_WA_TOKEN", "")

    if voice_transcript:
        text = voice_transcript.strip()
        log.info("▶ voice  phone=%s  biz=%s  transcript=%r", phone, business_id, text[:80])
    else:
        text = message.strip()
        log.info("▶ msg  phone=%s  biz=%s  img=%s  text=%r",
                 phone, business_id, message_has_image, text[:80])

    current_state = _get_state(phone, business_id)
    cart          = _load_cart(phone, business_id)

    log.info("state=%s  cart=%d", current_state, len(cart))

    # ══════════════════════════════════════════════════════════════════════════
    # P-3.5 — AGENT-SENT MESSAGE (echoed back from WhatsApp API)
    # ══════════════════════════════════════════════════════════════════════════
    if message_is_from_agent:
        log.info("P-3.5: agent-echo suppressed  phone=%s  state=%s", phone, current_state)
        return ""

    # ══════════════════════════════════════════════════════════════════════════
    # P-3 — HUMAN HANDOFF MODE
    # ══════════════════════════════════════════════════════════════════════════
    if current_state == "human_handoff":
        from services.conversation_service import is_ai_paused
        if is_ai_paused(current_state):
            log.info("human_handoff: AI paused — checking auto-resume  phone=%s", phone)
            handoff_result = _handoff_mod().handoff_customer_message(
                phone, business_id, text=text
            )
            if handoff_result == "__AUTO_RESUMED__":
                log.info("human_handoff: auto-resumed  phone=%s", phone)
                current_state = _get_state(phone, business_id)
            else:
                return handoff_result

    # ══════════════════════════════════════════════════════════════════════════
    # P-2.5 — HUMAN HANDOFF REQUEST DETECTION
    # ══════════════════════════════════════════════════════════════════════════
    if _handoff_mod().is_handoff_request(text) or _is_human_request(text):
        _set_human_handoff(phone, business_id)

        # Generate a support ticket number and store it for the agent + customer
        ticket = ""
        try:
            from services.whatsapp_catalog import generate_ticket_number
            customer = crud.get_or_create_customer(phone, business_id)
            cust_id  = customer.get("id") if customer else 0
            ticket   = generate_ticket_number(cust_id, business_id)
            _write_state_data(phone, business_id, {
                "state": "human_handoff",
                "session": {"ticket": ticket, "handoff_reason": "Customer request"},
            })
        except Exception as exc:
            log.debug("ticket generation failed: %s", exc)

        _handoff_mod().notify_dashboard(phone, business_id, business_name)
        log.info("human_handoff: triggered  phone=%s  biz=%s  ticket=%s", phone, business_id, ticket)
        return _handoff_mod().handoff_acknowledgement(business_name, ticket=ticket, reason="Customer request")

    # ══════════════════════════════════════════════════════════════════════════
    # P-2 — AGENT MESSAGE DETECTION
    # ══════════════════════════════════════════════════════════════════════════
    if _is_agent_message(text):
        log.info("agent message detected — suppressing reply  phone=%s", phone)
        return ""

    # ══════════════════════════════════════════════════════════════════════════
    # P-1 — SURVEY STATE
    # ══════════════════════════════════════════════════════════════════════════
    if current_state == "survey":
        if _is_survey_response(text):
            rating = _parse_survey_rating(text)
            _reset_state(phone, business_id)
            try:
                mem = _get_memory(phone, business_id)
                mem["last_rating"] = rating
                crud.save_user_memory(phone, business_id, mem)
            except Exception:
                pass
            follow_up = (
                "We're sorry to hear that. We'll work on improving! 🙏"
                if rating in ("poor", "average")
                else "That's wonderful to hear! 😊"
            )
            return (
                f"🙏 *Thank you for your feedback!*\n\n"
                f"Rating: *{rating.title()}*\n\n"
                f"{follow_up}\n\n"
                f"_We look forward to serving you again at *{business_name}*!_"
            )

        t_lower = text.lower().strip()
        if len(t_lower) > 8 and not _is_conversation_done(text):
            try:
                mem = _get_memory(phone, business_id)
                mem["last_suggestion"] = text[:200]
                crud.save_user_memory(phone, business_id, mem)
            except Exception:
                pass
            _reset_state(phone, business_id)
            return (
                f"📝 *Thank you for your suggestion!*\n\n"
                f"We really appreciate the feedback and will pass it on to our team.\n\n"
                f"_See you next time at *{business_name}*! 🙏_"
            )

        _reset_state(phone, business_id)
        return (
            f"Thanks again! Have a great day. 😊\n\n"
            f"_Type *menu* anytime to start a new order._"
        )

    # ══════════════════════════════════════════════════════════════════════════
    # P0 — GLOBAL CANCEL
    # ══════════════════════════════════════════════════════════════════════════
    if _is_cancel(text):
        if current_state == "browsing":
            t_lower      = text.lower()
            order_ref_id = _extract_order_id(text)
            if order_ref_id or any(w in t_lower for w in ["cancel order", "cancel my order"]):
                try:
                    from core.db import supabase as _sb
                    res = (
                        _sb.table("orders")
                        .select("id,status,payment_status,total_price")
                        .eq("customer_phone", phone)
                        .eq("business_id", business_id)
                        .in_("status", ["pending", "confirmed"])
                        .order("id", desc=True)
                        .limit(1)
                        .execute()
                    )
                    recent = res.data[0] if res.data else None
                except Exception:
                    recent = None

                if recent:
                    return (
                        f"🚫 *Cancel ORDER-{recent['id']}?*\n\n"
                        f"💰 Amount: ${float(recent['total_price']):.2f}\n"
                        f"📍 Status: {recent['status'].upper()}\n\n"
                        f"Reply *yes, cancel* to confirm cancellation, "
                        f"or type anything else to keep your order.\n\n"
                        f"_If you've already paid, reply *refund* and we'll arrange a refund._"
                    )

            return (
                "ℹ️ Nothing to cancel right now.\n\n"
                "Type *menu* to browse, or *cart* to see what's in your cart. 😊"
            )

        if current_state in ("checkout", "confirm_order"):
            _reset_state(phone, business_id)
            return (
                "🚫 *Checkout cancelled.*\n\n"
                f"{_format_cart(cart)}\n\n"
                "Your cart is saved. Type *checkout* whenever you're ready."
            )

        if current_state == "awaiting_payment":
            pending = _get_pending_payment(phone, business_id)
            if pending:
                order_id  = pending.get("order_id")
                reference = pending.get("reference", f"ORDER-{order_id}")
                try:
                    if order_id:
                        crud.update_order_payment(order_id, business_id, {
                            "payment_status": "cancelled",
                        })
                        from workflows.order_lifecycle import update_order_status_supabase
                        try:
                            update_order_status_supabase(order_id, "pending")
                        except Exception:
                            pass
                except Exception as exc:
                    log.warning("order cancel update failed: %s", exc)

            _reset_state(phone, business_id)
            return (
                "🚫 *Order cancelled.*\n\n"
                "If you've already sent payment, please contact us immediately "
                "and we'll sort it out.\n\n"
                "Type *menu* to start a new order. 😊"
            )

        if current_state == "awaiting_proof":
            _reset_state(phone, business_id)
            return (
                "🚫 *Cancelled.*\n\n"
                "If you've already made a payment, please contact us directly "
                "so we can verify and refund if needed.\n\n"
                "Type *menu* to browse. 😊"
            )

        _reset_state(phone, business_id)
        return "🚫 Cancelled. Type *menu* to start fresh. 😊"

    # ══════════════════════════════════════════════════════════════════════════
    # P0.5 — REFUND / DISPUTE REQUEST
    # ══════════════════════════════════════════════════════════════════════════
    if _is_refund_request(text):
        recent_order = None
        try:
            from core.db import supabase as _sb
            res = (
                _sb.table("orders")
                .select("id,status,payment_status,total_price,created_at")
                .eq("customer_phone", phone)
                .eq("business_id", business_id)
                .order("id", desc=True)
                .limit(1)
                .execute()
            )
            recent_order = res.data[0] if res.data else None
        except Exception as exc:
            log.warning("refund handler: order lookup failed: %s", exc)

        if recent_order:
            ref        = f"ORDER-{recent_order['id']}"
            pay_status = recent_order.get("payment_status", "pending")
            total      = float(recent_order.get("total_price") or 0)
            return (
                f"💳 *Refund / Dispute Request*\n\n"
                f"We've noted your request regarding *{ref}*.\n\n"
                f"  💰 Amount : ${total:.2f}\n"
                f"  📍 Payment: {pay_status.upper()}\n\n"
                f"Our team will review your request and get back to you shortly.\n\n"
                f"_For urgent issues, please contact us directly. "
                f"Refunds are processed within 24–48 hours once verified._\n\n"
                f"_Thank you for your patience. 🙏_"
            )
        return (
            f"💳 *Refund / Dispute Request*\n\n"
            f"We've noted your request and our team will be in touch shortly.\n\n"
            f"_Please include your order reference (e.g. *ORDER-13*) "
            f"to help us find your payment. Thank you! 🙏_"
        )

    # ══════════════════════════════════════════════════════════════════════════
    # P0.6 — ABUSIVE / OFFENSIVE LANGUAGE DETECTION
    # Calmly de-escalates rude or hostile messages instead of falling through
    # to a generic "I didn't quite get that". Tracks repeat offenses in
    # state_data.session.abuse_warnings — first offense gets a polite warning,
    # second+ gets a notice that continued abuse may lead to account
    # suspension. Never insults back; always professional.
    # ══════════════════════════════════════════════════════════════════════════
    if _is_abusive_message(text):
        session       = _get_session(phone, business_id) or {}
        warning_count = int(session.get("abuse_warnings", 0) or 0) + 1

        try:
            _write_state_data(phone, business_id, {
                "state": current_state,
                "session": {**session, "abuse_warnings": warning_count},
            })
        except Exception as exc:
            log.debug("abuse warning count write failed: %s", exc)

        log.warning(
            "ABUSE DETECTED  phone=%s  biz=%s  warning_count=%d  text=%r",
            phone, business_id, warning_count, text[:100],
        )

        if warning_count == 1:
            return (
                f"😕 We understand you may be frustrated, and we're here to help.\n\n"
                f"However, we kindly ask that you keep our conversation respectful — "
                f"our team works hard to assist every customer fairly.\n\n"
                f"_Let's start fresh: type *menu* to browse, *cart* to view your order, "
                f"or *agent* to speak with a human team member._ 🙏"
            )
        elif warning_count == 2:
            return (
                f"⚠️ *Second reminder:* please keep this conversation respectful.\n\n"
                f"Continued use of offensive or abusive language may result in "
                f"*restricted access* to this service.\n\n"
                f"We're happy to help — type *menu* to continue, or *agent* to "
                f"speak with a human team member. 🙏"
            )
        else:
            # Third+ offense — escalate to human + final warning
            try:
                _set_human_handoff(phone, business_id)
                _handoff_mod().notify_dashboard(phone, business_id, business_name)
            except Exception as exc:
                log.debug("abuse escalation handoff failed: %s", exc)
            return (
                f"🚫 *Final notice:* repeated offensive language has been logged "
                f"on this account.\n\n"
                f"Continued abuse may result in *suspension from this platform*.\n\n"
                f"_Your conversation has been flagged for our support team to review._"
            )


    # ══════════════════════════════════════════════════════════════════════════
    # P0.7 — CONVERSATION COMPLETION DETECTION
    # ══════════════════════════════════════════════════════════════════════════
    if _is_conversation_done(text) and current_state == "browsing":
        recent_ref = ""
        try:
            from core.db import supabase as _sb
            res = (
                _sb.table("orders")
                .select("id,status")
                .eq("customer_phone", phone)
                .eq("business_id", business_id)
                .order("id", desc=True)
                .limit(1)
                .execute()
            )
            if res.data:
                o = res.data[0]
                if o.get("status") in ("paid", "confirmed", "delivered"):
                    recent_ref = f"ORDER-{o['id']}"
        except Exception:
            pass

        order_line = f"\n📦 Order *{recent_ref}* is being taken care of.\n" if recent_ref else "\n"
        _set_survey_state(phone, business_id)
        return (
            f"😊 *You're welcome! We hope to see you again soon.*\n"
            f"{order_line}\n"
            f"Before you go — how was your experience today?\n\n"
            f"  1️⃣ *Excellent*\n"
            f"  2️⃣ *Good*\n"
            f"  3️⃣ *Average*\n"
            f"  4️⃣ *Poor*\n\n"
            f"_Reply with a number or word — this is optional and helps us improve! 🙏_"
        )

    # ══════════════════════════════════════════════════════════════════════════
    # P0.8 — URGENCY / DELIVERY FOLLOW-UP
    # ══════════════════════════════════════════════════════════════════════════
    if _is_urgency_message(text) and current_state == "browsing":
        active_order = None
        try:
            from core.db import supabase as _sb
            res = (
                _sb.table("orders")
                .select("id,status,payment_status,total_price")
                .eq("customer_phone", phone)
                .eq("business_id", business_id)
                .in_("status", ["pending", "confirmed", "paid"])
                .order("id", desc=True)
                .limit(1)
                .execute()
            )
            active_order = res.data[0] if res.data else None
        except Exception:
            pass

        if active_order:
            ref      = f"ORDER-{active_order['id']}"
            status   = active_order.get("status", "pending")
            fulfill  = active_order.get("fulfillment_method", "") or ""
            try:
                from services.whatsapp_catalog import build_progress_tracker
                tracker = build_progress_tracker(active_order["id"], status, fulfill)
                return (
                    f"⏳ *We hear you! Here's where things stand:*\n\n"
                    f"{tracker}\n"
                    f"_Our team has been notified and will update you shortly. 🙏_"
                )
            except Exception as exc:
                log.debug("progress tracker failed, using simple fallback: %s", exc)
                return (
                    f"⏳ *We hear you! Checking on your order...*\n\n"
                    f"📦 Order : *{ref}*\n"
                    f"📍 Status: *{status.upper()}*\n\n"
                    f"Our team has been notified of your message and will update you shortly.\n"
                    f"We apologise for any delay! 🙏\n\n"
                    f"_Type *{ref.lower()}* to see full order details._"
                )

        return (
            f"⏳ We're sorry you're waiting!\n\n"
            f"Please share your *order reference* (e.g. *ORDER-12*) "
            f"and we'll check the status for you right away. 🙏"
        )

    # ══════════════════════════════════════════════════════════════════════════
    # P0.2 — ORDER PREVIEW STATE
    # ══════════════════════════════════════════════════════════════════════════
    if current_state == "order_preview":
        session = _read_state_data(phone, business_id).get("session") or {}
        preview = session.get("preview_cart", [])

        if _is_cancel(text):
            _reset_state(phone, business_id)
            return "🚫 Order preview cancelled. Type *menu* to start fresh. 😊"

        if _is_yes(text):
            if not preview:
                _reset_state(phone, business_id)
                return "⚠️ Preview expired. Please type your order again."

            for line in preview:
                name  = line["name"]
                qty   = line["qty"]
                price = float(line["price"])
                found = False
                for item in cart:
                    if item["name"] == name:
                        item["qty"] += qty
                        found = True
                        break
                if not found:
                    cart.append({"name": name, "qty": qty, "price": price})

            _save_cart(phone, business_id, cart)
            _reset_state(phone, business_id)
            log.info("order_preview: confirmed  items=%d  phone=%s", len(preview), phone)

            sugg_text = ""
            try:
                _, _get_basket, _, _fmt = _sales_ai()
                if _get_basket:
                    mem         = _get_memory(phone, business_id)
                    basket_sugg = _get_basket(cart, products, mem)
                    sugg_text   = _fmt(basket_sugg, style="compact") if basket_sugg else ""
            except Exception as _exc:
                log.debug("sales_ai basket skipped (%s)", _exc)

            if not sugg_text:
                recs = _recommend(phone, business_id, products)
                if recs:
                    sugg_text = "💡 You might also like " + " or ".join(
                        f"*{r['name']}*" for r in recs) + "."

            rec_block = ("\n\n" + sugg_text) if sugg_text else ""
            return (
                f"✅ *Added to your cart!*\n\n"
                f"{_format_cart(cart)}"
                f"{rec_block}\n\n"
                f"_Type *checkout* when you're ready to order._"
            )

        return (
            f"Please reply *yes* to confirm, or *cancel* to start over.\n\n"
            + (f"{_format_cart(preview)}" if preview else "")
        )

    # ══════════════════════════════════════════════════════════════════════════
    # P0.3 — AWAITING FULFILLMENT (delivery vs pickup)
    # ══════════════════════════════════════════════════════════════════════════
    if current_state == "awaiting_fulfillment":
        session   = _read_state_data(phone, business_id).get("session") or {}
        order_id  = session.get("order_id")
        reference = session.get("reference", f"ORDER-{order_id}" if order_id else "your order")
        log.info("awaiting_fulfillment  order=%s  ref=%s  text=%r", order_id, reference, text)
        choice = _detect_fulfillment(text)

        if _is_cancel(text):
            _reset_state(phone, business_id)
            return "🚫 Cancelled. Type *menu* to start a new order."

        if choice == "delivery":
            _set_awaiting_address(phone, business_id, order_id=order_id, reference=reference)
            return (
                f"🚚 *Delivery selected!*\n\n"
                f"Please send your *delivery address* so we can arrange your order.\n\n"
                f"📦 Order: *{reference}*\n\n"
                f"_Just type your full address (street, suburb, city)._"
            )

        if choice == "pickup":
            try:
                crud.update_order_payment(order_id, business_id, {
                    "fulfillment_method": "pickup",
                })
            except Exception as exc:
                log.warning("pickup fulfillment save failed: %s", exc)
            _reset_state(phone, business_id)

            # Look up business address — show if present, hide gracefully if not
            address_line = ""
            try:
                biz_row = crud.get_business_by_id(business_id)
                addr = (biz_row.get("address") or "").strip() if biz_row else ""
                if addr:
                    address_line = f"\n📍 *Pickup Location:*\n{addr}\n"
            except Exception:
                pass

            return (
                f"🏪 *Pickup Confirmed!*\n\n"
                f"📦 Order: *{reference}*\n\n"
                f"⏱ *Estimated preparation time:*\n10–15 minutes\n"
                f"{address_line}\n"
                f"We'll notify you as soon as your order is ready. 😊\n\n"
                f"_Any questions? Type *{reference.lower()}* to check status._"
            )

        log.warning("awaiting_fulfillment: unrecognised reply  text=%r  order=%s", text, order_id)
        return (
            f"🤔 Please choose how you'd like to receive *{reference}*:\n\n"
            f"  1️⃣  *Delivery* — we bring it to you\n"
            f"  2️⃣  *Pickup* — collect from us\n\n"
            f"_Reply *1* / *delivery* or *2* / *pickup*_\n"
            f"_Type *cancel* to cancel._"
        )

    # ══════════════════════════════════════════════════════════════════════════
    # P0.4 — AWAITING ADDRESS
    # ══════════════════════════════════════════════════════════════════════════
    if current_state == "awaiting_address":
        session   = _get_session(phone, business_id)
        order_id  = session.get("order_id")
        reference = session.get("reference", f"ORDER-{order_id}")

        if _is_cancel(text):
            _reset_state(phone, business_id)
            return (
                "🚫 Address entry cancelled.\n\n"
                "Your order is still confirmed — type *menu* or contact us "
                "to arrange fulfillment."
            )

        address = text.strip()
        if len(address) < 5:
            return (
                "⚠️ That address looks too short. Please send your full delivery address.\n\n"
                f"_e.g. 42 Harare Street, Avondale, Harare_\n\n"
                f"_Type *cancel* to skip._"
            )

        try:
            crud.update_order_payment(order_id, business_id, {
                "fulfillment_method": "delivery",
                "delivery_address":   address,
            })
            log.info("delivery address saved  order=%s  address=%r", order_id, address[:60])
        except Exception as exc:
            log.warning("delivery address save failed: %s", exc)

        _reset_state(phone, business_id)
        return (
            f"📍 *Delivery address saved!*\n\n"
            f"  Address : _{address}_\n"
            f"  Order   : *{reference}*\n\n"
            f"Our team will arrange delivery and notify you with an ETA. 🛵\n\n"
            f"_Thank you for ordering from *{business_name}*! 🙏_"
        )

    # ══════════════════════════════════════════════════════════════════════════
    # P1 — AWAITING PROOF STATE
    # ══════════════════════════════════════════════════════════════════════════
    if current_state == "awaiting_proof":
        pending_proof = _get_pending_proof(phone, business_id)

        if not pending_proof:
            _reset_state(phone, business_id)
            return (
                "⚠️ I lost track of your payment session.\n\n"
                "Please type *checkout* to start again, or contact us directly."
            )

        order_id  = pending_proof.get("order_id")
        method    = pending_proof.get("method", "unknown")
        reference = pending_proof.get("reference", f"ORDER-{order_id}")

        is_proof, proof_value = _is_proof_submission(text, message_has_image)

        if is_proof:
            try:
                proof_note = (
                    "[IMAGE ATTACHED]" if proof_value == "image_attached"
                    else f"Txn/Proof: {proof_value}"
                )
                if order_id:
                    crud.update_order_payment(order_id, business_id, {
                        "payment_status": "awaiting_confirmation",
                        "payment_reference": f"{reference} | {proof_note}",
                    })
            except Exception as exc:
                log.warning("proof recording failed: %s", exc)

            _set_awaiting_fulfillment(phone, business_id,
                                      order_id=order_id, reference=reference)

            method_label = {"ecocash": "EcoCash", "paypal": "PayPal", "cash": "Cash"}.get(
                method, method.title()
            )
            proof_display = (
                "📸 *Image received.*"
                if proof_value == "image_attached"
                else f"📋 *Reference noted:* `{proof_value}`"
            )

            return (
                f"✅ *Payment proof received. Thank you!*\n\n"
                f"{proof_display}\n\n"
                f"📦 Order   : *{reference}*\n"
                f"💳 Method  : *{method_label}*\n\n"
                f"🔍 *A human agent is now reviewing your proof.*\n"
                f"Typical verification time: *5–15 minutes* ⏱\n\n"
                f"{'─' * 28}\n"
                f"🚚 *While we verify — how would you like to receive your order?*\n\n"
                f"  1️⃣  *Delivery* — we bring it to you\n"
                f"  2️⃣  *Pickup* — collect from us\n\n"
                f"_Reply *1* or *delivery* / *2* or *pickup*_"
            )

        method_label = {"ecocash": "EcoCash", "paypal": "PayPal"}.get(method, "payment")
        return (
            f"📋 *We need proof of your {method_label} payment to proceed.*\n\n"
            f"Please send:\n"
            f"  • Your *transaction ID* or *reference number*, OR\n"
            f"  • A *screenshot* of your payment confirmation\n\n"
            f"Order: *{reference}*\n\n"
            f"_Type *cancel* if you haven't paid yet._"
        )

    # ══════════════════════════════════════════════════════════════════════════
    # P2 — AWAITING PAYMENT STATE
    # ══════════════════════════════════════════════════════════════════════════
    if current_state == "awaiting_payment":
        pending = _get_pending_payment(phone, business_id)

        if not pending:
            _reset_state(phone, business_id)
            return "⚠️ I lost your payment session. Please type *checkout* to start again."

        order_id  = pending.get("order_id")
        method    = pending.get("method", "unknown")
        reference = pending.get("reference", f"ORDER-{order_id}")

        if _is_payment_confirmation(text):
            if method == "paypal":
                return _handle_paypal_paid_message(
                    phone=phone, business_id=business_id,
                    business_name=business_name,
                    order_id=order_id, reference=reference,
                )
            else:
                _set_awaiting_proof(phone, business_id,
                                    order_id=order_id, method=method, reference=reference)
                method_label = {"ecocash": "EcoCash", "paypal": "PayPal (email)"}.get(
                    method, "payment")
                return (
                    f"✅ *Got it! Thank you for paying.*\n\n"
                    f"To complete your order, please send your *{method_label} "
                    f"transaction ID* or a *screenshot* of your payment.\n\n"
                    f"📦 Order: *{reference}*\n\n"
                    f"_This helps us verify your payment quickly and process your order. 🙏_"
                )

        if message_has_image:
            _set_awaiting_proof(phone, business_id,
                                order_id=order_id, method=method, reference=reference)
            return generate_reply(
                message="image",
                phone=phone, business_id=business_id,
                business_name=business_name, products=products,
                message_has_image=True,
            )

        ref_id = _extract_order_id(text)
        if ref_id:
            return _order_status_message(ref_id, phone, business_id)

        confused_words = {
            "how", "what", "where", "instructions", "again", "resend",
            "send again", "help me", "show me", "details",
        }
        if any(w in text.lower() for w in confused_words):
            instructions = _build_payment_instructions(pending, business_id, business_name)
            return (
                f"{instructions}\n\n"
                f"{'─' * 28}\n"
                f"Once paid, reply *paid* to confirm.\n"
                f"_Type *cancel* to cancel this order._"
            )

        return (
            f"⏳ *Waiting for your payment.*\n\n"
            f"📦 Order  : *{reference}*\n\n"
            f"Once you've paid, reply *paid* and then send your "
            f"transaction ID or screenshot.\n\n"
            f"_Need the payment details again? Type *help*._\n"
            f"_To cancel, type *cancel*._"
        )

    # ══════════════════════════════════════════════════════════════════════════
    # P3 — CONFIRM ORDER STATE
    # ══════════════════════════════════════════════════════════════════════════
    if current_state == "confirm_order":
        session  = _get_session(phone, business_id)
        snapshot = session.get("cart_snapshot") or cart

        if _is_yes(text):
            _set_checkout_state(phone, business_id, snapshot)
            return _build_payment_menu(snapshot, business_id)

        if _is_no(text):
            _reset_state(phone, business_id)
            return (
                f"👌 No problem! Take your time.\n\n"
                f"{_format_cart(cart)}\n\n"
                "Type *checkout* when you're ready, or *remove [item]* to edit."
            )

        return (
            "Please reply *yes* to confirm your order or *no* to go back.\n\n"
            + _format_cart(snapshot)
        )

    # ══════════════════════════════════════════════════════════════════════════
    # P4 — CHECKOUT STATE
    # ══════════════════════════════════════════════════════════════════════════
    if current_state == "checkout":
        method = _detect_payment_method(text)

        if method in ("ecocash", "paypal", "cash"):
            session     = _get_session(phone, business_id)
            cart_to_use = session.get("cart_snapshot") or cart
            return _process_payment(
                method=method, cart=cart_to_use,
                phone=phone, business_id=business_id, business_name=business_name,
            )

        from services.payment_service import available_methods
        try:
            pay_settings = crud.get_business_payment_settings(business_id)
        except Exception:
            pay_settings = {}
        methods = available_methods({**pay_settings, "business_id": business_id})

        opts, num = [], 1
        for m in methods:
            label = {"ecocash": "EcoCash", "paypal": "PayPal", "cash": "Cash on delivery"}.get(
                m, m)
            opts.append(f"  {num}️⃣  *{label}*")
            num += 1

        return (
            "I didn't catch that — please choose how you'd like to pay:\n\n"
            + "\n".join(opts) +
            "\n\n_Reply with the number or name (e.g. *1*, *ecocash*, *cash*)_\n"
            "_Type *cancel* to go back._"
        )

    # ══════════════════════════════════════════════════════════════════════════
    # P4.3 — INTRODUCTION DETECTION (before product fuzzy match)
    # "My name is Tafadzwa" must never reach the product matcher.
    # ══════════════════════════════════════════════════════════════════════════
    if _is_introduction(text):
        detected_name = _extract_name(text)
        if detected_name:
            try:
                _mem = _get_memory(phone, business_id)
                _mem["customer_name"] = detected_name
                crud.save_user_memory(phone, business_id, _mem)
                log.info("introduction: name saved  name=%r  phone=%s", detected_name, phone)
            except Exception:
                pass
        name_to_show = detected_name or "there"
        hint = f"*{products[0]['name']}*" if products else "something"
        return (
            f"Nice to meet you, *{name_to_show}*! 😊\n\n"
            f"I'm the ordering assistant for *{business_name}*.\n"
            f"Type *menu* to see what we have, or just tell me what you'd like — "
            f"e.g. _{hint}_"
        )

    # ══════════════════════════════════════════════════════════════════════════
    # General intent detection (browsing state)
    # ══════════════════════════════════════════════════════════════════════════
    intent = _intent(text)
    log.info("intent=%s", intent)

    # ══════════════════════════════════════════════════════════════════════════
    # P4.4 — BOOKING HANDLER (service businesses only)
    # Gated on _is_service_biz — retail businesses never reach this block.
    # ══════════════════════════════════════════════════════════════════════════
    if _is_service_biz:
        from services.booking_service import (
            parse_booking_request, format_booking_preview, create_booking,
            get_bookings_for_customer, cancel_booking as _cancel_booking,
            format_booking_confirmation, _format_date, _format_time,
        )
        from datetime import date as _date

        if _is_my_bookings_query(text):
            bookings = get_bookings_for_customer(business_id, phone)
            if not bookings:
                return (
                    f"📅 You don't have any bookings with us yet.\n\n"
                    f"Type *book* to schedule an appointment with *{business_name}*! 😊"
                )
            lines = []
            for b in bookings[:5]:
                d, t = b.get("booking_date",""), b.get("start_time","")
                svc  = b.get("service_name","Appointment")
                stat = b.get("status","").title()
                try:    d_fmt = _format_date(_date.fromisoformat(d))
                except: d_fmt = d
                lines.append(f"  📌 *{svc}* — {d_fmt} {_format_time(t) if t else ''} [{stat}]")
            return (
                f"📅 *Your Bookings at {business_name}*\n\n"
                + "\n".join(lines) +
                "\n\n_Type *cancel booking* to cancel | *reschedule booking* to change_"
            )

        if _is_cancel_booking(text):
            bookings = get_bookings_for_customer(business_id, phone)
            active   = [b for b in bookings if b.get("status") in ("confirmed","pending","rescheduled")]
            if not active:
                return "ℹ️ You have no active bookings to cancel.\n\nType *book* to make a new appointment."
            booking = active[0]
            if _cancel_booking(booking["id"], business_id):
                try:    d_fmt = _format_date(_date.fromisoformat(booking["booking_date"]))
                except: d_fmt = booking.get("booking_date","")
                return f"🚫 *Booking Cancelled*\n\nYour appointment on *{d_fmt}* has been cancelled.\n\n_Type *book* to make a new appointment._"
            return "⚠️ Could not cancel your booking. Please contact us directly."

        if current_state == "awaiting_booking_date":
            session = _read_state_data(phone, business_id).get("session") or {}
            if _is_cancel(text):
                _reset_state(phone, business_id)
                return "🚫 Booking cancelled. Type *book* to start again."
            parsed = parse_booking_request(text)
            if not parsed.date_str:
                from services.booking_service import _parse_date_str, _resolve_relative_date
                raw = _parse_date_str(text) or _resolve_relative_date(text)
                if raw:
                    parsed.date_str = raw.isoformat()
                    parsed.has_booking_intent = True
            if parsed.date_str:
                if parsed.time_str:
                    _write_state_data(phone, business_id, {"state": "booking_confirm",
                        "session": {**session, "booking_date": parsed.date_str, "time_str": parsed.time_str}})
                    from services.booking_service import ParsedBooking as _PB
                    return format_booking_preview(_PB(has_booking_intent=True, date_str=parsed.date_str,
                        time_str=parsed.time_str, duration_hrs=_default_slot_mins/60), business_name)
                _write_state_data(phone, business_id, {"state": "awaiting_booking_time",
                    "session": {**session, "booking_date": parsed.date_str}})
                try:    d_fmt = _format_date(_date.fromisoformat(parsed.date_str))
                except: d_fmt = parsed.date_str
                return (f"📅 Got it — *{d_fmt}*.\n\nWhat time would you like?\n\n"
                        "_e.g. *10am*, *2:30pm*_\n_Type *cancel* to go back._")
            return ("📅 I didn't catch that date.\n\n_e.g. *tomorrow*, *Friday*, *14/09*_\n_Type *cancel* to go back._")

        if current_state == "awaiting_booking_time":
            session = _read_state_data(phone, business_id).get("session") or {}
            if _is_cancel(text):
                _reset_state(phone, business_id)
                return "🚫 Booking cancelled. Type *book* to start again."
            from services.booking_service import _parse_time, ParsedBooking as _PB
            time_str = _parse_time(text)
            if time_str:
                booking_date = session.get("booking_date","")
                _write_state_data(phone, business_id, {"state": "booking_confirm",
                    "session": {**session, "time_str": time_str}})
                return format_booking_preview(_PB(has_booking_intent=True, date_str=booking_date,
                    time_str=time_str, duration_hrs=_default_slot_mins/60,
                    service_name=session.get("service","")), business_name)
            return ("🕐 I didn't catch that time.\n\n_e.g. *10am*, *2:30pm*, *14:00*_\n_Type *cancel* to go back._")

        if current_state == "booking_confirm":
            session = _read_state_data(phone, business_id).get("session") or {}
            if _is_cancel(text) or _is_no(text):
                _reset_state(phone, business_id)
                return "🚫 Booking cancelled. Type *book* to start fresh."
            if _is_yes(text):
                booking = create_booking(business_id=business_id, customer_phone=phone,
                    booking_date=session.get("booking_date",""),
                    start_time=session.get("time_str","09:00"),
                    duration_hrs=_default_slot_mins/60,
                    service_name=session.get("service",""))
                _reset_state(phone, business_id)
                if booking:
                    try:
                        from services.calendar_service import sync_booking
                        sync_booking(booking, business_name)
                    except Exception: pass
                    return format_booking_confirmation(booking, business_name)
                return "⚠️ Could not save your booking. Please try again or contact us. 🙏"
            return "Please reply *yes* to confirm your booking or *no* to cancel."

        if _is_booking_intent(text):
            parsed = parse_booking_request(text)
            if parsed.confidence >= 0.85 and parsed.date_str and parsed.time_str:
                _write_state_data(phone, business_id, {"state": "booking_confirm",
                    "session": {"booking_date": parsed.date_str, "time_str": parsed.time_str,
                                "service": parsed.service_name or ""}})
                return format_booking_preview(parsed, business_name)
            elif parsed.date_str:
                _write_state_data(phone, business_id, {"state": "awaiting_booking_time",
                    "session": {"booking_date": parsed.date_str, "service": parsed.service_name or ""}})
                try:    d_fmt = _format_date(_date.fromisoformat(parsed.date_str))
                except: d_fmt = parsed.date_str
                return (f"📅 Great! *{d_fmt}* works.\n\nWhat time would you like?\n\n"
                        "_e.g. *10am*, *2:30pm*_\n_Type *cancel* to go back._")
            else:
                _write_state_data(phone, business_id, {"state": "awaiting_booking_date",
                    "session": {"service": parsed.service_name or ""}})
                svc_hint = (", ".join(f"*{p['name']}*" for p in products[:4])
                             if products else "our services")
                return (f"📅 I'd love to book you in at *{business_name}*!\n\n"
                        f"Services: {svc_hint}\n\n"
                        f"What date works for you?\n\n_e.g. *tomorrow*, *Friday*, *14 June*_\n_Type *cancel* to go back._")


    # ══════════════════════════════════════════════════════════════════════════
    # P4.5 — REORDER
    # ══════════════════════════════════════════════════════════════════════════
    if _is_reorder_request(text):
        mem         = _get_memory(phone, business_id)
        last_orders = mem.get("last_orders", [])
        if not last_orders:
            return (
                "🛒 No previous orders found!\n\n"
                "Type *menu* to browse and place your first order. 😊"
            )
        last_item_names = last_orders[-1]

        name_map = {p["name"].lower(): p for p in products}
        rebuilt  = []
        missing  = []
        for name in last_item_names:
            p = name_map.get(name.lower())
            if p:
                rebuilt.append({"name": p["name"], "qty": 1, "price": float(p["price"])})
            else:
                missing.append(name)

        if not rebuilt:
            unavail = ", ".join(missing)
            return (
                f"😔 Your previous items (*{unavail}*) are no longer available.\n\n"
                "Type *menu* to see the current menu."
            )

        _save_cart(phone, business_id, rebuilt)
        log.info("reorder  items=%d  phone=%s", len(rebuilt), phone)

        cart_text = _format_cart(rebuilt)
        note      = ""
        if missing:
            note = f"\n\n⚠️ Some items were unavailable: *{', '.join(missing)}*"

        return (
            f"🔄 *Rebuilt your last order!*\n\n"
            f"{cart_text}"
            f"{note}\n\n"
            f"_Type *checkout* to confirm, or *menu* to modify._"
        )

    # ══════════════════════════════════════════════════════════════════════════
    # P5 — CHECKOUT TRIGGER
    # ══════════════════════════════════════════════════════════════════════════
    if intent == "checkout":
        if not cart:
            return (
                "🛒 Your cart is empty!\n\n"
                "Type *menu* to browse, then add something — "
                "e.g. _\"Sadza\"_ or _\"2 Beef\"_"
            )
        if not _check_rate_limit(phone, business_id):
            return _rate_limit_message()
        _set_confirm_state(phone, business_id, cart)
        return _build_confirm_prompt(cart)

    # ══════════════════════════════════════════════════════════════════════════
    # P6 — REMOVE ITEM
    # ══════════════════════════════════════════════════════════════════════════
    if intent == "remove":
        t_lower     = text.lower().strip()
        search_term = re.sub(
            r"^(remove|delete|take out|take off|drop|cancel)\s+",
            "", t_lower, flags=re.IGNORECASE
        ).strip()
        log.debug("remove: search_term=%r  cart_items=%s",
                  search_term, [i["name"] for i in cart])

        matched_item = None

        for item in cart:
            if item["name"].lower() in t_lower:
                matched_item = item
                break

        if not matched_item and search_term:
            for item in cart:
                if search_term in item["name"].lower():
                    matched_item = item
                    break

        if not matched_item and search_term:
            search_words = [w for w in search_term.split() if len(w) >= 3]
            if search_words:
                for item in cart:
                    if all(w in item["name"].lower() for w in search_words):
                        matched_item = item
                        break

        if not matched_item and search_term:
            search_words = [w for w in search_term.split() if len(w) >= 4]
            for word in search_words:
                for item in cart:
                    if word in item["name"].lower():
                        matched_item = item
                        break
                if matched_item:
                    break

        if matched_item:
            cart.remove(matched_item)
            _save_cart(phone, business_id, cart)
            log.info("remove: removed  item=%r  phone=%s", matched_item["name"], phone)
            return f"🗑️ Removed *{matched_item['name']}* from your cart.\n\n{_format_cart(cart)}"

        log.info("remove: no match  search_term=%r  cart_items=%s",
                 search_term, [i["name"] for i in cart])
        return f"⚠️ I couldn't find that item in your cart.\n\n{_format_cart(cart)}"

    # ══════════════════════════════════════════════════════════════════════════
    # P7 — ADD TO CART (order parser → multi-item → single item)
    # ══════════════════════════════════════════════════════════════════════════
    if intent == "order":

        # ── P7a: Smart Order Parser ───────────────────────────────────────────
        _text_words   = text.split()
        _has_connector = any(
            w.lower() in {"and", "ne", "na", "&", "+", ",", "futi", "zvakare"}
            for w in _text_words
        )
        _has_quantity = bool(re.search(
            r"\b[2-9]\d*\b|\b(?:two|three|four|five|six|seven|eight|nine|ten)\b",
            text, re.IGNORECASE,
        ))

        if (len(_text_words) >= 4 or
                (_has_connector and _has_quantity) or
                (len(_text_words) >= 2 and _has_connector)):
            try:
                _parse_fn, _preview_fn = _order_parser()
                _parsed = _parse_fn(text, products, existing_cart=cart)

                if _parsed.is_confident and len(_parsed.items) >= 2:
                    preview_msg = _preview_fn(_parsed, business_name)
                    if preview_msg:
                        _set_order_preview_state(phone, business_id, _parsed.cart_lines())
                        log.info("order_parser: showing preview  items=%d  conf=%.2f  phone=%s",
                                 len(_parsed.items), _parsed.confidence, phone)
                        return preview_msg
            except Exception as exc:
                log.warning("order_parser invocation failed (%s) — using existing path", exc)

        # ── P7b: Multi-item check ─────────────────────────────────────────────
        multi = _parse_multi_items(text, products)
        if multi:
            added_names = []
            blocked     = []
            for product, qty in multi:
                try:
                    fresh = crud.get_product_by_name(business_id, product["name"])
                    if fresh:
                        product = fresh
                except Exception:
                    pass

                product_name = product["name"]
                available    = product.get("stock")
                in_cart      = next((i["qty"] for i in cart if i["name"] == product_name), 0)

                if available is not None and in_cart + qty > available:
                    blocked.append(
                        f"*{product_name}* (out of stock)" if available == 0
                        else f"*{product_name}* (only {available} left)"
                    )
                    continue

                found = False
                for item in cart:
                    if item["name"] == product_name:
                        item["qty"] += qty
                        found = True
                        break
                if not found:
                    cart.append({"name": product_name, "qty": qty, "price": float(product["price"])})
                added_names.append(f"*{product_name}*" + (f" ×{qty}" if qty > 1 else ""))

            if added_names:
                _save_cart(phone, business_id, cart)
                log.info("multi-add  items=%s  phone=%s", added_names, phone)
                blocked_note = f"\n\n⚠️ Could not add: {', '.join(blocked)}" if blocked else ""
                return (
                    f"👍 Added {', '.join(added_names)} to your cart.\n\n"
                    f"{_format_cart(cart)}"
                    f"{blocked_note}"
                    f"\n\n_Type *checkout* when you're ready to order._"
                )

        # ── P7c: Single item ──────────────────────────────────────────────────
        product, qty = _fuzzy().extract_product_and_quantity(text, products)

        if product is None:
            product = _find_product(text, products)
            if product:
                qty = _qty(text)

        if product:
            try:
                fresh = crud.get_product_by_name(business_id, product["name"])
                if fresh:
                    product = fresh
            except Exception as exc:
                log.warning("stock refresh failed: %s", exc)

            product_name = product["name"]
            available    = product.get("stock")
            if available is not None:
                in_cart = next((i["qty"] for i in cart if i["name"] == product_name), 0)
                if in_cart + qty > available:
                    if available == 0:
                        return (
                            f"😔 *{product_name}* is currently out of stock.\n\n"
                            "Type *menu* to see what's available."
                        )
                    return (
                        f"⚠️ Only *{available}* unit(s) of *{product_name}* available "
                        f"(you already have {in_cart} in your cart)."
                    )

            found = False
            for item in cart:
                if item["name"] == product_name:
                    item["qty"] += qty
                    found = True
                    break
            if not found:
                cart.append({"name": product_name, "qty": qty, "price": float(product["price"])})

            _save_cart(phone, business_id, cart)
            log.info("added  %s ×%d  phone=%s", product_name, qty, phone)

            qty_label = f" ×{qty}" if qty > 1 else ""
            msg = (
                f"👍 Nice choice! Added *{product_name}*{qty_label} to your cart.\n\n"
                f"{_format_cart(cart)}"
            )

            try:
                _get_sugg, _, _get_upsell, _fmt = _sales_ai()
                if _get_sugg:
                    mem         = _get_memory(phone, business_id)
                    suggestions = _get_sugg(product, cart, products, mem)
                    upsell      = _get_upsell(product, products, cart)
                    sugg_text   = _fmt(suggestions, upsell=upsell, style="compact")
                    if sugg_text:
                        msg += "\n\n" + sugg_text
                else:
                    recs = _recommend(phone, business_id, products, exclude=product_name)
                    if recs:
                        msg += "\n\n💡 You might also like " + " or ".join(
                            f"*{r['name']}*" for r in recs) + "."
            except Exception as _exc:
                log.debug("sales_ai skipped (%s) — using _recommend fallback", _exc)
                recs = _recommend(phone, business_id, products, exclude=product_name)
                if recs:
                    msg += "\n\n💡 You might also like " + " or ".join(
                        f"*{r['name']}*" for r in recs) + "."

            msg += "\n\n_Type *checkout* when you're ready to order._"
            return msg

    # ══════════════════════════════════════════════════════════════════════════
    # P8 — CART VIEW
    # ══════════════════════════════════════════════════════════════════════════
    if intent == "cart":
        reply = _format_cart(cart)
        if cart:
            reply += "\n\n_Ready? Type *checkout* to place your order._"
        return reply

    # ══════════════════════════════════════════════════════════════════════════
    # P8.5 — SHOW PRODUCT IMAGE (Phases 3-6, 8)
    # "show me flowers", "picture of roses", "what do cakes look like"
    # Phase 8: graceful fallback — never blocks ordering or menu
    # ══════════════════════════════════════════════════════════════════════════
    if _is_show_image_request(text):
        target = _extract_show_target(text)
        if target and products:
            # _find_product already imported at module level — no inner import needed
            matched = _find_product(target, products)
            if matched:
                try:
                    from services.whatsapp_catalog import (
                        send_product_image, build_product_card_text,
                    )
                    # Use env-var credentials as fallback when biz_config not yet updated
                    result = send_product_image(_phone_number_id, _wa_token, phone, matched, _currency_sym)
                    if result.get("fallback"):
                        return result.get("text") or build_product_card_text(matched, _currency_sym)
                    # Image sent directly via API — return short follow-up text
                    name = matched.get("name", target)
                    return (
                        f"*{name}* — {_currency_sym}{float(matched.get('price', 0)):.2f}\n\n"
                        f"Type *{name.lower()}* to add to cart. 🛒"
                    )
                except Exception as _exc:
                    import logging as _lg
                    _lg.getLogger("wazibot").warning("show_image error: %s", _exc)
                    # Fall through to normal menu handling
            else:
                pass  # Product not found — fall through

    # ══════════════════════════════════════════════════════════════════════════
    # P9.5 — VISUAL CATALOG / GALLERY (Phases 4-6, 9)
    # "catalog", "gallery", "show products", "show flowers", "more"
    # Phase 9: batched (CATALOG_BATCH_SIZE products per call)
    # Phase 10: always uses business_id — no cross-tenant leakage
    # ══════════════════════════════════════════════════════════════════════════
    _cat_filter = _extract_show_category(text)
    if _is_catalog_request(text) or _is_more_products_request(text) or _cat_filter:
        if not products:
            return f"📦 No products available yet. Check back soon! 🙏"

        from services.whatsapp_catalog import (
            send_catalog, send_product_gallery,
            has_product_images, build_text_catalog,
        )

        # Pagination: read page from session state
        session      = _get_session(phone, business_id)
        catalog_page = int((session or {}).get("catalog_page", 0))
        if not (_is_more_products_request(text)):
            catalog_page = 0  # fresh request resets page

        if _cat_filter:
            result = send_product_gallery(
                _phone_number_id, _wa_token, phone,
                products, _cat_filter, _currency_sym,
            )
        else:
            result = send_catalog(
                _phone_number_id, _wa_token, phone,
                products, _currency_sym, page=catalog_page,
            )

        # Persist next page in session
        if result.get("has_more"):
            _write_state_data(phone, business_id, {
                "state": current_state,
                "session": {**(session or {}), "catalog_page": result.get("next_page", 0)},
            })
        else:
            # Reset pagination
            _write_state_data(phone, business_id, {
                "state": current_state,
                "session": {**(session or {}), "catalog_page": 0},
            })

        fallback_text = result.get("fallback_text")
        if fallback_text:
            return fallback_text
        # Images were sent directly via API — return a short guide text
        section = _cat_filter.title() if _cat_filter else business_name
        more_hint = "\n_Type *more* to see more products._" if result.get("has_more") else ""
        return (
            f"🛍️ *{section}*\n\n"
            f"_Type a product name to add to cart._"
            f"{more_hint}"
        )

    # ══════════════════════════════════════════════════════════════════════════
    # P9 — BROWSE MENU
    # ══════════════════════════════════════════════════════════════════════════
    if intent == "browse":
        if not products:
            return f"📋 *{business_name}*\n\nNo items available yet. Check back soon! 🙏"

        # ── Personalised greeting ────────────────────────────────────────────
        mem         = _get_memory(phone, business_id)
        order_count = int(mem.get("order_count", 0) or 0)
        cust_name   = (mem.get("customer_name") or "").strip()
        greeting    = ""
        if order_count >= 2 and cust_name:
            greeting = f"👋 Welcome back, *{cust_name}*! Great to see you again.\n\n"
        elif order_count >= 2:
            greeting = f"👋 Welcome back! You've ordered *{order_count} times* from us. 🙏\n\n"

        # ── Visual menu: send images when products have image_url ────────────
        try:
            from services.whatsapp_catalog import has_product_images, send_catalog, send_text_message
            if has_product_images(products) and _phone_number_id and _wa_token:
                if greeting:
                    try:
                        send_text_message(_phone_number_id, _wa_token, phone, greeting.strip())
                    except Exception:
                        pass
                result = send_catalog(_phone_number_id, _wa_token, phone, products, _currency_sym, page=0)
                fallback = result.get("fallback_text")
                if not fallback:
                    hint = products[0]["name"] if products else "an item"
                    more = "\n_Type *more* to see more._" if result.get("has_more") else ""
                    return (
                        f"_Type a product name to add to cart — e.g. *{hint}*_{more}"
                    )
                # Image sending failed — fall through to text menu
        except Exception as _vis_exc:
            import logging as _vl
            _vl.getLogger("wazibot").debug("visual menu error (non-fatal): %s", _vis_exc)

        # ── Text menu (no images, or image send failed) ──────────────────────
        lines = []
        for i, p in enumerate(products):
            note = ""
            s = p.get("stock")
            if s is not None and s <= 5:
                note = f"  ⚠️ _only {s} left_"
            lines.append(f"  {i+1}. *{p['name']}* — {_currency_sym}{float(p['price']):.2f}{note}")

        recs     = _recommend(phone, business_id, products)
        rec_text = ""
        if recs:
            rec_text = "\n\n⭐ *You usually order:*\n" + "\n".join(
                f"  • {r['name']}" for r in recs)

        header       = _menu_header or f"📋 *{business_name} Menu*"
        hint_product = products[0]["name"] if products else "an item"
        add_hint     = f"_Type a name to add it — e.g. *{hint_product}* or *2 {hint_product}*_"
        return (
            f"{greeting}"
            f"{header}\n\n"
            + "\n".join(lines)
            + rec_text
            + f"\n\n{add_hint}"
        )

    # ══════════════════════════════════════════════════════════════════════════
    # P10 — ORDER REFERENCE LOOKUP
    # ══════════════════════════════════════════════════════════════════════════
    ref_id = _extract_order_id(text)
    if ref_id:
        return _order_status_message(ref_id, phone, business_id)

    # ══════════════════════════════════════════════════════════════════════════
    # P11 — HELP / GREETING
    # ══════════════════════════════════════════════════════════════════════════
    if intent == "help":
        hint        = f"*{products[0]['name']}*" if products else "an item"
        mem         = _get_memory(phone, business_id)
        order_count = int(mem.get("order_count", 0) or 0)
        cust_name   = (mem.get("customer_name") or "").strip()

        # Category: prefer business_config, fall back to DB lookup
        biz_category = _biz_category
        if not biz_category:
            try:
                biz_row      = crud.get_business_by_id(business_id)
                biz_category = (biz_row.get("category") or "").lower().strip() if biz_row else ""
            except Exception:
                pass

        _FOOD_CATS   = {"food", "restaurant", "food & beverage", "café", "cafe",
                        "fast food", "takeaway", "takeout", "grocery"}
        _RETAIL_CATS = {"fashion", "boutique", "clothing", "apparel", "hardware",
                        "tools", "electronics"}
        _HEALTH_CATS = {"pharmacy", "health", "wellness", "beauty"}

        if any(c in biz_category for c in _FOOD_CATS):     action_verb = "order"
        elif any(c in biz_category for c in _RETAIL_CATS): action_verb = "shop"
        elif any(c in biz_category for c in _HEALTH_CATS): action_verb = "get"
        else:                                               action_verb = "order"

        if order_count >= 5 and cust_name:
            greeting = (
                f"👋 Hey *{cust_name}*! Great to have you back — "
                f"you've ordered *{order_count} times* with us! 🏆\n\n"
            )
        elif order_count >= 2 and cust_name:
            greeting = f"👋 Welcome back, *{cust_name}*! Great to see you again.\n\n"
        elif order_count >= 2:
            greeting = f"👋 Welcome back! Glad to see you again 😊\n\n"
        elif cust_name:
            greeting = f"👋 Hey *{cust_name}*! Welcome to *{business_name}*!\n\n"
        else:
            greeting = f"👋 Hey! Welcome to *{business_name}*!\n\n"

        # Per-business custom welcome overrides the default greeting block
        if _welcome_msg:
            # Replace the auto-generated greeting with the custom one,
            # but still personalise with the customer's name if known.
            custom = _welcome_msg
            if cust_name and "{name}" in custom:
                custom = custom.replace("{name}", cust_name)
            elif cust_name and not any(
                word in custom.lower() for word in [cust_name.lower(), "you", "back"]
            ):
                custom = f"👋 Hey *{cust_name}*! {custom}"
            greeting = custom + "\n\n"

        return (
            f"{greeting}"
            f"Here's how to {action_verb}:\n"
            f"  📋 *menu* — see everything we offer\n"
            f"  🛍️ Type a name — e.g. _{hint}_\n"
            f"  🛒 *cart* — review what you've added\n"
            f"  ✅ *checkout* — place your {action_verb}\n"
            f"  ❌ *remove [item]* — remove from cart\n"
            f"  🔍 *ORDER-9* — check an order status\n"
            f"  🚫 *cancel* — cancel checkout at any time\n"
            f"  🔄 *repeat last order* — reorder quickly\n\n"
            f"What can I get you today? 😊"
        )

    # ── Name capture fallback (mid-conversation mentions, e.g. "call me Rudo") ──
    # Pure introductions are handled at P4.3 above.
    detected_name = _extract_name(text)
    if detected_name:
        try:
            _mem = _get_memory(phone, business_id)
            if not _mem.get("customer_name"):
                _mem["customer_name"] = detected_name
                crud.save_user_memory(phone, business_id, _mem)
                log.info("name captured (fallback)  name=%r  phone=%s", detected_name, phone)
        except Exception:
            pass

    # ══════════════════════════════════════════════════════════════════════════
    # P10.5 — CONTEXTUAL ACTIVE ORDER QUERIES
    # ══════════════════════════════════════════════════════════════════════════
    if _is_status_query(text):
        active = _get_active_order(phone, business_id)
        if active:
            return _order_status_message(active["id"], phone, business_id)

    t_lower = text.lower().strip()
    if any(w in t_lower for w in ["delivery", "pickup", "collect", "address",
                                    "eta", "when", "how long"]):
        active = _get_active_order(phone, business_id)
        if active:
            fm  = active.get("fulfillment_method", "")
            da  = active.get("delivery_address", "")
            ref = f"ORDER-{active['id']}"
            if "address" in t_lower and fm == "delivery":
                addr_line = f"\n📍 Address: _{da}_" if da else "\n📍 No address saved yet."
                return (
                    f"📦 *{ref}* — Delivery{addr_line}\n\n"
                    f"_Type *{ref.lower()}* for full status._"
                )
            return _order_status_message(active["id"], phone, business_id)

    # ══════════════════════════════════════════════════════════════════════════
    # P12 — FALLBACK
    # ══════════════════════════════════════════════════════════════════════════
    product = _find_product(text, products)
    if product:
        return generate_reply(
            message=product["name"],
            phone=phone, business_id=business_id,
            business_name=business_name, products=products,
        )

    active_order = _get_active_order(phone, business_id)

    if cart and active_order:
        ref  = f"ORDER-{active_order['id']}"
        hint = f"e.g. _{products[0]['name']}_" if products else ""
        return (
            f"🤔 I didn't catch that.\n\n"
            f"📦 You have an active order: *{ref}*\n"
            f"{_format_cart(cart) if cart else ''}\n\n"
            f"  📋 *menu* — browse products {'| 🛍️ ' + hint if hint else ''}\n"
            f"  🛒 *cart* — view your cart\n"
            f"  ✅ *checkout* — place your order\n"
            f"  🔍 *{ref}* — check order status\n"
        )

    if active_order:
        ref = f"ORDER-{active_order['id']}"
        return (
            f"🤔 I'm not sure what you mean — but happy to help!\n\n"
            f"You have an active order *{ref}* — type it to see the status.\n\n"
            f"Or type *menu* to browse and add more items, or *help* for all commands. 😊"
        )

    if cart:
        hint = f"e.g. _{products[0]['name']}_" if products else ""
        return (
            f"🤔 I'm not sure what you mean — but here's where you're at:\n\n"
            f"{_format_cart(cart)}\n\n"
            f"  ✅ *checkout* — place your order\n"
            f"  📋 *menu* — browse more items {'| 🛍️ ' + hint if hint else ''}\n"
            f"  🗑️ *remove [item]* — remove something\n"
            f"  🆘 *help* — see all commands\n"
        )

    hint = f"e.g. _{products[0]['name']}_" if products else "e.g. _Burger_"
    return (
        f"🤖 Hmm, I'm not sure what you mean by that — but no worries! 😊\n\n"
        f"Here's what I can help with:\n"
        f"  📋 *menu* — browse products\n"
        f"  🛍️ Type a product name — {hint}\n"
        f"  🛒 *cart* — view your cart\n"
        f"  ✅ *checkout* — place your order\n"
        f"  🔍 *ORDER-9* — check order status\n"
        f"  🔄 *repeat last order* — reorder quickly\n"
        f"  🙋 *agent* — talk to a human\n\n"
        f"_Type *help* anytime to see this list again._"
    )
