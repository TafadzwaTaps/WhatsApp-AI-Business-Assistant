"""
routes/webhook_routes.py — WhatsApp webhook (GET verify + POST receive),
payment webhook, invoice download, and WebSocket.

Routes: GET /webhook, POST /webhook, POST /payment/webhook,
        GET /invoice/{order_id}, WS /ws/chat/{business_id}
"""

import json
import os
import logging

from fastapi import APIRouter, Request, HTTPException, Depends, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel

import crud
from core.auth import decode_token, require_business
from core.crypto import TokenDecryptionError
from services.ai import generate_reply
from services.security import verify_meta_signature
from workflows.order_lifecycle import update_order_status_supabase, get_order

log = logging.getLogger("wazibot")
router = APIRouter()

# Runtime config — injected by main.py
VERIFY_TOKEN        = os.getenv("VERIFY_TOKEN", "myverifytoken123")
WHATSAPP_APP_SECRET = ""   # set by main.py
SHARED_PHONE_NUMBER_ID = ""
SHARED_WA_TOKEN        = ""
manager              = None  # ConnectionManager — set by main.py
send_whatsapp        = None  # set by main.py
_send_direct         = None  # set by main.py
_log_event           = None  # set by main.py
INVOICES_DIR         = ""    # set by main.py


def _get_customer_state_for_log(phone: str, business_id: int) -> str:
    try:
        from core.db import supabase as _sb
        res = (
            _sb.table("carts")
            .select("state_data")
            .eq("phone", phone)
            .eq("business_id", business_id)
            .limit(1)
            .execute()
        )
        if res.data:
            return (res.data[0].get("state_data") or {}).get("state", "unknown")
    except Exception:
        pass
    return "unknown"


# ── Webhook verify ────────────────────────────────────────────────────────────

@router.get("/webhook")
async def verify_webhook(request: Request):
    p         = request.query_params
    mode      = p.get("hub.mode", "")
    token     = p.get("hub.verify_token", "")
    challenge = p.get("hub.challenge", "")
    log.info("🔔 Webhook verify  mode=%r  token_match=%s  challenge=%r",
             mode, token == VERIFY_TOKEN, challenge)
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return PlainTextResponse(content=challenge, status_code=200)
    raise HTTPException(403, "Webhook verification failed — token mismatch")


# ── Webhook receive ───────────────────────────────────────────────────────────

