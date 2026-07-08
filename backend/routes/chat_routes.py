"""
routes/chat_routes.py — Chat inbox (CRM), human handoff, cart debug, and PayPal routes.

Routes: /chat/*, /cart/*, /payments/paypal/*, /payments/manual/confirm
"""

import json
import logging
from typing import Optional

from fastapi import APIRouter, Request, HTTPException, Depends, Query
from pydantic import BaseModel, validator

import crud
from core.auth import require_business, get_current_user
from core.crypto import TokenDecryptionError

log = logging.getLogger("wazibot")
router = APIRouter()

# Runtime config — set by main.py
send_whatsapp  = None
manager        = None
SHARED_WA_TOKEN        = ""
SHARED_PHONE_NUMBER_ID = ""


# ── Chat inbox ────────────────────────────────────────────────────────────────

@router.get("/chat/customers")
def chat_customers(search: Optional[str] = Query(None), user=Depends(get_current_user)):
    return crud.get_customers_for_business(user["business_id"], search=search)


@router.get("/chat/conversations")
def chat_conversations(unread_only: bool = Query(False), user=Depends(get_current_user)):
    # Strict tenant isolation: each business sees ONLY its own customers.
    # This applies to both dedicated-number and shared-number businesses.
    return crud.get_chat_conversations(user["business_id"], filter_unread=unread_only)


@router.get("/chat/conversations/{phone:path}")
def chat_messages_by_phone(
    phone: str,
    limit:  int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user=Depends(get_current_user),
):
    from urllib.parse import unquote
    phone = unquote(phone)
    from core.db import supabase as _supa
    customer = (
        _supa.table("customers").select("*")
        .eq("phone", phone).eq("business_id", user["business_id"]).limit(1).execute().data
    )
    if not customer:
        legacy = crud.get_messages_for_phone(user["business_id"], phone)
        if not legacy: raise HTTPException(404, f"No conversation found for phone: {phone}")
        return {
            "customer_id": None, "phone": phone,
            "total_fetched": len(legacy), "limit": limit, "offset": offset,
            "messages": [{
                "id": m.get("id"), "text": m.get("message", ""),
                "direction": "outgoing" if m.get("direction") == "out" else "incoming",
                "is_read": True, "status": "sent", "created_at": m.get("created_at"),
            } for m in legacy],
        }
    c    = customer[0]
    msgs = crud.get_messages_by_customer(c["id"], limit=limit, offset=offset)
    return {"customer_id": c["id"], "phone": phone, "total_fetched": len(msgs),
            "limit": limit, "offset": offset, "messages": msgs}


@router.get("/chat/messages/{customer_id}")
def chat_messages(
    customer_id: int,
    limit:  int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user=Depends(get_current_user),
):
    customer = crud.get_customer_by_id(customer_id, user["business_id"])
    if not customer: raise HTTPException(404, "Customer not found")
    msgs = crud.get_messages_by_customer(customer_id, limit=limit, offset=offset)
    return {"customer_id": customer_id, "phone": customer["phone"],
            "total_fetched": len(msgs), "limit": limit, "offset": offset, "messages": msgs}


@router.post("/chat/read/{customer_id}")
def mark_read(customer_id: int, user=Depends(get_current_user)):
    customer = crud.get_customer_by_id(customer_id, user["business_id"])
    if not customer: raise HTTPException(404, "Customer not found")
    crud.mark_messages_read(customer_id, user["business_id"])
    return {"ok": True, "customer_id": customer_id}


# ── Chat send ─────────────────────────────────────────────────────────────────

class ChatSendRequest(BaseModel):
    customer_id: int
    text: str
    @validator("text")
    def text_valid(cls, v):
        v = v.strip()
        if not v:         raise ValueError("Message cannot be empty")
        if len(v) > 4096: raise ValueError("Message too long")
        return v


