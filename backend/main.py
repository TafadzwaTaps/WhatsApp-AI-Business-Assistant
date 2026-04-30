"""
main.py — WaziBot SaaS API (Supabase edition)

All DB access goes through crud.py which uses the supabase-py client.
No SQLAlchemy, no database.py, no Session dependency.
"""

import os
import json
import logging
from typing import Optional
from datetime import datetime

import requests as http_requests
from dotenv import load_dotenv
from fastapi import (
    FastAPI, Request, Depends, HTTPException,
    WebSocket, WebSocketDisconnect, Query,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, validator

import crud
from crypto import TokenDecryptionError
from ai import generate_reply
from auth import (
    verify_password,
    create_access_token, create_refresh_token,
    decode_token,
    get_current_user, require_superadmin, require_business,
    SUPER_ADMIN_USERNAME, SUPER_ADMIN_PASSWORD,
)
from order_lifecycle import (
    create_order_supabase,
    update_order_status_supabase,
    get_order,
    VALID_STATUSES,
)
from invoice import generate_invoice_text
from payments import confirm_payment

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("wazibot")

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "myverifytoken123")

# ── Static files ───────────────────────────────────────────────────────────────
_BASE = os.path.dirname(os.path.abspath(__file__))
_STATIC_CANDIDATES = [
    os.path.join(_BASE, "static"),
    os.path.join(_BASE, "..", "static"),
    os.path.join(_BASE, "..", "..", "static"),
]
STATIC_DIR = next(
    (os.path.abspath(p) for p in _STATIC_CANDIDATES if os.path.isdir(p)),
    os.path.abspath(os.path.join(_BASE, "static")),
)
os.makedirs(STATIC_DIR, exist_ok=True)
log.info("📁 Static dir: %s", STATIC_DIR)

# ── Invoice dir ────────────────────────────────────────────────────────────────
INVOICES_DIR = os.path.join(_BASE, "invoices")
os.makedirs(INVOICES_DIR, exist_ok=True)

# ── App ────────────────────────────────────────────────────────────────────────
app = FastAPI(title="WaziBot API", docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _html(name: str) -> FileResponse:
    path = os.path.join(STATIC_DIR, name)
    if not os.path.exists(path):
        raise HTTPException(404, detail=f"{name} not found")
    return FileResponse(path)


@app.get("/")
def landing():    return _html("landing.html")

@app.get("/dashboard")
def dashboard():  return _html("dashboard.html")

@app.get("/inbox")
def inbox():      return _html("inbox.html")

@app.get("/signup")
def signup_page(): return _html("signup.html")


# ── WebSocket manager ──────────────────────────────────────────────────────────
class ConnectionManager:
    def __init__(self):
        self._conns: dict[int, list[WebSocket]] = {}

    async def connect(self, ws: WebSocket, business_id: int):
        await ws.accept()
        self._conns.setdefault(business_id, []).append(ws)

    def disconnect(self, ws: WebSocket, business_id: int):
        lst = self._conns.get(business_id, [])
        if ws in lst:
            lst.remove(ws)

    async def broadcast(self, business_id: int, payload: dict):
        dead = []
        for ws in list(self._conns.get(business_id, [])):
            try:
                await ws.send_text(json.dumps(payload, default=str))
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws, business_id)


manager = ConnectionManager()


# ── WhatsApp sender ────────────────────────────────────────────────────────────
def send_whatsapp(phone_number_id: str, token: str, to: str, message: str) -> dict:
    if not phone_number_id or not token:
        missing = [k for k, v in {"phone_number_id": phone_number_id, "token": token}.items() if not v]
        log.error("send_whatsapp: ABORTED — missing %s", missing)
        return {"error": f"missing credentials: {missing}"}

    to = to.replace("whatsapp:", "").strip()
    url = f"https://graph.facebook.com/v18.0/{phone_number_id}/messages"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    body = {
        "messaging_product": "whatsapp",
        "to":   to,
        "type": "text",
        "text": {"body": message},
    }
    log.info("📤 WhatsApp → to=%s  phone_id=%s  token_tail=…%s", to, phone_number_id, token[-6:])
    try:
        resp = http_requests.post(url, headers=headers, json=body, timeout=10)
        result = resp.json()
        if resp.status_code == 200:
            msg_id = (result.get("messages") or [{}])[0].get("id", "?")
            log.info("✅ WhatsApp OK  msg_id=%s  to=%s", msg_id, to)
        else:
            err = result.get("error", {})
            log.error("❌ WhatsApp API %d  code=%s  msg=%s", resp.status_code, err.get("code"), err.get("message"))
        return result
    except Exception as exc:
        log.exception("send_whatsapp exception: %s", exc)
        return {"error": str(exc)}