@router.post("/webhook")
async def receive_message(request: Request):
    raw_body   = await request.body()
    sig_header = request.headers.get("X-Hub-Signature-256", "")
    if not verify_meta_signature(raw_body, sig_header, WHATSAPP_APP_SECRET):
        log.error("webhook: INVALID signature  ip=%s",
                  request.headers.get("x-forwarded-for", "?"))
        raise HTTPException(403, "Invalid webhook signature")

    try:
        data = json.loads(raw_body)
    except Exception:
        return {"status": "ok"}

    # STEP 1: Parse Meta payload
    try:
        entry = data.get("entry", [])
        if not entry: return {"status": "ok"}
        changes = entry[0].get("changes", [])
        if not changes: return {"status": "ok"}
        value = changes[0].get("value", {})
        if "statuses" in value and "messages" not in value: return {"status": "ok"}
        if "messages" not in value: return {"status": "ok"}

        msg_obj  = value["messages"][0]
        msg_type = msg_obj.get("type", "")
        SUPPORTED_TYPES = ("text", "image", "document", "sticker", "audio")
        if msg_type not in SUPPORTED_TYPES:
            log.info("Webhook: skipping unsupported message type=%s", msg_type)
            return {"status": "ok"}

        if msg_type in ("image", "document", "sticker"):
            img_obj = msg_obj.get(msg_type, {})
            text = img_obj.get("caption", "").strip() or "[image]"
        elif msg_type == "audio":
            text = "[voice_note]"
        elif msg_type != "text":
            return {"status": "ok"}

        metadata        = value.get("metadata", {})
        phone_number_id = metadata.get("phone_number_id", "")
        customer_phone  = msg_obj.get("from", "")
        text            = msg_obj.get("text", {}).get("body", "").strip()
        wa_message_id   = msg_obj.get("id", "")

        if not phone_number_id or not customer_phone or not text:
            log.warning("Webhook: missing required fields")
            return {"status": "ok"}

        log.info("📩 STEP 1 OK  wa_id=%s  from=%s  text=%r",
                 wa_message_id, customer_phone, text)
    except Exception as exc:
        log.error("📥 STEP 1 FAIL: %s", exc)
        return {"status": "ok"}

    # STEP 1b: Deduplication
    if wa_message_id:
        try:
            if crud.message_exists(wa_message_id):
                return {"status": "ok"}
        except Exception as exc:
            log.error("⚠️  Dedup check failed: %s", exc)

    # STEP 2: Find business
    try:
        from services.tenant_router import (
            is_shared_number, resolve_business_for_shared_number, is_switch_request,
            is_businesses_help_request, build_business_picker, _category_icon,
        )
        if is_shared_number(phone_number_id):
            log.info("📋 STEP 2 — shared number  phone=%s", customer_phone)
            active_businesses = crud.get_active_businesses()

            # Help command: show picker even when already in a business
            # (does NOT clear selection — customer must say "switch" to actually change)
            from services.tenant_router import get_selected_business_id, get_selected_business_name
            if is_businesses_help_request(text) and get_selected_business_id(customer_phone):
                platform_name = get_shared_wa_phone() if callable(get_shared_wa_phone) else "WaziBot"
                current_name  = get_selected_business_name(customer_phone)
                picker        = build_business_picker(active_businesses, platform_name,
                                                       current_name=current_name)
                _send_direct(phone_number_id, SHARED_WA_TOKEN, customer_phone, picker)
                return {"status": "ok"}

            business, direct_reply = resolve_business_for_shared_number(
                phone=customer_phone, text=text, active_businesses=active_businesses,
            )
            if direct_reply and not business:
                # Picker shown (no business selected yet) — send and stop
                _send_direct(phone_number_id, SHARED_WA_TOKEN, customer_phone, direct_reply)
                try:
                    cust_any = crud.get_or_create_customer(customer_phone, 0)
                    crud.create_message(cust_any["id"], 0, text, "incoming",
                                        wa_message_id=wa_message_id)
                    crud.create_message(cust_any["id"], 0, direct_reply, "outgoing")
                except Exception:
                    pass
                return {"status": "ok"}

            if direct_reply and business:
                # Business just selected — send confirmation immediately,
                # then fall through to generate_reply() for the welcome greeting
                _send_direct(phone_number_id, SHARED_WA_TOKEN, customer_phone, direct_reply)
                try:
                    cust_confirmed = crud.get_or_create_customer(customer_phone, business["id"])
                    crud.create_message(cust_confirmed["id"], business["id"],
                                        direct_reply, "outgoing", sender_type="ai")
                except Exception:
                    pass
                # Replace the customer's text with "hi" so generate_reply sends
                # a proper welcome rather than treating "2" as an order attempt
                text = "hi"

            if not business:
                log.error("📋 STEP 2 — no business resolved for shared number")
                return {"status": "ok"}
            token = SHARED_WA_TOKEN
        else:
            business = crud.get_business_by_phone_id(phone_number_id)
            if not business:
                log.error("📋 STEP 2 FAIL — No business for phone_number_id=%s", phone_number_id)
                return {"status": "ok"}
            if not business.get("is_active", True):
                return {"status": "ok"}
            token = ""
    except Exception as exc:
        log.exception("📋 STEP 2 FAIL: %s", exc)
        return {"status": "ok"}

    # STEP 3: Decrypt token
    if not is_shared_number(phone_number_id):
        try:
            token = crud.get_decrypted_token(business)
        except TokenDecryptionError as exc:
            log.error("🔑 STEP 3 FAIL: %s", exc)
            token = ""

    # STEP 4: Get or create customer
    try:
        customer = crud.get_or_create_customer(customer_phone, business["id"])
    except Exception as exc:
        log.exception("👤 STEP 4 FAIL: %s", exc)
        return {"status": "ok"}

    # STEP 5: Save incoming message
    in_msg: dict = {}
    try:
        crud.log_message(business["id"], customer_phone, "in", text)
        in_msg = crud.create_message(
            customer["id"], business["id"], text, "incoming",
            wa_message_id=wa_message_id,
        )
    except Exception as exc:
        err_str = str(exc)
        if "wa_message_id" in err_str or "unique" in err_str.lower():
            log.warning("💾 STEP 5 — Duplicate at INSERT  wa_id=%s", wa_message_id)
            return {"status": "ok"}
        log.exception("💾 STEP 5 FAIL: %s", exc)

    try:
        await manager.broadcast(business["id"], {
            "event": "new_message", "customer_id": customer["id"],
            "phone": customer_phone, "message": in_msg,
        })
    except Exception:
        pass

    # STEP 6: Generate AI reply
    try:
        products = crud.get_products(business["id"])
        message_has_image = (msg_type in ("image", "document", "sticker"))
        is_voice_note = (msg_type == "audio")

        if is_voice_note and text == "[voice_note]":
            reply = (
                "🎤 I heard your voice note! Unfortunately I can't process audio yet.\n\n"
                "Could you type your order instead? Type *menu* to get started! 😊"
            )
            try:
                out_msg = crud.create_message(
                    customer["id"], business["id"], reply, "outgoing", sender_type="ai",
                )
            except Exception as exc:
                log.warning("STEP 7 voice-note-reply failed: %s", exc)
            if token:
                send_whatsapp(phone_number_id, token, customer_phone, reply)
            return {"status": "ok"}

        business_phone_id = business.get("whatsapp_phone_id", "")
        msg_from          = msg_obj.get("from", "")
        is_from_agent     = bool(business_phone_id and msg_from and msg_from == business_phone_id)

        # Build per-business config so AI can tailor its copy
        biz_config = {
            "welcome_message":       business.get("welcome_message", "")     or "",
            "currency":              business.get("currency", "USD")          or "USD",
            "currency_symbol":       business.get("currency_symbol", "$")     or "$",
            "category":              business.get("category", "")             or "",
            "menu_header":           business.get("menu_header", "")          or "",
            # Service business mode
            "is_service_business":   bool(business.get("is_service_business", False)),
            "default_slot_mins":     int(business.get("default_slot_mins", 60) or 60),
        }

        reply = generate_reply(
            message=text,
            phone=customer_phone,
            business_id=business["id"],
            business_name=business["name"],
            products=products,
            message_has_image=message_has_image,
            message_is_from_agent=is_from_agent,
            business_config=biz_config,
        )
        _log_event(
            "ai.request" if reply else "ai.suppressed",
            phone=customer_phone, biz=business["id"],
            msg_len=len(text), reply_len=len(reply),
        )
    except Exception as exc:
        log.exception("📦 STEP 6 FAIL: %s", exc)
        reply = (
            f"Hi! 👋 Thanks for contacting *{business['name']}*. "
            f"We received your message and will get back to you shortly."
        )

    # STEP 7: Save outgoing
    out_msg: dict = {}
    try:
        crud.log_message(business["id"], customer_phone, "out", reply)
        out_msg = crud.create_message(
            customer["id"], business["id"], reply, "outgoing", sender_type="ai",
        )
    except Exception as exc:
        log.exception("💾 STEP 7 FAIL: %s", exc)

    try:
        await manager.broadcast(business["id"], {
            "event": "new_message", "customer_id": customer["id"],
            "phone": customer_phone, "message": out_msg,
        })
    except Exception:
        pass

    # STEP 8: Send via WhatsApp API
    if not reply:
        log.info("📤 STEP 8 SKIP — empty reply  phone=%s  state=%s",
                 customer_phone, _get_customer_state_for_log(customer_phone, business["id"]))
        return {"status": "ok"}

    if token:
        result = send_whatsapp(phone_number_id, token, customer_phone, reply)
        if "error" in result:
            _log_event("wa.failed", phone=customer_phone, biz=business["id"],
                       error=result["error"])
        else:
            msg_id = (result.get("messages") or [{}])[0].get("id", "?")
            _log_event("wa.sent", phone=customer_phone, biz=business["id"],
                       msg_id=msg_id, reply_len=len(reply))
    else:
        log.error("📤 STEP 8 FAIL — No token for '%s'", business["name"])

    return {"status": "ok"}