@router.post("/chat/send")
async def chat_send(body: ChatSendRequest, user=Depends(require_business)):
    bid      = user["business_id"]
    customer = crud.get_customer_by_id(body.customer_id, bid)
    # Shared number: customer may belong to a different business_id
    if not customer and (SHARED_WA_TOKEN or SHARED_PHONE_NUMBER_ID):
        try:
            from core.db import supabase as _sb
            res = _sb.table("customers").select("*").eq("id", body.customer_id).limit(1).execute()
            if res.data:
                customer = res.data[0]
                log.info("shared-number cross-tenant lookup  customer_id=%s  customer_biz=%s  agent_biz=%s",
                         body.customer_id, customer.get("business_id"), bid)
        except Exception as exc:
            log.warning("cross-tenant lookup failed: %s", exc)
    if not customer:
        raise HTTPException(404, "Customer not found")

    business = crud.get_business_by_id(bid)
    try:
        token = crud.get_decrypted_token(business)
    except TokenDecryptionError as exc:
        raise HTTPException(503, "WhatsApp token cannot be decrypted. Re-enter it in Settings.")

    # On shared number, the customer's business_id may differ from the agent's
    customer_biz_id = customer.get("business_id", bid)
    if customer_biz_id != bid:
        business = crud.get_business_by_id(customer_biz_id) or business
        try:
            token = crud.get_decrypted_token(business)
        except Exception:
            token = ""

    has_phone_id = bool(business.get("whatsapp_phone_id"))
    has_token    = bool(token)

    # Use customer's actual business_id for logging — critical for shared number
    # where customer_biz_id (e.g. 5) may differ from the agent's bid (e.g. 4)
    log_biz_id = customer_biz_id

    crud.log_message(log_biz_id, customer["phone"], "out", body.text)
    agent_username = user.get("username", "Agent")
    msg = crud.create_message(
        customer["id"], log_biz_id, body.text, "outgoing",
        sender_type="agent", sender_name=agent_username, agent_id=agent_username,
    )

    wa_result: dict = {}
    if has_token and has_phone_id:
        wa_result = send_whatsapp(business["whatsapp_phone_id"], token, customer["phone"], body.text)
    elif SHARED_WA_TOKEN and SHARED_PHONE_NUMBER_ID:
        wa_result = send_whatsapp(SHARED_PHONE_NUMBER_ID, SHARED_WA_TOKEN, customer["phone"], body.text)
    else:
        missing = [k for k, v in {"phone_number_id": has_phone_id, "token": has_token}.items() if not v]
        wa_result = {"error": f"credentials missing: {missing}"}

    try:
        from workflows.human_handoff import record_agent_reply
        record_agent_reply(customer["phone"], log_biz_id)
    except Exception as exc:
        log.debug("record_agent_reply skipped: %s", exc)

    # Broadcast to both the agent's ws connection and the customer's business ws
    await manager.broadcast(log_biz_id, {
        "event": "new_message", "customer_id": customer["id"],
        "phone": customer["phone"], "message": msg,
    })
    if log_biz_id != bid:
        await manager.broadcast(bid, {
            "event": "new_message", "customer_id": customer["id"],
            "phone": customer["phone"], "message": msg,
        })

    return {"ok": True, "message_id": msg["id"], "whatsapp_result": wa_result}


# ── Human handoff ─────────────────────────────────────────────────────────────

@router.get("/chat/handoff/pending")
def handoff_pending(user=Depends(require_business)):
    from workflows.human_handoff import get_pending_handoffs
    # Strict isolation: only this business's handoffs
    return get_pending_handoffs(user["business_id"])


def _handoff_get_customer(customer_id: int, bid: int, agent_user: dict, action: str) -> dict:
    """
    Tenant-safe customer lookup for handoff endpoints.

    Looks up the customer scoped to the agent's own business_id first.
    If not found, performs a cross-business lookup ONLY to produce a
    clearer error message and a structured security log entry — it never
    returns a customer belonging to a different business_id than the
    requesting agent. This prevents Business A from pausing/resuming AI
    or messaging Business B's customers on a shared WhatsApp number.

    Raises HTTPException(404) if no customer is found, or
    HTTPException(403) if the customer belongs to a different business.
    """
    customer = crud.get_customer_by_id(customer_id, bid)
    if customer:
        log.info(
            "HANDOFF AUTH SUCCESS  action=%s  user=%s  business_id=%s  customer_id=%s",
            action, agent_user.get("username"), bid, customer_id,
        )
        return customer

    # Not found under this business — check if it exists under another business
    # (shared-number deployments may have the same customer_id space)
    try:
        from core.db import supabase as _sb
        res = _sb.table("customers").select("*").eq("id", customer_id).limit(1).execute()
        other = res.data[0] if res.data else None
    except Exception as exc:
        log.warning("HANDOFF AUTH FAILED  action=%s  user=%s  business_id=%s  customer_id=%s  "
                     "reason=lookup_error  detail=%s",
                     action, agent_user.get("username"), bid, customer_id, exc)
        raise HTTPException(404, "Customer not found")

    if other:
        other_bid = other.get("business_id")
        log.warning(
            "HANDOFF AUTH FAILED  action=%s  user=%s  business_id=%s  customer_id=%s  "
            "reason=cross_tenant  customer_business_id=%s",
            action, agent_user.get("username"), bid, customer_id, other_bid,
        )
        raise HTTPException(403, "This conversation belongs to a different business")

    log.warning(
        "HANDOFF AUTH FAILED  action=%s  user=%s  business_id=%s  customer_id=%s  reason=not_found",
        action, agent_user.get("username"), bid, customer_id,
    )
    raise HTTPException(404, "Customer not found")