# ── JWT token pair helper ──────────────────────────────────────────────────────
def _token_pair(sub: str, role: str, business_id: int | None = None) -> dict:
    data: dict = {"sub": sub, "role": role}
    if business_id is not None:
        data["business_id"] = business_id
    return {
        "access_token":  create_access_token(data),
        "refresh_token": create_refresh_token(data),
        "token_type":    "bearer",
    }


# ─────────────────────────────────────────────────────────────────────────────
# AUTH
# ─────────────────────────────────────────────────────────────────────────────

class SignupRequest(BaseModel):
    business_name: str
    username: str
    password: str
    whatsapp_phone_id: str = ""
    whatsapp_token: str = ""

    @validator("username")
    def username_valid(cls, v):
        v = v.strip().lower()
        if len(v) < 3: raise ValueError("Username must be ≥ 3 characters")
        if " " in v:   raise ValueError("Username cannot contain spaces")
        return v

    @validator("password")
    def password_valid(cls, v):
        if len(v) < 6: raise ValueError("Password must be ≥ 6 characters")
        return v

    @validator("business_name")
    def bizname_valid(cls, v):
        v = v.strip()
        if len(v) < 2: raise ValueError("Business name too short")
        return v


@app.post("/auth/signup")
def signup(data: SignupRequest):
    if data.username == SUPER_ADMIN_USERNAME.lower():
        raise HTTPException(400, "Username not available")
    if crud.get_business_by_username(data.username):
        raise HTTPException(400, "Username already taken")

    phone_id = data.whatsapp_phone_id.strip() or None
    if phone_id and crud.get_business_by_phone_id(phone_id):
        raise HTTPException(
            400,
            "That WhatsApp Phone Number ID is already registered. "
            "Check your Meta Developer Portal or update your existing account in Settings.",
        )

    class _Payload:
        name              = data.business_name
        owner_username    = data.username
        owner_password    = data.password
        whatsapp_phone_id = phone_id
        whatsapp_token    = data.whatsapp_token.strip() or None

    biz = crud.create_business(_Payload())
    log.info("🆕 Signup: %s (@%s)", biz["name"], biz["owner_username"])
    return {
        **_token_pair(biz["owner_username"], "business", biz["id"]),
        "role":          "business",
        "business_name": biz["name"],
        "business_id":   biz["id"],
    }


class LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/auth/login")
def login(data: LoginRequest):
    username = data.username.strip().lower()

    if username == SUPER_ADMIN_USERNAME.lower():
        if not verify_password(data.password, SUPER_ADMIN_PASSWORD):
            raise HTTPException(401, "Invalid credentials")
        return {**_token_pair(SUPER_ADMIN_USERNAME, "superadmin"), "role": "superadmin"}

    biz = crud.get_business_by_username(username)
    if not biz or not verify_password(data.password, biz["owner_password"]):
        raise HTTPException(401, "Invalid credentials")
    if not biz.get("is_active", True):
        raise HTTPException(403, "Account suspended. Contact support.")

    log.info("🔑 Login: %s", biz["owner_username"])
    return {
        **_token_pair(biz["owner_username"], "business", biz["id"]),
        "role":          "business",
        "business_name": biz["name"],
        "business_id":   biz["id"],
    }


class RefreshRequest(BaseModel):
    refresh_token: str


@app.post("/auth/refresh")
def refresh_token_endpoint(data: RefreshRequest):
    try:
        payload = decode_token(data.refresh_token)
    except HTTPException:
        raise HTTPException(401, "Refresh token invalid or expired. Please log in again.")

    if payload.get("type") != "refresh":
        raise HTTPException(401, "Not a refresh token.")

    sub         = payload.get("sub", "")
    role        = payload.get("role", "business")
    business_id = payload.get("business_id")

    if role == "business":
        biz = crud.get_business_by_username(sub)
        if not biz or not biz.get("is_active", True):
            raise HTTPException(401, "Account not found or suspended.")
        business_id = biz["id"]

    log.info("🔄 Token refreshed for: %s", sub)
    return {
        **_token_pair(sub, role, business_id),
        "role": role,
        **({} if business_id is None else {"business_id": business_id}),
    }