# ── Payment webhook ───────────────────────────────────────────────────────────

class PaymentConfirmRequest(BaseModel):
    reference: str
    amount: float


@router.post("/payment/webhook")
async def payment_webhook(data: PaymentConfirmRequest):
    reference = (data.reference or "").strip().upper()
    if not reference.startswith("ORDER-"):
        raise HTTPException(400, f"Invalid reference. Expected ORDER-{{id}}, got: {reference}")
    try:
        order_id = int(reference.split("-")[1])
    except (IndexError, ValueError):
        raise HTTPException(400, f"Cannot parse order ID: {reference}")

    order = get_order(order_id)
    if not order:
        raise HTTPException(404, f"Order {order_id} not found")

    order_total = float(order.get("total_price") or 0)
    if round(float(data.amount), 2) != round(order_total, 2):
        raise HTTPException(400,
            f"Amount mismatch: expected ${order_total:.2f}, received ${data.amount:.2f}")

    if order.get("payment_status") == "paid":
        return {"success": True, "message": f"Order {order_id} already paid.", "order_id": order_id}

    biz_id = order.get("business_id")
    crud.update_order_payment(order_id, biz_id, {
        "payment_status":    "paid",
        "payment_reference": reference,
    })
    try:
        update_order_status_supabase(order_id, "paid")
    except Exception:
        pass

    phone = order.get("customer_phone", "")
    if phone and biz_id:
        try:
            business = crud.get_business_by_id(biz_id)
            if business:
                token    = crud.get_decrypted_token(business)
                phone_id = business.get("whatsapp_phone_id")
                if token and phone_id:
                    send_whatsapp(phone_id, token, phone,
                        f"✅ *Payment Confirmed!*\n\n"
                        f"Thank you! Your payment for *{reference}* has been verified.\n\n"
                        f"💰 Amount: ${order_total:.2f}\n"
                        f"📦 Your order is now being prepared. Thank you! 🙏"
                    )
        except Exception as exc:
            log.exception("payment_webhook notify error: %s", exc)

    return {"success": True, "order_id": order_id, "reference": reference,
            "message": f"Payment confirmed for {reference}"}