@router.post("/chat/handoff/{customer_id}/request")
async def handoff_request(customer_id: int, user=Depends(require_business)):
    bid      = user["business_id"]
    customer = _handoff_get_customer(customer_id, bid, user, action="request")

    from workflows.human_handoff import notify_dashboard, handoff_acknowledgement
    from services._ai_state import _set_human_handoff

    phone      = customer["phone"]
    actual_bid = bid  # _handoff_get_customer guarantees customer.business_id == bid
    biz        = crud.get_business_by_id(actual_bid)
    biz_name   = biz.get("name", "") if biz else ""
    agent_name = user.get("username", "Agent")

    _set_human_handoff(phone, actual_bid)

    # Generate a support ticket number for this handoff
    from services.whatsapp_catalog import generate_ticket_number
    ticket = generate_ticket_number(customer_id, actual_bid)

    # Mark this handoff as agent-initiated — restart-intent words ("menu", "hi",
    # "cart" etc.) from the customer will NOT auto-resume the AI while this flag
    # is set. Only the "▶ Resume AI" button (handoff_release) or the timeout
    # safety net can end an agent-initiated handoff.
    try:
        from services._ai_state import _write_state_data
        _write_state_data(phone, actual_bid, {
            "state": "human_handoff",
            "session": {"agent_initiated": True, "agent_name": agent_name,
                         "ticket": ticket, "handoff_reason": "Agent took over"},
        })
    except Exception as exc:
        log.debug("handoff_request: agent_initiated flag write failed: %s", exc)

    notify_dashboard(phone, actual_bid, biz_name)

    # Phase 5: notify the customer on WhatsApp that a human has joined
    try:
        token    = crud.get_decrypted_token(biz) if biz else ""
        phone_id = biz.get("whatsapp_phone_id", "") if biz else ""
        ack_msg  = handoff_acknowledgement(biz_name, ticket=ticket, reason="Agent took over")
        if token and phone_id:
            send_whatsapp(phone_id, token, phone, ack_msg)
        elif SHARED_WA_TOKEN and SHARED_PHONE_NUMBER_ID:
            send_whatsapp(SHARED_PHONE_NUMBER_ID, SHARED_WA_TOKEN, phone, ack_msg)
    except Exception as exc:
        log.warning("handoff_request: WA notification failed: %s", exc)

    log.info(
        "HANDOFF STARTED  customer_id=%s  business_id=%s  agent_name=%s  phone=%s",
        customer_id, actual_bid, agent_name, phone,
    )

    # Broadcast WebSocket event so the inbox updates in real time
    if manager:
        try:
            await manager.broadcast(actual_bid, {
                "event":       "handoff_requested",
                "customer_id": customer_id,
                "phone":       phone,
                "reason":      "manual",
            })
        except Exception as exc:
            log.debug("handoff ws broadcast failed: %s", exc)

    return {"ok": True, "customer_id": customer_id, "phone": phone, "state": "human_handoff"}


class HandoffReasonRequest(BaseModel):
    reason:   str = ""
    priority: str = "normal"