# ─────────────────────────────────────────────────────────────────────────────
# DEBUG
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/debug/env")
def debug_env():
    fernet_key = os.getenv("FERNET_KEY", "")
    secret_key = os.getenv("SECRET_KEY", "")
    supa_url   = os.getenv("SUPABASE_URL", "")
    supa_key   = os.getenv("SUPABASE_KEY", "")
    return {
        "FERNET_KEY":   f"{fernet_key[:8]}…({len(fernet_key)} chars)" if fernet_key else "❌ NOT SET",
        "SECRET_KEY":   f"{secret_key[:4]}…({len(secret_key)} chars)" if secret_key else "❌ NOT SET",
        "SUPABASE_URL": f"{supa_url[:30]}…" if supa_url else "❌ NOT SET",
        "SUPABASE_KEY": f"{supa_key[:8]}…({len(supa_key)} chars)" if supa_key else "❌ NOT SET",
        "VERIFY_TOKEN": "✅ set" if os.getenv("VERIFY_TOKEN") else "⚠ using default",
    }


@app.get("/debug/token")
def debug_token():
    from crypto import encrypt_token, decrypt_token
    test = "wazibot-test-12345"
    try:
        ct = encrypt_token(test)
        pt = decrypt_token(ct)
        ok = pt == test
        return {"ok": ok, "ciphertext_prefix": ct[:12] + "…", "round_trip": "✅ PASS" if ok else "❌ FAIL"}
    except Exception as exc:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})


@app.get("/debug/webhook")
def debug_webhook(user=Depends(require_business)):
    bid      = user["business_id"]
    business = crud.get_business_by_id(bid)
    if not business:
        return {"ok": False, "step": 1, "error": "Business not found"}

    steps = {
        "business_id":        bid,
        "business_name":      business.get("name"),
        "whatsapp_phone_id":  business.get("whatsapp_phone_id"),
        "has_phone_id":       bool(business.get("whatsapp_phone_id")),
        "has_token_stored":   bool(business.get("whatsapp_token")),
    }

    token = ""
    try:
        token = crud.get_decrypted_token(business)
        steps["token_decrypts"] = bool(token)
        steps["token_tail"]     = "…" + token[-6:] if token else "empty"
    except Exception as exc:
        steps["token_decrypts"] = False
        steps["token_error"]    = str(exc)

    try:
        products = crud.get_products(bid)
        steps["products_count"] = len(products)
        steps["products"]       = [{"name": p["name"], "price": p["price"]} for p in products]
    except Exception as exc:
        steps["products_count"] = 0
        steps["products_error"] = str(exc)

    if token and business.get("whatsapp_phone_id"):
        try:
            resp = http_requests.get(
                f"https://graph.facebook.com/v18.0/{business['whatsapp_phone_id']}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=5,
            )
            steps["whatsapp_api_status"] = resp.status_code
            steps["whatsapp_api_ok"]     = resp.status_code == 200
            if resp.status_code != 200:
                steps["whatsapp_api_error"] = resp.json().get("error", {}).get("message")
        except Exception as exc:
            steps["whatsapp_api_ok"]    = False
            steps["whatsapp_api_error"] = str(exc)
    else:
        steps["whatsapp_api_ok"]   = False
        steps["whatsapp_api_skip"] = "missing phone_id or token"

    all_ok = (
        steps["has_phone_id"]
        and steps.get("token_decrypts")
        and steps.get("products_count", 0) > 0
        and steps.get("whatsapp_api_ok")
    )
    steps["overall"] = "✅ ALL GOOD — webhook should work" if all_ok else "❌ FIX ISSUES ABOVE"
    return steps


# ─────────────────────────────────────────────────────────────────────────────
# WEBHOOK
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/webhook")
async def verify_webhook(request: Request):
    from fastapi.responses import PlainTextResponse
    p = request.query_params
    mode      = p.get("hub.mode", "")
    token     = p.get("hub.verify_token", "")
    challenge = p.get("hub.challenge", "")
    log.info("🔔 Webhook verify  mode=%r  token_match=%s  challenge=%r",
             mode, token == VERIFY_TOKEN, challenge)
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return PlainTextResponse(content=challenge, status_code=200)
    raise HTTPException(403, "Webhook verification failed — token mismatch")


