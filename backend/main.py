"""
main.py — WaziBot SaaS API (Supabase edition)

SQLAlchemy completely removed. All DB access goes through crud.py
which uses the supabase-py client directly.

No models.py, no database.py, no Session dependency.
"""

import os
import json
import logging
from typing import List, Optional
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

# ── App ────────────────────────────────────────────────────────────────────────
app = FastAPI(title="WaziBot API", docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
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
            "Check your Meta Developer Portal or update your existing account in Settings."
        )

    # Use a simple namespace so crud.create_business(data) works unchanged
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
        **({"business_id": business_id} if business_id else {}),
    }


# ─────────────────────────────────────────────────────────────────────────────
# DEBUG
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/debug/env")
def debug_env():
    fernet_key  = os.getenv("FERNET_KEY", "")
    secret_key  = os.getenv("SECRET_KEY", "")
    supa_url    = os.getenv("SUPABASE_URL", "")
    supa_key    = os.getenv("SUPABASE_KEY", "")
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
    """
    Diagnostic endpoint — checks every step the webhook relies on:
      1. Business record found by phone_number_id
      2. Token can be decrypted
      3. Products exist in DB
      4. WhatsApp API reachable with current token
    Hit this URL in a browser after logging in to see exactly what's broken.
    """
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

    # Step: decrypt token
    token = ""
    try:
        token = crud.get_decrypted_token(business)
        steps["token_decrypts"] = bool(token)
        steps["token_tail"]     = "…" + token[-6:] if token else "empty"
    except Exception as exc:
        steps["token_decrypts"] = False
        steps["token_error"]    = str(exc)

    # Step: products
    try:
        products = crud.get_products(bid)
        steps["products_count"] = len(products)
        steps["products"]       = [{"name": p["name"], "price": p["price"]} for p in products]
    except Exception as exc:
        steps["products_count"] = 0
        steps["products_error"] = str(exc)

    # Step: WhatsApp API ping
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
        steps["whatsapp_api_ok"] = False
        steps["whatsapp_api_skip"] = "missing phone_id or token"

    # Overall health
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
    """
    Meta webhook verification.
    MUST return the challenge as plain text (not JSON).
    Returning an int or JSON object causes Meta to reject the verification
    and stop sending all incoming messages to this endpoint.
    """
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
    """
    WhatsApp Cloud API webhook.

    Every step is logged individually so Render logs show exactly where
    a failure occurs rather than silently returning {"status": "ok"}.
    """
    data = await request.json()

    # ── STEP 1: Parse Meta payload ────────────────────────────────────────
    try:
        entry   = data.get("entry", [])
        if not entry:
            log.debug("Webhook: no entry — likely a status update, ignoring")
            return {"status": "ok"}

        changes = entry[0].get("changes", [])
        if not changes:
            return {"status": "ok"}

        value = changes[0].get("value", {})

        # Status updates (delivered / read receipts) — ignore silently
        if "statuses" in value and "messages" not in value:
            return {"status": "ok"}

        if "messages" not in value:
            log.debug("Webhook: no messages key in value — ignoring  value_keys=%s", list(value.keys()))
            return {"status": "ok"}

        msg_obj = value["messages"][0]
        msg_type = msg_obj.get("type", "")

        # Log non-text types so we know they arrived but were skipped
        if msg_type != "text":
            log.info("Webhook: skipping non-text message  type=%s", msg_type)
            return {"status": "ok"}

        metadata        = value.get("metadata", {})
        phone_number_id = metadata.get("phone_number_id", "")
        customer_phone  = msg_obj.get("from", "")
        text            = msg_obj.get("text", {}).get("body", "").strip()

        if not phone_number_id or not customer_phone or not text:
            log.warning("Webhook: missing required fields  phone_id=%r  from=%r  text=%r",
                        phone_number_id, customer_phone, text)
            return {"status": "ok"}

        # ── Extract the WhatsApp message ID (wamid.XXX…) ─────────────────
        # This is Meta's unique ID per message — present on every inbound text.
        # We use it as the deduplication key so webhook retries are ignored.
        wa_message_id = msg_obj.get("id", "")

        log.info(
            "📩 STEP 1 OK — Incoming  wa_id=%s  phone_id=%s  from=%s  text=%r",
            wa_message_id, phone_number_id, customer_phone, text,
        )

    except Exception as exc:
        log.error("📥 STEP 1 FAIL — Could not parse webhook payload: %s | body: %s", exc, data)
        return {"status": "ok"}

    # ── STEP 1b: Deduplication gate ───────────────────────────────────────
    # WhatsApp retries unacknowledged webhooks up to ~20 times over ~24 h.
    # We must return HTTP 200 immediately (which we do), but if a previous
    # delivery already processed this message_id we must NOT process it again.
    # Check happens here — BEFORE business lookup, token decryption, DB writes,
    # AI calls, and WhatsApp sends — so retries are cheap (one DB read).
    if wa_message_id:
        try:
            if crud.message_exists(wa_message_id):
                log.warning(
                    "⚠️  DUPLICATE — wa_id=%s already processed, skipping  from=%s",
                    wa_message_id, customer_phone,
                )
                return {"status": "ok"}
        except Exception as exc:
            # If the dedup check itself fails, log and continue processing
            # to avoid dropping a real message due to a transient DB error.
            log.error("⚠️  Dedup check failed (will process anyway): %s", exc)

    # ── STEP 2: Find business ─────────────────────────────────────────────
    try:
        business = crud.get_business_by_phone_id(phone_number_id)
        if not business:
            log.error("📋 STEP 2 FAIL — No business found for phone_number_id=%s  "
                      "Check that this Phone Number ID is saved in Settings.", phone_number_id)
            return {"status": "ok"}
        if not business.get("is_active", True):
            log.warning("📋 STEP 2 — Business suspended  id=%s  name=%s", business["id"], business["name"])
            return {"status": "ok"}
        log.info("📋 STEP 2 OK — Business found  id=%s  name=%s", business["id"], business["name"])
    except Exception as exc:
        log.exception("📋 STEP 2 FAIL — business lookup error: %s", exc)
        return {"status": "ok"}

    # ── STEP 3: Decrypt token ─────────────────────────────────────────────
    token = ""
    try:
        token = crud.get_decrypted_token(business)
        if token:
            log.info("🔑 STEP 3 OK — Token decrypted  tail=…%s", token[-6:])
        else:
            log.warning("🔑 STEP 3 — No token configured for business %s  "
                        "Messages will be saved but NOT sent via WhatsApp", business["name"])
    except TokenDecryptionError as exc:
        log.error("🔑 STEP 3 FAIL — Token decryption error for %s: %s  "
                  "Re-enter WhatsApp token in Settings.", business["name"], exc)

    # ── STEP 4: Get or create customer ────────────────────────────────────
    try:
        customer = crud.get_or_create_customer(customer_phone, business["id"])
        log.info("👤 STEP 4 OK — Customer  id=%s  phone=%s", customer["id"], customer["phone"])
    except Exception as exc:
        log.exception("👤 STEP 4 FAIL — customer upsert error: %s", exc)
        return {"status": "ok"}

    # ── STEP 5: Save incoming message ────────────────────────────────────
    in_msg: dict = {}
    try:
        crud.log_message(business["id"], customer_phone, "in", text)
        in_msg = crud.create_message(
            customer["id"], business["id"], text, "incoming",
            wa_message_id=wa_message_id,   # stored for dedup on retries
        )
        log.info("💾 STEP 5 OK — Incoming message saved  id=%s  wa_id=%s",
                 in_msg.get("id", "?"), wa_message_id)
    except Exception as exc:
        # A UNIQUE violation on wa_message_id here means the dedup SELECT
        # above had a race condition with a concurrent retry — safe to skip.
        err_str = str(exc)
        if "wa_message_id" in err_str or "unique" in err_str.lower():
            log.warning("💾 STEP 5 — Duplicate detected at INSERT level  wa_id=%s — skipping",
                        wa_message_id)
            return {"status": "ok"}
        log.exception("💾 STEP 5 FAIL — could not save incoming message: %s", exc)
        # Non-fatal — still attempt to reply

    try:
        await manager.broadcast(business["id"], {
            "event": "new_message", "customer_id": customer["id"],
            "phone": customer_phone, "message": in_msg,
        })
    except Exception:
        pass  # WebSocket broadcast failure must never block the reply

    # ── STEP 6: Fetch products + generate reply ────────────────────────────
    try:
        products = crud.get_products(business["id"])
        log.info("📦 STEP 6 — Products fetched  count=%d  business_id=%s",
                 len(products), business["id"])

        text_lower = text.lower().strip()

        if "menu" in text_lower:
            if products:
                lines = "\n".join(
                    [f"{i+1}. {p['name']} — ${float(p['price']):.2f}"
                     for i, p in enumerate(products)]
                )
                reply = (
                    f"📋 *{business['name']} Menu*\n\n"
                    f"{lines}\n\n"
                    f"To order, type:\n*order <item> <quantity>*\n"
                    f"Example: _order {products[0]['name']} 1_"
                )
                log.info("📋 STEP 6 — Menu reply built  products=%d", len(products))
            else:
                reply = (
                    f"Hi! 👋 {business['name']}'s menu is being updated. "
                    f"Check back very soon! 🙏"
                )
                log.warning("📋 STEP 6 — Menu requested but NO products in DB for business_id=%s",
                            business["id"])

        elif text_lower.startswith("order "):
            parts = text.strip().split()
            if len(parts) < 3:
                reply = "❌ Format: order <item> <quantity>\nExample: order sadza 2"
            else:
                try:
                    p_name = parts[1].lower()
                    qty    = int(parts[2])
                    if qty <= 0:
                        raise ValueError("qty must be positive")

                    # Use a plain dict passed into crud.create_order via a simple namespace
                    class _OrderPayload:
                        pass
                    op = _OrderPayload()
                    op.customer_phone = customer_phone
                    op.product_name   = p_name
                    op.quantity       = qty

                    crud.create_order(business["id"], op)
                    log.info("🛒 STEP 6 — Order created  product=%s  qty=%d", p_name, qty)
                    reply = (
                        f"✅ *Order confirmed!*\n\n"
                        f"{p_name.capitalize()} × {qty}\n\n"
                        f"Thank you for ordering from *{business['name']}*! "
                        f"We\'ll be in touch shortly. 🙏"
                    )
                except ValueError:
                    reply = "❌ Invalid quantity. Please use a whole number.\nExample: order sadza 2"

        else:
            class _ProductProxy:
                def __init__(self, d: dict):
                    self.name  = d.get("name", "")
                    self.price = d.get("price", 0)

            reply = generate_reply(
                message=text,
                business_name=business["name"],
                products=[_ProductProxy(p) for p in products],
            )
            log.info("🤖 STEP 6 — AI reply generated  len=%d", len(reply))

    except Exception as exc:
        log.exception("📦 STEP 6 FAIL — reply generation error: %s", exc)
        reply = (
            f"Hi! 👋 Thanks for contacting *{business['name']}*. "
            f"We received your message and will get back to you shortly."
        )

    # ── STEP 7: Save outgoing message ─────────────────────────────────────
    try:
        crud.log_message(business["id"], customer_phone, "out", reply)
        out_msg = crud.create_message(customer["id"], business["id"], reply, "outgoing")
        log.info("💾 STEP 7 OK — Outgoing message saved  id=%s", out_msg["id"] if out_msg else "?")
    except Exception as exc:
        log.exception("💾 STEP 7 FAIL — could not save outgoing message: %s", exc)

    try:
        await manager.broadcast(business["id"], {
            "event": "new_message", "customer_id": customer["id"],
            "phone": customer_phone, "message": out_msg,
        })
    except Exception:
        pass

    # ── STEP 8: Send via WhatsApp API ─────────────────────────────────────
    if token:
        log.info("📤 STEP 8 — Sending via WhatsApp  to=%s  phone_id=%s  reply_len=%d",
                 customer_phone, phone_number_id, len(reply))
        result = send_whatsapp(phone_number_id, token, customer_phone, reply)
        if "error" in result:
            log.error("📤 STEP 8 FAIL — WhatsApp API error: %s", result["error"])
        else:
            log.info("📤 STEP 8 OK — WhatsApp delivered  msg_id=%s",
                     (result.get("messages") or [{}])[0].get("id", "?"))
    else:
        log.error(
            "📤 STEP 8 FAIL — No WhatsApp token for business '%s' (id=%s). "
            "Message was SAVED to DB but NOT delivered. "
            "Fix: go to Settings → enter WhatsApp Access Token.",
            business["name"], business["id"]
        )

    return {"status": "ok"}


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