@router.post("/chat/handoff/{customer_id}/request-with-reason")
async def handoff_request_with_reason(
    customer_id: int,
    body: HandoffReasonRequest,
    user=Depends(require_business),
):
    """
    Same as /chat/handoff/{customer_id}/request, but also stores the agent's
    selected reason and priority in carts.state_data for display in the
    conversation summary and handoff queue.
    """
    bid      = user["business_id"]
    customer = _handoff_get_customer(customer_id, bid, user, action="request-with-reason")

    from workflows.human_handoff import notify_dashboard, handoff_acknowledgement
    from services._ai_state import _set_human_handoff, _write_state_data

    phone      = customer["phone"]
    actual_bid = bid  # _handoff_get_customer guarantees customer.business_id == bid
    biz        = crud.get_business_by_id(actual_bid)
    biz_name   = biz.get("name", "") if biz else ""
    agent_name = user.get("username", "Agent")

    _set_human_handoff(phone, actual_bid)

    # Generate a support ticket number for this handoff
    from services.whatsapp_catalog import generate_ticket_number
    ticket       = generate_ticket_number(customer_id, actual_bid)
    reason_label = body.reason or "Agent took over"

    # Store reason + priority alongside the handoff state, and mark this
    # handoff as agent-initiated so customer "menu"/"hi" messages don't
    # silently auto-resume the AI while the agent is handling the chat.
    try:
        _write_state_data(phone, actual_bid, {
            "state": "human_handoff",
            "session": {
                "handoff_reason":   body.reason or "",
                "handoff_priority": body.priority or "normal",
                "agent_initiated":  True,
                "agent_name":       agent_name,
                "ticket":           ticket,
            },
        })
    except Exception as exc:
        log.debug("handoff_request_with_reason: state write failed: %s", exc)

    notify_dashboard(phone, actual_bid, biz_name)

    # Notify the customer on WhatsApp
    try:
        token    = crud.get_decrypted_token(biz) if biz else ""
        phone_id = biz.get("whatsapp_phone_id", "") if biz else ""
        ack_msg  = handoff_acknowledgement(biz_name, ticket=ticket, reason=reason_label)
        if token and phone_id:
            send_whatsapp(phone_id, token, phone, ack_msg)
        elif SHARED_WA_TOKEN and SHARED_PHONE_NUMBER_ID:
            send_whatsapp(SHARED_PHONE_NUMBER_ID, SHARED_WA_TOKEN, phone, ack_msg)
    except Exception as exc:
        log.warning("handoff_request_with_reason: WA notification failed: %s", exc)

    log.info(
        "HANDOFF STARTED  customer_id=%s  business_id=%s  agent_name=%s  phone=%s  "
        "reason=%r  priority=%s",
        customer_id, actual_bid, agent_name, phone, body.reason, body.priority,
    )

    if manager:
        try:
            await manager.broadcast(actual_bid, {
                "event":       "handoff_requested",
                "customer_id": customer_id,
                "phone":       phone,
                "reason":      body.reason or "manual",
                "priority":    body.priority or "normal",
            })
        except Exception as exc:
            log.debug("handoff ws broadcast failed: %s", exc)

    return {
        "ok": True, "customer_id": customer_id, "phone": phone,
        "state": "human_handoff", "reason": body.reason, "priority": body.priority,
    }


@router.post("/chat/handoff/{customer_id}/release")
async def handoff_release(customer_id: int, user=Depends(require_business)):
    bid      = user["business_id"]
    customer = _handoff_get_customer(customer_id, bid, user, action="release")

    from workflows.human_handoff import clear_handoff_flag, ai_resumed_message
    from services._ai_state import _reset_state

    phone      = customer["phone"]
    actual_bid = bid  # _handoff_get_customer guarantees customer.business_id == bid
    biz        = crud.get_business_by_id(actual_bid)
    biz_name   = biz.get("name", "") if biz else ""

    _reset_state(phone, actual_bid)
    clear_handoff_flag(phone, actual_bid)

    try:
        token    = crud.get_decrypted_token(biz) if biz else ""
        phone_id = biz.get("whatsapp_phone_id", "") if biz else ""
        # Shared number fallback: business has no dedicated phone_id
        if token and phone_id:
            send_whatsapp(phone_id, token, phone, ai_resumed_message(biz_name))
        elif SHARED_WA_TOKEN and SHARED_PHONE_NUMBER_ID:
            send_whatsapp(SHARED_PHONE_NUMBER_ID, SHARED_WA_TOKEN, phone, ai_resumed_message(biz_name))
    except Exception as exc:
        log.warning("handoff_release: WA notification failed: %s", exc)

    log.info(
        "HANDOFF RESUMED  customer_id=%s  business_id=%s  by=%s",
        customer_id, actual_bid, user.get("username"),
    )
    return {"ok": True, "customer_id": customer_id, "phone": phone, "state": "browsing"}