# ── Invoice download ──────────────────────────────────────────────────────────

@router.get("/invoice/{order_id}")
def download_invoice(order_id: int, user=Depends(require_business)):
    order = crud.get_order_by_id(order_id, user["business_id"])
    if not order:
        raise HTTPException(404, "Order not found")

    pdf_path = os.path.join(INVOICES_DIR, f"invoice_{order_id}.pdf")
    if not os.path.exists(pdf_path):
        try:
            business = crud.get_business_by_id(user["business_id"])
            order["business_name"] = business.get("name", "") if business else ""
            from services.pdf_invoice import generate_pdf_invoice
            pdf_path = generate_pdf_invoice(order)
        except Exception as exc:
            log.exception("download_invoice: PDF generation failed — %s", exc)
            raise HTTPException(500, "Failed to generate invoice PDF")

    return FileResponse(path=pdf_path, media_type="application/pdf",
                        filename=f"invoice_{order_id}.pdf")


# ── WebSocket ─────────────────────────────────────────────────────────────────

@router.websocket("/ws/chat/{business_id}")
async def websocket_chat(websocket: WebSocket, business_id: int):
    token_param = websocket.query_params.get("token", "")
    try:
        payload = decode_token(token_param)
        if payload.get("business_id") != business_id and payload.get("role") != "superadmin":
            await websocket.close(code=4001)
            return
    except Exception:
        await websocket.close(code=4001)
        return

    await manager.connect(websocket, business_id)
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw) if raw else {}
                if msg.get("type") == "ping":
                    await websocket.send_text(json.dumps({"type": "pong"}))
            except Exception:
                pass
    except WebSocketDisconnect:
        manager.disconnect(websocket, business_id)