@app.post("/admin/businesses")
def admin_create_business(data: BaseModel, _=Depends(require_superadmin)):
    if crud.get_business_by_username(data.owner_username):
        raise HTTPException(400, "Username already taken")
    if getattr(data, "whatsapp_phone_id", None) and crud.get_business_by_phone_id(data.whatsapp_phone_id):
        raise HTTPException(400, "WhatsApp Phone Number ID already registered.")
    return crud.create_business(data)


@app.patch("/admin/businesses/{business_id}")
def admin_update_business(business_id: int, data: BaseModel, _=Depends(require_superadmin)):
    b = crud.update_business(business_id, data)
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
# BUSINESS PROFILE
# ─────────────────────────────────────────────────────────────────────────────

class BusinessUpdate(BaseModel):
    name:               Optional[str]  = None
    whatsapp_phone_id:  Optional[str]  = None
    whatsapp_token:     Optional[str]  = None
    is_active:          Optional[bool] = None


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

    # Duplicate phone_id check before UPDATE to give a clean 400 instead of DB error
    new_phone_id = safe.get("whatsapp_phone_id")
    if new_phone_id:
        existing = crud.get_business_by_phone_id(new_phone_id)
        if existing and existing["id"] != user["business_id"]:
            raise HTTPException(
                400,
                "That WhatsApp Phone Number ID is already registered to another account."
            )

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
    name:      str
    price:     float
    image_url: Optional[str] = None