@router.delete("/chat/conversations/{customer_id}")
async def delete_conversation(customer_id: int, user=Depends(require_business)):
    """
    Delete a single conversation's messages.
    Removes rows from `messages` (by customer_id) and `chat_messages` (by phone —
    chat_messages has no customer_id column, only phone + business_id).
    Does NOT touch orders, CRM, customer memory, or analytics.
    """
    bid      = user["business_id"]
    customer = crud.get_customer_by_id(customer_id, bid)
    if not customer: raise HTTPException(404, f"Customer {customer_id} not found")
    phone    = customer.get("phone", "")
    try:
        from core.db import supabase as _sb
        _sb.table("messages").delete().eq("customer_id", customer_id).eq("business_id", bid).execute()
        # chat_messages has no customer_id column — match by phone + business_id instead
        if phone:
            try:
                _sb.table("chat_messages").delete().eq("phone", phone).eq("business_id", bid).execute()
            except Exception as exc:
                log.debug("delete_conversation: chat_messages cleanup failed (non-fatal): %s", exc)
        # Reset unread count on the customer record
        try:
            _sb.table("customers").update({"unread_count": 0}).eq("id", customer_id).execute()
        except Exception:
            pass
        return {"ok": True, "customer_id": customer_id, "message": "Conversation deleted"}
    except Exception as exc:
        raise HTTPException(500, str(exc))


@router.delete("/chat/messages/{message_id}")
def delete_single_message(message_id: int, user=Depends(require_business)):
    deleted = crud.delete_message(message_id, user["business_id"])
    if not deleted: raise HTTPException(404, "Message not found or access denied")
    return {"ok": True, "deleted_id": message_id}


@router.delete("/chat/clear/{customer_id}")
def clear_conversation(customer_id: int, user=Depends(require_business)):
    customer = crud.get_customer_by_id(customer_id, user["business_id"])
    if not customer: raise HTTPException(404, "Customer not found")
    count = crud.clear_customer_messages(customer_id, user["business_id"])
    return {"ok": True, "customer_id": customer_id, "deleted": count}


# ── Cart debug ────────────────────────────────────────────────────────────────

@router.get("/cart/{phone}")
def get_cart(phone: str, user=Depends(require_business)):
    cart  = crud.get_cart(phone, user["business_id"])
    total = sum(i["qty"] * float(i["price"]) for i in cart)
    return {"phone": phone, "items": cart, "total": round(total, 2), "count": len(cart)}


@router.delete("/cart/{phone}")
def clear_cart(phone: str, user=Depends(require_business)):
    crud.clear_cart(phone, user["business_id"])
    return {"ok": True, "phone": phone, "message": "Cart cleared"}


# ── PayPal callbacks ──────────────────────────────────────────────────────────

async def _notify_customer_payment(order: dict, message: str) -> None:
    if not order: return
    biz_id = order.get("business_id")
    phone  = order.get("customer_phone", "")
    if not biz_id or not phone: return
    try:
        business = crud.get_business_by_id(biz_id)
        if not business: return
        token    = crud.get_decrypted_token(business)
        phone_id = business.get("whatsapp_phone_id")
        if token and phone_id:
            send_whatsapp(phone_id, token, phone, message)
    except Exception as exc:
        log.error("_notify_customer_payment error: %s", exc)


