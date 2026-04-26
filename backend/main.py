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
    
    existing = crud.get_business_by_phone_id(phone_id)
    if existing and existing["owner_username"] != data.username:
        raise HTTPException(
        400,
        "That WhatsApp Phone Number ID is already used by another account."
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


# ─────────────────────────────────────────────────────────────────────────────
# WEBHOOK
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/webhook")
async def verify_webhook(request: Request):
    p = request.query_params
    if p.get("hub.verify_token") == VERIFY_TOKEN:
        return int(p.get("hub.challenge", 0))
    raise HTTPException(403, "Verification failed")


@app.post("/webhook")
async def receive_message(request: Request):
    data = await request.json()
    try:
        changes = data["entry"][0]["changes"][0]["value"]
        if "messages" not in changes:
            return {"status": "ok"}

        msg_obj = changes["messages"][0]
        if msg_obj.get("type") != "text":
            return {"status": "ok"}

        phone_number_id = changes["metadata"]["phone_number_id"]
        customer_phone  = msg_obj["from"]
        text            = msg_obj["text"]["body"].strip()
        log.info("📥 Incoming  phone_id=%s  from=%s  text=%r", phone_number_id, customer_phone, text)

        business = crud.get_business_by_phone_id(phone_number_id)
        if not business or not business.get("is_active", True):
            return {"status": "ok"}

        try:
            token = crud.get_decrypted_token(business)
        except TokenDecryptionError as exc:
            log.error("⚠️  Token decryption failed for business %s: %s", business["name"], exc)
            token = ""

        customer = crud.get_or_create_customer(customer_phone, business["id"])
        crud.log_message(business["id"], customer_phone, "in", text)
        in_msg = crud.create_message(customer["id"], business["id"], text, "incoming")

        await manager.broadcast(business["id"], {
            "event":       "new_message",
            "customer_id": customer["id"],
            "phone":       customer_phone,
            "message":     in_msg,
        })

        products = crud.get_products(business["id"])

        if "menu" in text.lower():
            if products:
                lines = "\n".join([f"{i+1}. {p['name']} — ${float(p['price']):.2f}" for i, p in enumerate(products)])
                reply = f"📋 *{business['name']} Menu*\n\n{lines}\n\nTo order: *order <item> <qty>*"
            else:
                reply = f"Hi! {business['name']}'s menu is being updated. Check back soon! 🙏"

        elif text.lower().startswith("order "):
            parts = text.strip().split()
            if len(parts) < 3:
                reply = "❌ Format: order <item> <quantity>\nExample: order sadza 2"
            else:
                try:
                    product_name = parts[1].lower()
                    qty = int(parts[2])
                    if qty <= 0: raise ValueError

                    class _Order:
                        customer_phone = customer_phone
                        product_name   = product_name
                        quantity       = qty

                    crud.create_order(business["id"], _Order())
                    reply = (
                        f"✅ *Order confirmed!*\n\n{product_name.capitalize()} × {qty}\n\n"
                        f"Thank you for ordering from {business['name']}! We'll be in touch. 🙏"
                    )
                except ValueError:
                    reply = "❌ Invalid quantity. Use a whole number.\nExample: order sadza 2"
        else:
            # Pass product dicts — ai.py accesses .name and .price via attribute;
            # we adapt with a simple wrapper
            class _P:
                def __init__(self, d):
                    self.name  = d["name"]
                    self.price = d["price"]

            reply = generate_reply(
                message=text,
                business_name=business["name"],
                products=[_P(p) for p in products],
            )

        crud.log_message(business["id"], customer_phone, "out", reply)
        out_msg = crud.create_message(customer["id"], business["id"], reply, "outgoing")

        await manager.broadcast(business["id"], {
            "event":       "new_message",
            "customer_id": customer["id"],
            "phone":       customer_phone,
            "message":     out_msg,
        })

        if token:
            send_whatsapp(phone_number_id, token, customer_phone, reply)
        else:
            log.warning("📵 WhatsApp NOT sent — no token for '%s'", business["name"])

    except KeyError as exc:
        log.error("⚠️  Webhook KeyError: %s | body: %s", exc, data)
    except Exception as exc:
        log.exception("⚠️  Webhook error: %s", exc)

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