@app.post("/webhook")
async def receive_message(request: Request):
    data = await request.json()

    # ── STEP 1: Parse Meta payload ────────────────────────────────────────
    try:
        entry = data.get("entry", [])
        if not entry:
            return {"status": "ok"}

        changes = entry[0].get("changes", [])
        if not changes:
            return {"status": "ok"}

        value = changes[0].get("value", {})

        if "statuses" in value and "messages" not in value:
            return {"status": "ok"}

        if "messages" not in value:
            return {"status": "ok"}

        msg_obj  = value["messages"][0]
        msg_type = msg_obj.get("type", "")

        if msg_type != "text":
            log.info("Webhook: skipping non-text message  type=%s", msg_type)
            return {"status": "ok"}

        metadata        = value.get("metadata", {})
        phone_number_id = metadata.get("phone_number_id", "")
        customer_phone  = msg_obj.get("from", "")
        text            = msg_obj.get("text", {}).get("body", "").strip()
        wa_message_id   = msg_obj.get("id", "")

        if not phone_number_id or not customer_phone or not text:
            log.warning("Webhook: missing required fields")
            return {"status": "ok"}

        log.info("📩 STEP 1 OK  wa_id=%s  from=%s  text=%r", wa_message_id, customer_phone, text)

    except Exception as exc:
        log.error("📥 STEP 1 FAIL — parse error: %s", exc)
        return {"status": "ok"}

    # ── STEP 1b: Deduplication ─────────────────────────────────────────────
    if wa_message_id:
        try:
            if crud.message_exists(wa_message_id):
                return {"status": "ok"}
        except Exception as exc:
            log.error("⚠️  Dedup check failed (will process anyway): %s", exc)

    # ── STEP 2: Find business ─────────────────────────────────────────────
    try:
        business = crud.get_business_by_phone_id(phone_number_id)
        if not business:
            log.error("📋 STEP 2 FAIL — No business for phone_number_id=%s", phone_number_id)
            return {"status": "ok"}
        if not business.get("is_active", True):
            return {"status": "ok"}
        log.info("📋 STEP 2 OK  id=%s  name=%s", business["id"], business["name"])
    except Exception as exc:
        log.exception("📋 STEP 2 FAIL: %s", exc)
        return {"status": "ok"}

    # ── STEP 3: Decrypt token ─────────────────────────────────────────────
    token = ""
    try:
        token = crud.get_decrypted_token(business)
        if token:
            log.info("🔑 STEP 3 OK  tail=…%s", token[-6:])
        else:
            log.warning("🔑 STEP 3 — No token for %s", business["name"])
    except TokenDecryptionError as exc:
        log.error("🔑 STEP 3 FAIL — %s: %s", business["name"], exc)

    # ── STEP 4: Get or create customer ────────────────────────────────────
    try:
        customer = crud.get_or_create_customer(customer_phone, business["id"])
        log.info("👤 STEP 4 OK  id=%s  phone=%s", customer["id"], customer["phone"])
    except Exception as exc:
        log.exception("👤 STEP 4 FAIL: %s", exc)
        return {"status": "ok"}

    # ── STEP 5: Save incoming message ────────────────────────────────────
    in_msg: dict = {}
    try:
        crud.log_message(business["id"], customer_phone, "in", text)
        in_msg = crud.create_message(
            customer["id"], business["id"], text, "incoming",
            wa_message_id=wa_message_id,
        )
        log.info("💾 STEP 5 OK  id=%s  wa_id=%s", in_msg.get("id", "?"), wa_message_id)
    except Exception as exc:
        err_str = str(exc)
        if "wa_message_id" in err_str or "unique" in err_str.lower():
            log.warning("💾 STEP 5 — Duplicate at INSERT level  wa_id=%s — skipping", wa_message_id)
            return {"status": "ok"}
        log.exception("💾 STEP 5 FAIL: %s", exc)

    try:
        await manager.broadcast(business["id"], {
            "event": "new_message", "customer_id": customer["id"],
            "phone": customer_phone, "message": in_msg,
        })
    except Exception:
        pass

    # ── STEP 6: Fetch products + generate AI reply ────────────────────────
    try:
        products = crud.get_products(business["id"])
        log.info("📦 STEP 6 — Products fetched  count=%d", len(products))

        reply = generate_reply(
            message=text,
            phone=customer_phone,
            business_id=business["id"],
            business_name=business["name"],
            products=products,
        )
        log.info("🤖 STEP 6 — AI reply generated  len=%d", len(reply))

    except Exception as exc:
        log.exception("📦 STEP 6 FAIL: %s", exc)
        reply = (
            f"Hi! 👋 Thanks for contacting *{business['name']}*. "
            f"We received your message and will get back to you shortly."
        )

    # ── STEP 7: Save outgoing message ─────────────────────────────────────
    out_msg: dict = {}
    try:
        crud.log_message(business["id"], customer_phone, "out", reply)
        out_msg = crud.create_message(customer["id"], business["id"], reply, "outgoing")
        log.info("💾 STEP 7 OK  id=%s", out_msg.get("id", "?") if out_msg else "?")
    except Exception as exc:
        log.exception("💾 STEP 7 FAIL: %s", exc)

    try:
        await manager.broadcast(business["id"], {
            "event": "new_message", "customer_id": customer["id"],
            "phone": customer_phone, "message": out_msg,
        })
    except Exception:
        pass

    # ── STEP 8: Send via WhatsApp API ─────────────────────────────────────
    if token:
        result = send_whatsapp(phone_number_id, token, customer_phone, reply)
        if "error" in result:
            log.error("📤 STEP 8 FAIL — WhatsApp error: %s", result["error"])
        else:
            log.info("📤 STEP 8 OK  msg_id=%s",
                     (result.get("messages") or [{}])[0].get("id", "?"))
    else:
        log.error(
            "📤 STEP 8 FAIL — No token for '%s' (id=%s). "
            "Message saved but NOT delivered.",
            business["name"], business["id"],
        )

    return {"status": "ok"}