@router.get("/payments/paypal/success")
async def paypal_success(
    request: Request, token: str = "", PayerID: str = "", reference: str = "",
):
    from services.payment_service import capture_paypal_order
    from workflows.order_lifecycle import get_order
    log.info("paypal_success  token=%s  reference=%s  PayerID=%s", token, reference, PayerID)
    if not token: return {"status": "error", "detail": "Missing PayPal token"}
    try:
        capture = capture_paypal_order(token)
        if not capture["paid"]:
            return {"status": "capture_failed", "detail": capture.get("error"),
                    "message": "Payment not completed. Please try again."}
        ref = reference or capture.get("reference", "")
        internal_id = capture.get("internal_order_id")
        order = None
        if internal_id: order = get_order(internal_id)
        elif ref.startswith("ORDER-"):
            try: order = get_order(int(ref.split("-")[1]))
            except (ValueError, IndexError): pass
        if not order: return {"status": "error", "detail": "Order not found"}
        order_id = order["id"]
        biz_id   = order["business_id"]
        ref      = ref or f"ORDER-{order_id}"
        if order.get("payment_status") == "paid":
            return {"status": "ok", "paid": True, "reference": ref, "message": "Already confirmed."}
        crud.update_order_payment(order_id, biz_id, {"payment_status": "paid", "payment_reference": ref, "paypal_order_id": token})
        await _notify_customer_payment(order,
            f"✅ *PayPal Payment Confirmed!*\n\nThank you! Payment for *{ref}* is complete.\n\n"
            f"💰 Amount: ${capture['amount']:.2f} USD\n📦 Your order is being prepared. 🙏"
        )
        return {"status": "ok", "paid": True, "reference": ref,
                "amount": capture["amount"], "message": "Payment confirmed!"}
    except Exception as exc:
        log.exception("paypal_success error: %s", exc)
        return {"status": "error", "detail": str(exc)}


@router.get("/payments/paypal/cancel")
async def paypal_cancel(reference: str = ""):
    return {"status": "cancelled", "reference": reference,
            "message": "Payment cancelled. Your cart is still saved."}


@router.post("/payments/paypal/webhook")
async def paypal_webhook(request: Request):
    from services.payment_service import verify_paypal_webhook_signature
    from workflows.order_lifecycle import get_order, update_order_status_supabase
    import os

    WEBHOOK_ID = os.getenv("PAYPAL_WEBHOOK_ID", "").strip()
    raw_body   = await request.body()

    if WEBHOOK_ID:
        if not verify_paypal_webhook_signature(headers=dict(request.headers), raw_body=raw_body, webhook_id=WEBHOOK_ID):
            raise HTTPException(400, "Invalid PayPal webhook signature")

    try:
        event = await request.json()
    except Exception as exc:
        raise HTTPException(400, "Invalid JSON body")

    event_type = event.get("event_type", "")

    if event_type == "PAYMENT.CAPTURE.COMPLETED":
        try:
            resource        = event.get("resource", {})
            supplementary   = resource.get("supplementary_data", {})
            related_ids     = supplementary.get("related_ids", {})
            paypal_order_id = related_ids.get("order_id", "")
            if not paypal_order_id:
                for link in resource.get("links", []):
                    if "orders" in link.get("href", ""):
                        paypal_order_id = link["href"].rstrip("/").split("/")[-1]
                        break
            if not paypal_order_id:
                return {"status": "error", "detail": "Could not extract order ID"}

            amount_obj    = resource.get("amount", {})
            paid_amount   = float(amount_obj.get("value", 0))
            paid_currency = amount_obj.get("currency_code", "USD").upper()
            if paid_currency != "USD": return {"status": "error", "detail": f"Unexpected currency: {paid_currency}"}

            order = crud.get_order_by_paypal_id(paypal_order_id)
            if not order:
                custom_id = resource.get("custom_id", "")
                if custom_id and custom_id.isdigit(): order = get_order(int(custom_id))
            if not order: return {"status": "error", "detail": "Order not found"}

            order_id = order["id"]
            biz_id   = order["business_id"]
            ref      = order.get("payment_reference") or f"ORDER-{order_id}"

            if order.get("payment_status") == "paid": return {"status": "ok", "detail": "already_paid"}

            order_total = round(float(order.get("total_price") or 0), 2)
            if abs(round(paid_amount, 2) - order_total) > 0.10:
                raise HTTPException(400, f"Amount mismatch: expected ${order_total:.2f}, received ${paid_amount:.2f}")

            crud.update_order_payment(order_id, biz_id, {"payment_status": "paid", "payment_reference": ref, "paypal_order_id": paypal_order_id})
            try: update_order_status_supabase(order_id, "paid")
            except Exception as exc: log.warning("paypal_webhook: status update failed: %s", exc)

            customer_phone = order.get("customer_phone", "")
            if customer_phone:
                try:
                    from services._ai_state import _reset_state
                    _reset_state(customer_phone, biz_id)
                except Exception: pass

            await _notify_customer_payment(order,
                f"✅ *Payment Received!*\n\nYour PayPal payment of *${paid_amount:.2f} USD* has been confirmed.\n\n"
                f"📦 Order : *{ref}*\n📍 Status: *CONFIRMED*\n\nWe're preparing your order! 🙌\n\n_Thank you!_ 🙏"
            )
            return {"status": "ok", "order_id": order_id, "paid": True}
        except HTTPException: raise
        except Exception as exc:
            log.exception("paypal_webhook COMPLETED handler error: %s", exc)
            return {"status": "error", "detail": str(exc)}

    elif event_type == "PAYMENT.CAPTURE.DENIED":
        try:
            resource  = event.get("resource", {})
            custom_id = resource.get("custom_id", "")
            order     = get_order(int(custom_id)) if custom_id.isdigit() else None
            if order:
                crud.update_order_payment(order["id"], order["business_id"], {"payment_status": "payment_failed"})
                await _notify_customer_payment(order,
                    f"❌ *PayPal payment failed.*\n\nYour payment for *ORDER-{order['id']}* was declined.\n\nPlease try again or choose a different payment method.\nType *checkout* to try again."
                )
        except Exception as exc: log.error("paypal_webhook DENIED handler error: %s", exc)
        return {"status": "ok"}

    return {"status": "ok", "detail": f"event {event_type} not handled"}