@app.get("/products")
def get_products(user=Depends(require_business)):
    return crud.get_products(user["business_id"])


@app.post("/products", status_code=201)
def create_product(product: ProductCreate, user=Depends(require_business)):
    log.info(
        "📦 create_product  business_id=%s  name=%r  price=%s  has_image=%s",
        user["business_id"], product.name, product.price, bool(product.image_url),
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

@app.get("/orders")
def get_orders(user=Depends(require_business)):
    return crud.get_orders(user["business_id"])


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
    """
    Return messages for a conversation identified by phone number.

    dashboard.js calls:  GET /chat/conversations/{phone}
    This endpoint resolves phone → customer → messages so the dashboard
    never gets a 404.  The response shape is identical to /chat/messages/{id}
    so no frontend changes are needed.

    The {phone:path} converter handles phone numbers that contain a '+'
    prefix (e.g. +263771234567) which FastAPI would otherwise reject as
    an invalid path segment.
    """
    # URL-decode the phone in case it was percent-encoded by the browser
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
        # Fall back to legacy chat_messages table so the dashboard still
        # shows data for contacts who haven't been migrated to the CRM yet.
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
    log.info(
        "chat_send  customer=%s  phone=%s  has_phone_id=%s  has_token=%s  token_tail=…%s",
        body.customer_id, customer["phone"], has_phone_id, has_token,
        token[-6:] if has_token else "N/A",
    )

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