# ─────────────────────────────────────────────────────────────────────────────
# PAYMENT WEBHOOK
# ─────────────────────────────────────────────────────────────────────────────

class PaymentWebhookRequest(BaseModel):
    reference: str   # e.g. ORDER-12
    amount: float


@app.post("/payment/webhook")
async def payment_webhook(data: PaymentWebhookRequest):
    """
    Receive a payment confirmation from a payment gateway or manual trigger.

    Payload: { "reference": "ORDER-12", "amount": 10.00 }

    Steps:
      1. Confirm payment (validate amount, mark order paid)
      2. Look up the business that owns the order
      3. Send a WhatsApp confirmation message to the customer
    """
    result = confirm_payment(reference=data.reference, amount=data.amount)

    if not result["success"]:
        log.warning("payment_webhook: failed — %s", result.get("error"))
        raise HTTPException(400, result.get("error", "Payment confirmation failed"))

    order     = result["order"]
    order_id  = result["order_id"]
    phone     = order.get("customer_phone", "")
    biz_id    = order.get("business_id")

    # Send WhatsApp confirmation to customer
    if phone and biz_id:
        try:
            business = crud.get_business_by_id(biz_id)
            if business:
                token       = crud.get_decrypted_token(business)
                phone_id    = business.get("whatsapp_phone_id")
                biz_name    = business.get("name", "")

                if token and phone_id:
                    confirmation_msg = (
                        f"✅ *Payment Received!*\n\n"
                        f"Thank you! Your payment for *ORDER-{order_id}* has been confirmed.\n\n"
                        f"💰 Amount: ${float(order.get('total_price', 0)):.2f}\n"
                        f"📦 Status: *CONFIRMED*\n\n"
                        f"Your order is now being processed. Thank you for shopping with *{biz_name}*! 🙏"
                    )
                    send_whatsapp(phone_id, token, phone, confirmation_msg)
                    log.info("payment_webhook: confirmation sent  order=%s  phone=%s", order_id, phone)
        except Exception as exc:
            log.exception("payment_webhook: WhatsApp notification failed — %s", exc)

    log.info("payment_webhook: ✅ success  order=%s  amount=%.2f", order_id, data.amount)
    return {
        "success":  True,
        "message":  result["message"],
        "order_id": order_id,
        "order":    order,
    }


# ─────────────────────────────────────────────────────────────────────────────
# INVOICE DOWNLOAD
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/invoice/{order_id}")
def download_invoice(order_id: int, user=Depends(require_business)):
    """
    Generate (or return cached) PDF invoice for an order.
    Returns the PDF file as a download.
    """
    order = crud.get_order_by_id(order_id, user["business_id"])
    if not order:
        raise HTTPException(404, "Order not found")

    pdf_path = os.path.join(INVOICES_DIR, f"invoice_{order_id}.pdf")

    # Regenerate if missing
    if not os.path.exists(pdf_path):
        try:
            business = crud.get_business_by_id(user["business_id"])
            order["business_name"] = business.get("name", "") if business else ""
            from pdf_invoice import generate_pdf_invoice
            pdf_path = generate_pdf_invoice(order)
        except Exception as exc:
            log.exception("download_invoice: PDF generation failed — %s", exc)
            raise HTTPException(500, "Failed to generate invoice PDF")

    return FileResponse(
        path=pdf_path,
        media_type="application/pdf",
        filename=f"invoice_{order_id}.pdf",
    )


# ─────────────────────────────────────────────────────────────────────────────
# WEBSOCKET
# ─────────────────────────────────────────────────────────────────────────────

@app.websocket("/ws/chat/{business_id}")
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


# ─────────────────────────────────────────────────────────────────────────────
# SUPERADMIN
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/admin/businesses")
def list_businesses(_=Depends(require_superadmin)):
    return crud.get_all_businesses()


@app.patch("/admin/businesses/{business_id}")
def admin_update_business(business_id: int, data: dict, _=Depends(require_superadmin)):
    class _D:
        def dict(self, **_):
            return data
    b = crud.update_business(business_id, _D())
    if not b:
        raise HTTPException(404, "Business not found")
    return b