@router.post("/payments/manual/confirm")
async def manual_payment_confirm(request: Request, user=Depends(require_business)):
    from workflows.order_lifecycle import get_order, update_order_status_supabase
    body      = await request.json()
    order_id  = int(body.get("order_id", 0))
    reference = body.get("reference", f"ORDER-{order_id}")
    amount    = float(body.get("amount", 0))
    if not order_id: raise HTTPException(400, "order_id is required")
    order = get_order(order_id)
    if not order: raise HTTPException(404, f"Order {order_id} not found")
    if order.get("business_id") != user["business_id"]: raise HTTPException(403, "Access denied")
    crud.update_order_payment(order_id, user["business_id"], {"payment_status": "paid", "payment_reference": reference})
    try: update_order_status_supabase(order_id, "paid")
    except Exception: pass
    # Sync customer memory — update order_count + total_spent
    try:
        from workflows.order_lifecycle import _sync_user_memory_after_order
        _phone = order.get("customer_phone", "")
        if _phone:
            _sync_user_memory_after_order(user["business_id"], _phone)
    except Exception as _me:
        log.debug("memory sync on manual confirm: %s", _me)
    await _notify_customer_payment(order,
        f"✅ *Payment Confirmed!*\n\nYour payment for *{reference}* has been manually verified.\n\n"
        f"💰 Amount: ${amount:.2f}\n📦 Your order is confirmed. Thank you! 🙏"
    )
    return {"ok": True, "order_id": order_id, "reference": reference,
            "message": "Payment confirmed and customer notified."}
class ConversationCloseRequest(BaseModel):
    note: str = ""
    send_closing_message: bool = True


@router.post("/chat/conversations/{customer_id}/close")
async def close_conversation(
    customer_id: int,
    body: ConversationCloseRequest,
    user=Depends(require_business),
):
    """
    #9 — Professional Conversation Closing.

    When an agent clicks "End Conversation":
    1. Sends a professional closing message to the customer via WhatsApp
    2. Resets AI state to browsing (ends any handoff)
    3. Stores closed_by / closed_at / close_note in state_data for audit
    4. Broadcasts websocket event so inbox refreshes

    Does NOT delete messages. Does NOT affect orders.
    """
    bid      = user["business_id"]
    agent    = user.get("username", "Agent")
    customer = crud.get_customer_by_id(customer_id, bid)
    if not customer and (SHARED_WA_TOKEN or SHARED_PHONE_NUMBER_ID):
        try:
            from core.db import supabase as _sb
            res = _sb.table("customers").select("*").eq("id", customer_id).limit(1).execute()
            customer = res.data[0] if res.data else None
        except Exception:
            pass
    if not customer:
        raise HTTPException(404, "Customer not found")

    phone      = customer["phone"]
    actual_bid = customer.get("business_id", bid)
    biz        = crud.get_business_by_id(actual_bid)
    biz_name   = biz.get("name", "") if biz else ""

    import datetime as _dt
    closed_at_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()

    # 1. Store close metadata in state_data (non-destructive — state stays browsing)
    from services._ai_state import _write_state_data, _reset_state
    _reset_state(phone, actual_bid)
    try:
        _write_state_data(phone, actual_bid, {
            "state": "browsing",
            "closed_by":   agent,
            "closed_at":   closed_at_iso,
            "close_note":  body.note or "",
        })
    except Exception as exc:
        log.debug("close_conversation: state write failed: %s", exc)

    # 2. Clear any active handoff
    try:
        from workflows.human_handoff import clear_handoff_flag
        clear_handoff_flag(phone, actual_bid)
    except Exception as exc:
        log.debug("close_conversation: clear_handoff_flag failed: %s", exc)

    # 3. Send professional closing message to customer
    if body.send_closing_message:
        closing_msg = _build_closing_message(biz_name, agent)
        try:
            token    = crud.get_decrypted_token(biz) if biz else ""
            phone_id = biz.get("whatsapp_phone_id", "") if biz else ""
            if token and phone_id:
                send_whatsapp(phone_id, token, phone, closing_msg)
            elif SHARED_WA_TOKEN and SHARED_PHONE_NUMBER_ID:
                send_whatsapp(SHARED_PHONE_NUMBER_ID, SHARED_WA_TOKEN, phone, closing_msg)
        except Exception as exc:
            log.warning("close_conversation: WA closing message failed: %s", exc)

        # Log the closing message in conversation history
        try:
            msg = crud.create_message(
                customer_id, actual_bid, closing_msg, "outgoing",
                sender_type="agent", sender_name=agent, agent_id=agent,
            )
        except Exception as exc:
            log.debug("close_conversation: message log failed: %s", exc)

    # 4. Broadcast so inbox refreshes immediately
    if manager:
        try:
            await manager.broadcast(actual_bid, {
                "event":       "conversation_closed",
                "customer_id": customer_id,
                "phone":       phone,
                "closed_by":   agent,
                "closed_at":   closed_at_iso,
            })
        except Exception as exc:
            log.debug("close_conversation: broadcast failed: %s", exc)

    log.info("CONVERSATION CLOSED  customer_id=%s  business_id=%s  by=%s",
             customer_id, actual_bid, agent)
    return {
        "ok": True,
        "customer_id": customer_id,
        "phone": phone,
        "closed_by": agent,
        "closed_at": closed_at_iso,
    }


@router.get("/chat/conversations/{customer_id}/close-info")
def close_info(customer_id: int, user=Depends(require_business)):
    """
    Returns who closed a conversation and when.
    Used by the inbox header to show the 'Closed By / Closed At' badge.
    Returns null fields if the conversation has not been closed.
    """
    bid      = user["business_id"]
    customer = crud.get_customer_by_id(customer_id, bid)
    if not customer:
        raise HTTPException(404, "Customer not found")
    phone = customer["phone"]
    try:
        from services._ai_state import _read_state_data
        sd = _read_state_data(phone, bid)
        return {
            "customer_id": customer_id,
            "closed_by":   sd.get("closed_by"),
            "closed_at":   sd.get("closed_at"),
            "close_note":  sd.get("close_note", ""),
        }
    except Exception as exc:
        log.debug("close_info: state read failed: %s", exc)
        return {"customer_id": customer_id, "closed_by": None, "closed_at": None, "close_note": ""}


def _build_closing_message(biz_name: str, agent_name: str) -> str:
    """
    Professional WhatsApp closing message sent to the customer when an
    agent ends the conversation.
    """
    return (
        f"😊 *Thank you for reaching out to {biz_name}!*\n\n"
        f"Your query has been attended to by *{agent_name}*.\n\n"
        f"We hope everything was resolved to your satisfaction.\n"
        f"Have a wonderful day! 🌟\n\n"
        f"_To start a new conversation, simply type *hi* or *menu*._"
    )