@app.delete("/admin/businesses/{business_id}")
def admin_delete_business(business_id: int, _=Depends(require_superadmin)):
    b = crud.delete_business(business_id)
    if not b:
        raise HTTPException(404, "Business not found")
    return {"deleted": business_id}


@app.get("/admin/stats")
def admin_stats(_=Depends(require_superadmin)):
    return crud.get_admin_stats()


# ─────────────────────────────────────────────────────────────────────────────
# ANALYTICS DASHBOARD
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/analytics/{business_id}")
def get_analytics(business_id: int, user=Depends(get_current_user)):
    """
    Full analytics payload for the business dashboard.
    Returns: total_revenue, total_orders, total_customers,
             orders_per_day, revenue_per_day, top_products,
             orders_by_status, customers_per_day, recent_orders.
    """
    # Allow superadmin to view any business, normal users only their own
    if user["role"] != "superadmin" and user.get("business_id") != business_id:
        raise HTTPException(403, "Access denied")

    business = crud.get_business_by_id(business_id)
    if not business:
        raise HTTPException(404, "Business not found")

    try:
        stats = crud.get_dashboard_stats(business_id)
        stats["business_name"] = business.get("name", "")
        return stats
    except Exception as exc:
        log.exception("get_analytics error: %s", exc)
        raise HTTPException(500, "Failed to load analytics")


# ─────────────────────────────────────────────────────────────────────────────
# BUSINESS PROFILE
# ─────────────────────────────────────────────────────────────────────────────

class BusinessUpdate(BaseModel):
    name:              Optional[str]  = None
    whatsapp_phone_id: Optional[str]  = None
    whatsapp_token:    Optional[str]  = None
    is_active:         Optional[bool] = None


@app.get("/me")
def get_me(user=Depends(require_business)):
    b = crud.get_business_by_id(user["business_id"])
    if not b:
        raise HTTPException(404, "Not found")
    b.pop("owner_password", None)
    b.pop("whatsapp_token", None)
    return b


@app.patch("/me")
def update_me(data: BusinessUpdate, user=Depends(require_business)):
    safe = data.dict(exclude_none=True)
    safe.pop("is_active", None)

    new_phone_id = safe.get("whatsapp_phone_id")
    if new_phone_id:
        existing = crud.get_business_by_phone_id(new_phone_id)
        if existing and existing["id"] != user["business_id"]:
            raise HTTPException(400, "That WhatsApp Phone Number ID is already registered.")

    class _D:
        def dict(self, **_):
            return safe

    b = crud.update_business(user["business_id"], _D())
    if not b:
        raise HTTPException(404, "Not found")
    b.pop("owner_password", None)
    b.pop("whatsapp_token", None)
    return b


@app.get("/me/test-whatsapp")
def test_whatsapp_connection(user=Depends(require_business)):
    b = crud.get_business_by_id(user["business_id"])
    if not b or not b.get("whatsapp_phone_id"):
        return {"ok": False, "reason": "No Phone Number ID saved"}
    try:
        token = crud.get_decrypted_token(b)
    except TokenDecryptionError as exc:
        return {"ok": False, "reason": f"Token decryption failed — {exc}"}
    if not token:
        return {"ok": False, "reason": "No access token saved"}
    try:
        resp = http_requests.get(
            f"https://graph.facebook.com/v18.0/{b['whatsapp_phone_id']}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=5,
        )
        if resp.status_code == 200:
            return {"ok": True, "reason": "Connected to Meta API ✅"}
        return {"ok": False, "reason": resp.json().get("error", {}).get("message", "Unknown")}
    except Exception as exc:
        return {"ok": False, "reason": str(exc)}


# ─────────────────────────────────────────────────────────────────────────────
# PRODUCTS
# ─────────────────────────────────────────────────────────────────────────────

class ProductCreate(BaseModel):
    name:                str
    price:               float
    image_url:           Optional[str] = None
    stock:               int           = 0
    low_stock_threshold: int           = 5


@app.get("/products")
def get_products(user=Depends(require_business)):
    return crud.get_products(user["business_id"])


@app.post("/products", status_code=201)
def create_product(product: ProductCreate, user=Depends(require_business)):
    log.info(
        "📦 create_product  business_id=%s  name=%r  price=%s  stock=%s",
        user["business_id"], product.name, product.price, product.stock,
    )
    return crud.create_product(user["business_id"], product)


@app.delete("/products/{product_id}")
def delete_product(product_id: int, user=Depends(require_business)):
    p = crud.delete_product(product_id, user["business_id"])
    if not p:
        raise HTTPException(404, "Product not found")
    return {"deleted": product_id}


# ─────────────────────────────────────────────────────────────────────────────
# ORDERS
# ─────────────────────────────────────────────────────────────────────────────

class OrderCreateRequest(BaseModel):
    customer_phone: str
    items: list   # [{name: str, qty: int, price: float}, ...]


class OrderStatusUpdate(BaseModel):
    status: str

    @validator("status")
    def status_valid(cls, v):
        if v not in VALID_STATUSES:
            raise ValueError(f"status must be one of {VALID_STATUSES}")
        return v


@app.get("/orders")
def get_orders(user=Depends(require_business)):
    return crud.get_orders(user["business_id"])


@app.post("/orders", status_code=201)
def create_order_api(data: OrderCreateRequest, user=Depends(require_business)):
    """
    Create an order for the authenticated business.
    Reduces stock automatically.
    Returns the order + text invoice.
    """
    try:
        order = create_order_supabase(
            business_id=user["business_id"],
            customer_phone=data.customer_phone,
            cart=data.items,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        log.exception("create_order_api error: %s", exc)
        raise HTTPException(500, "Failed to create order")

    invoice = generate_invoice_text(order)
    return {
        "message":  "Order created",
        "order_id": order.get("id"),
        "order":    order,
        "invoice":  invoice,
    }


@app.put("/orders/{order_id}/status")
def update_order_status_api(
    order_id: int,
    data: OrderStatusUpdate,
    user=Depends(require_business),
):
    existing = crud.get_order_by_id(order_id, user["business_id"])
    if not existing:
        raise HTTPException(404, "Order not found")

    try:
        order = update_order_status_supabase(order_id, data.status)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    return {"message": "Status updated", "order_id": order_id, "status": order["status"]}


@app.get("/orders/{order_id}/invoice")
def get_invoice_text(order_id: int, user=Depends(require_business)):
    order = crud.get_order_by_id(order_id, user["business_id"])
    if not order:
        raise HTTPException(404, "Order not found")
    return {"invoice": generate_invoice_text(order)}


# ─────────────────────────────────────────────────────────────────────────────
# LEGACY CONVERSATIONS
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/conversations")
def get_conversations(user=Depends(require_business)):
    return crud.get_conversations(user["business_id"])


@app.get("/conversations/{phone}")
def get_chat(phone: str, user=Depends(require_business)):
    return crud.get_messages_for_phone(user["business_id"], phone)


# ─────────────────────────────────────────────────────────────────────────────
# BROADCAST
# ─────────────────────────────────────────────────────────────────────────────

class BroadcastRequest(BaseModel):
    message: str

    @validator("message")
    def msg_valid(cls, v):
        v = v.strip()
        if len(v) < 3:    raise ValueError("Message too short")
        if len(v) > 1024: raise ValueError("Message too long (max 1024 chars)")
        return v


@app.post("/broadcast")
def broadcast(body: BroadcastRequest, user=Depends(require_business)):
    bid      = user["business_id"]
    business = crud.get_business_by_id(bid)

    try:
        token = crud.get_decrypted_token(business)
    except TokenDecryptionError as exc:
        log.error("broadcast: token decryption failed — %s", exc)
        raise HTTPException(503, "WhatsApp token cannot be decrypted. Re-enter it in Settings.")

    if not token:
        raise HTTPException(400, "WhatsApp token not configured. Go to Settings.")
    if not business.get("whatsapp_phone_id"):
        raise HTTPException(400, "WhatsApp Phone Number ID not configured. Go to Settings.")

    phones = crud.get_all_customer_phones(bid)
    if not phones:
        return {"sent": 0, "failed": 0, "total": 0, "message": "No customers found"}

    log.info("📢 Broadcast start  recipients=%d  business=%s", len(phones), business["name"])
    sent, failed, failed_phones = 0, 0, []

    for phone in phones:
        try:
            result = send_whatsapp(business["whatsapp_phone_id"], token, phone, body.message)
            if "error" in result:
                raise RuntimeError(result["error"])
            crud.log_message(bid, phone, "out", f"[BROADCAST] {body.message}")
            sent += 1
        except Exception as exc:
            log.error("broadcast: failed for %s — %s", phone, exc)
            failed += 1
            failed_phones.append(phone)

    log.info("📢 Broadcast done  sent=%d  failed=%d", sent, failed)
    return {"sent": sent, "failed": failed, "total": len(phones), "failed_numbers": failed_phones}


@app.get("/customers")
def get_customers(user=Depends(require_business)):
    phones = crud.get_all_customer_phones(user["business_id"])
    return {"phones": phones, "total": len(phones)}


# ─────────────────────────────────────────────────────────────────────────────
# CHAT INBOX (CRM)
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/chat/customers")
def chat_customers(
    search: Optional[str] = Query(None),
    user=Depends(get_current_user),
):
    return crud.get_customers_for_business(user["business_id"], search=search)


@app.get("/chat/conversations")
def chat_conversations(
    unread_only: bool = Query(False),
    user=Depends(get_current_user),
):
    return crud.get_chat_conversations(user["business_id"], filter_unread=unread_only)


@app.get("/chat/conversations/{phone:path}")
def chat_messages_by_phone(
    phone: str,
    limit:  int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user=Depends(get_current_user),
):
    from urllib.parse import unquote
    phone = unquote(phone)

    from db import supabase as _supa
    customer = (
        _supa.table("customers")
        .select("*")
        .eq("phone", phone)
        .eq("business_id", user["business_id"])
        .limit(1)
        .execute()
        .data
    )
    if not customer:
        legacy = crud.get_messages_for_phone(user["business_id"], phone)
        if not legacy:
            raise HTTPException(404, f"No conversation found for phone: {phone}")
        return {
            "customer_id":   None,
            "phone":         phone,
            "total_fetched": len(legacy),
            "limit":         limit,
            "offset":        offset,
            "messages": [
                {
                    "id":         m.get("id"),
                    "text":       m.get("message", ""),
                    "direction":  "outgoing" if m.get("direction") == "out" else "incoming",
                    "is_read":    True,
                    "status":     "sent",
                    "created_at": m.get("created_at"),
                }
                for m in legacy
            ],
        }

    c = customer[0]
    msgs = crud.get_messages_by_customer(c["id"], limit=limit, offset=offset)
    return {
        "customer_id":   c["id"],
        "phone":         phone,
        "total_fetched": len(msgs),
        "limit":         limit,
        "offset":        offset,
        "messages":      msgs,
    }


@app.get("/chat/messages/{customer_id}")
def chat_messages(
    customer_id: int,
    limit:  int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user=Depends(get_current_user),
):
    customer = crud.get_customer_by_id(customer_id, user["business_id"])
    if not customer:
        raise HTTPException(404, "Customer not found")

    msgs = crud.get_messages_by_customer(customer_id, limit=limit, offset=offset)
    return {
        "customer_id":   customer_id,
        "phone":         customer["phone"],
        "total_fetched": len(msgs),
        "limit":         limit,
        "offset":        offset,
        "messages":      msgs,
    }


@app.post("/chat/read/{customer_id}")
def mark_read(customer_id: int, user=Depends(get_current_user)):
    customer = crud.get_customer_by_id(customer_id, user["business_id"])
    if not customer:
        raise HTTPException(404, "Customer not found")
    crud.mark_messages_read(customer_id, user["business_id"])
    return {"ok": True, "customer_id": customer_id}


# ─────────────────────────────────────────────────────────────────────────────
# CHAT SEND
# ─────────────────────────────────────────────────────────────────────────────

class ChatSendRequest(BaseModel):
    customer_id: int
    text: str

    @validator("text")
    def text_valid(cls, v):
        v = v.strip()
        if not v:         raise ValueError("Message cannot be empty")
        if len(v) > 4096: raise ValueError("Message too long")
        return v


@app.post("/chat/send")
async def chat_send(body: ChatSendRequest, user=Depends(require_business)):
    bid      = user["business_id"]
    customer = crud.get_customer_by_id(body.customer_id, bid)
    if not customer:
        raise HTTPException(404, "Customer not found")

    business = crud.get_business_by_id(bid)
    try:
        token = crud.get_decrypted_token(business)
    except TokenDecryptionError as exc:
        log.error("chat_send: token decryption failed  business=%s  error=%s", business["name"], exc)
        raise HTTPException(503, "WhatsApp token cannot be decrypted. Re-enter it in Settings.")

    has_phone_id = bool(business.get("whatsapp_phone_id"))
    has_token    = bool(token)

    crud.log_message(bid, customer["phone"], "out", body.text)
    msg = crud.create_message(customer["id"], bid, body.text, "outgoing")

    wa_result: dict = {}
    if has_token and has_phone_id:
        wa_result = send_whatsapp(business["whatsapp_phone_id"], token, customer["phone"], body.text)
    else:
        missing = [k for k, v in {"phone_number_id": has_phone_id, "token": has_token}.items() if not v]
        log.warning("chat_send: WhatsApp NOT sent — missing: %s", missing)
        wa_result = {"error": f"credentials missing: {missing}"}

    await manager.broadcast(bid, {
        "event":       "new_message",
        "customer_id": customer["id"],
        "phone":       customer["phone"],
        "message":     msg,
    })

    return {"ok": True, "message_id": msg["id"], "whatsapp_result": wa_result}
