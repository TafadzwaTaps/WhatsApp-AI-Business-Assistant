"""
main.py — WaziBot SaaS API

Fixes applied (see inline FIX comments):
  FIX-1  /auth/refresh endpoint implemented — removes frontend 404 errors.
  FIX-2  Static directory uses __file__-relative path; falls back to ./static
         so it works whether the folder is at backend/ or at project root.
  FIX-3  send_whatsapp() now logs the partial token + full API response for
         easy debugging.
  FIX-4  chat_send: logs token status before attempting WhatsApp delivery.
  FIX-5  broadcast: tracks per-number success/failure with detailed logging.
  FIX-6  saveSession() in the frontend now always writes business_id.
  FIX-7  All token decryption goes through crud.get_decrypted_token() which
         uses the fixed crypto.py — no raw decrypt calls elsewhere.
"""

import os
import json
import logging
from typing import List, Optional
from datetime import datetime

from fastapi import (
    FastAPI, Request, Depends, HTTPException,
    WebSocket, WebSocketDisconnect, Query,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, validator
from sqlalchemy.orm import Session
import requests as http_requests
from dotenv import load_dotenv

from database import Base, engine, SessionLocal
import models
from schemas import (
    ProductCreate, ProductOut,
    OrderOut, OrderCreate,
    ChatMessageOut,
    BusinessCreate, BusinessOut, BusinessUpdate,
)
import crud
from ai import generate_reply
from auth import (
    verify_password,
    create_access_token,
    create_refresh_token,
    decode_token,
    get_current_user,
    require_superadmin,
    require_business,
    SUPER_ADMIN_USERNAME,
    SUPER_ADMIN_PASSWORD,
)

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(name)s  %(message)s")
log = logging.getLogger("wazibot")

Base.metadata.create_all(bind=engine)

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "myverifytoken123")

# FIX-2 — robust static directory resolution
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_candidates = [
    os.path.join(BASE_DIR, "static"),          # backend/static/
    os.path.join(BASE_DIR, "..", "static"),     # project-root/static/
    os.path.join(BASE_DIR, "..", "..", "static"),
]
STATIC_DIR = next(
    (os.path.abspath(p) for p in _candidates if os.path.isdir(p)),
    os.path.abspath(os.path.join(BASE_DIR, "static")),   # will be created
)
os.makedirs(STATIC_DIR, exist_ok=True)
log.info("📁 Static directory: %s", STATIC_DIR)

app = FastAPI(title="WaziBot SaaS API", docs_url=None, redoc_url=None)

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
        raise HTTPException(status_code=404, detail=f"{name} not found in static dir")
    return FileResponse(path)


@app.get("/")          
def landing():   return _html("landing.html")

@app.get("/dashboard") 
def dashboard(): return _html("dashboard.html")

@app.get("/inbox")     
def inbox():     return _html("inbox.html")

@app.get("/signup")    
def signup_page():return _html("signup.html")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ─── WEBSOCKET CONNECTION MANAGER ────────────────────────
class ConnectionManager:
    def __init__(self):
        self._connections: dict[int, list[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, business_id: int):
        await websocket.accept()
        self._connections.setdefault(business_id, []).append(websocket)

    def disconnect(self, websocket: WebSocket, business_id: int):
        conns = self._connections.get(business_id, [])
        if websocket in conns:
            conns.remove(websocket)

    async def broadcast(self, business_id: int, payload: dict):
        dead = []
        for ws in self._connections.get(business_id, []):
            try:
                await ws.send_text(json.dumps(payload, default=str))
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws, business_id)


manager = ConnectionManager()


# ─── WHATSAPP SENDER ─────────────────────────────────────
# FIX-3 — detailed logging for every send attempt
def send_whatsapp(phone_number_id: str, token: str, to: str, message: str) -> dict:
    if not phone_number_id or not token:
        log.warning("send_whatsapp: missing credentials — phone_id=%s token_set=%s",
                    phone_number_id, bool(token))
        return {"error": "missing credentials"}

    to = to.replace("whatsapp:", "").strip()
    url = f"https://graph.facebook.com/v18.0/{phone_number_id}/messages"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    body = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": message},
    }

    # Log partial token so you can verify the right key is used without leaking it
    log.info("📤 WhatsApp → to=%s  phone_id=%s  token=…%s", to, phone_number_id, token[-6:])

    try:
        resp = http_requests.post(url, headers=headers, json=body, timeout=10)
        result = resp.json()
        if resp.status_code == 200:
            msg_id = (result.get("messages") or [{}])[0].get("id", "?")
            log.info("✅ WhatsApp sent OK  msg_id=%s  to=%s", msg_id, to)
        else:
            err = result.get("error", {})
            log.error(
                "❌ WhatsApp API %s: code=%s msg=%s",
                resp.status_code,
                err.get("code"),
                err.get("message"),
            )
        return result
    except Exception as exc:
        log.exception("send_whatsapp exception: %s", exc)
        return {"error": str(exc)}


# ─── SIGNUP ──────────────────────────────────────────────
class SignupRequest(BaseModel):
    business_name: str
    username: str
    password: str
    whatsapp_phone_id: str = ""
    whatsapp_token: str = ""

    @validator("username")
    def username_valid(cls, v):
        v = v.strip().lower()
        if len(v) < 3:   raise ValueError("Username must be at least 3 characters")
        if " " in v:     raise ValueError("Username cannot contain spaces")
        return v

    @validator("password")
    def password_valid(cls, v):
        if len(v) < 6:   raise ValueError("Password must be at least 6 characters")
        return v

    @validator("business_name")
    def bizname_valid(cls, v):
        v = v.strip()
        if len(v) < 2:   raise ValueError("Business name too short")
        return v


@app.post("/auth/signup")
def signup(data: SignupRequest, db: Session = Depends(get_db)):
    if data.username == SUPER_ADMIN_USERNAME.lower():
        raise HTTPException(status_code=400, detail="Username not available")
    if crud.get_business_by_username(db, data.username):
        raise HTTPException(status_code=400, detail="Username already taken")

    business = crud.create_business(db, BusinessCreate(
        name=data.business_name,
        owner_username=data.username,
        owner_password=data.password,
        whatsapp_phone_id=data.whatsapp_phone_id.strip() or None,
        whatsapp_token=data.whatsapp_token.strip() or None,
    ))
    access  = create_access_token({"sub": business.owner_username, "role": "business", "business_id": business.id})
    refresh = create_refresh_token({"sub": business.owner_username, "role": "business", "business_id": business.id})
    log.info("🆕 Signup: %s (@%s)", business.name, business.owner_username)
    return {
        "access_token":  access,
        "refresh_token": refresh,
        "token_type":    "bearer",
        "role":          "business",
        "business_name": business.name,
        "business_id":   business.id,
    }


# ─── LOGIN ────────────────────────────────────────────────
class LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/auth/login")
def login(data: LoginRequest, db: Session = Depends(get_db)):
    username = data.username.strip().lower()

    if username == SUPER_ADMIN_USERNAME.lower():
        if not verify_password(data.password, SUPER_ADMIN_PASSWORD):
            raise HTTPException(status_code=401, detail="Invalid credentials")
        access  = create_access_token({"sub": SUPER_ADMIN_USERNAME, "role": "superadmin"})
        refresh = create_refresh_token({"sub": SUPER_ADMIN_USERNAME, "role": "superadmin"})
        return {"access_token": access, "refresh_token": refresh, "token_type": "bearer", "role": "superadmin"}

    business = crud.get_business_by_username(db, username)
    if not business or not verify_password(data.password, business.owner_password):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not business.is_active:
        raise HTTPException(status_code=403, detail="Account suspended. Contact support.")

    access  = create_access_token({"sub": business.owner_username, "role": "business", "business_id": business.id})
    refresh = create_refresh_token({"sub": business.owner_username, "role": "business", "business_id": business.id})
    log.info("🔑 Login: %s", business.owner_username)
    return {
        "access_token":  access,
        "refresh_token": refresh,
        "token_type":    "bearer",
        "role":          "business",
        "business_name": business.name,
        "business_id":   business.id,
    }


# FIX-1 — /auth/refresh endpoint
class RefreshRequest(BaseModel):
    refresh_token: str


@app.post("/auth/refresh")
def refresh_token_endpoint(data: RefreshRequest, db: Session = Depends(get_db)):
    """
    Exchange a valid refresh token for a new access token.
    Frontend calls this automatically when it gets a 401.
    """
    try:
        payload = decode_token(data.refresh_token)
    except HTTPException:
        raise HTTPException(status_code=401, detail="Refresh token invalid or expired. Please log in again.")

    if payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Not a refresh token.")

    sub         = payload.get("sub")
    role        = payload.get("role", "business")
    business_id = payload.get("business_id")

    # Verify the account still exists / is active
    if role == "business":
        biz = crud.get_business_by_username(db, sub)
        if not biz or not biz.is_active:
            raise HTTPException(status_code=401, detail="Account not found or suspended.")
        business_id = biz.id

    new_access   = create_access_token({"sub": sub, "role": role, "business_id": business_id})
    new_refresh  = create_refresh_token({"sub": sub, "role": role, "business_id": business_id})

    response = {
        "access_token":  new_access,
        "refresh_token": new_refresh,
        "token_type":    "bearer",
        "role":          role,
    }
    if role == "business":
        response["business_id"] = business_id
    log.info("🔄 Token refreshed for: %s", sub)
    return response


# ─── WEBHOOK ─────────────────────────────────────────────
@app.get("/webhook")
async def verify_webhook(request: Request):
    params = request.query_params
    if params.get("hub.verify_token") == VERIFY_TOKEN:
        return int(params.get("hub.challenge"))
    return {"error": "Verification failed"}


@app.post("/webhook")
async def receive_message(request: Request, db: Session = Depends(get_db)):
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

        log.info("📥 Incoming | phone_id=%s | from=%s | text=%r", phone_number_id, customer_phone, text)

        business = crud.get_business_by_phone_id(db, phone_number_id)
        if not business or not business.is_active:
            return {"status": "ok"}

        token = crud.get_decrypted_token(business)

        customer = crud.get_or_create_customer(db, customer_phone, business.id)
        crud.log_message(db, business.id, customer_phone, "in", text)
        in_msg = crud.create_message(db, customer.id, business.id, text, "incoming")

        await manager.broadcast(business.id, {
            "event":       "new_message",
            "customer_id": customer.id,
            "phone":       customer_phone,
            "message": {
                "id":         in_msg.id,
                "text":       in_msg.text,
                "direction":  in_msg.direction,
                "status":     in_msg.status,
                "is_read":    in_msg.is_read,
                "created_at": in_msg.created_at.isoformat() if in_msg.created_at else None,
            },
        })

        products = crud.get_products(db, business.id)

        if "menu" in text.lower():
            if products:
                lines = "\n".join([f"{i+1}. {p.name} - ${p.price}" for i, p in enumerate(products)])
                reply = f"📋 *{business.name} Menu*\n\n{lines}\n\nTo order, type:\n*order <item> <quantity>*\nExample: order sadza 2"
            else:
                reply = f"Hi! {business.name}'s menu is being updated. Check back soon! 🙏"

        elif text.lower().startswith("order "):
            parts = text.strip().split()
            if len(parts) < 3:
                reply = "❌ Please use the format:\norder <item> <quantity>\nExample: order sadza 2"
            else:
                try:
                    product_name = parts[1].lower()
                    qty = int(parts[2])
                    if qty <= 0:
                        raise ValueError
                    crud.create_order(db, business.id, OrderCreate(
                        customer_phone=customer_phone,
                        product_name=product_name,
                        quantity=qty,
                    ))
                    reply = (
                        f"✅ *Order confirmed!*\n\n{product_name.capitalize()} × {qty}\n\n"
                        f"Thank you for ordering from {business.name}! We'll be in touch shortly. 🙏"
                    )
                except ValueError:
                    reply = "❌ Invalid quantity. Use a whole number.\nExample: order sadza 2"
        else:
            reply = generate_reply(message=text, business_name=business.name, products=products)

        crud.log_message(db, business.id, customer_phone, "out", reply)
        out_msg = crud.create_message(db, customer.id, business.id, reply, "outgoing")

        await manager.broadcast(business.id, {
            "event":       "new_message",
            "customer_id": customer.id,
            "phone":       customer_phone,
            "message": {
                "id":         out_msg.id,
                "text":       out_msg.text,
                "direction":  out_msg.direction,
                "status":     out_msg.status,
                "is_read":    out_msg.is_read,
                "created_at": out_msg.created_at.isoformat() if out_msg.created_at else None,
            },
        })

        if token:
            send_whatsapp(phone_number_id, token, customer_phone, reply)
        else:
            log.warning("📵 No token for business %s — reply NOT sent via WhatsApp", business.name)

        log.info("✅ [%s] %s replied", business.name, customer_phone)

    except KeyError as exc:
        log.error("⚠️  Webhook KeyError: %s | data: %s", exc, data)
    except Exception as exc:
        log.exception("⚠️  Webhook error: %s", exc)

    return {"status": "ok"}


# ─── WEBSOCKET: REAL-TIME INBOX ───────────────────────────
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
            msg = json.loads(raw) if raw else {}
            if msg.get("type") == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))
    except WebSocketDisconnect:
        manager.disconnect(websocket, business_id)


# ─── SUPERADMIN ───────────────────────────────────────────
@app.get("/admin/businesses", response_model=List[BusinessOut])
def list_businesses(db=Depends(get_db), user=Depends(require_superadmin)):
    return crud.get_all_businesses(db)


@app.post("/admin/businesses", response_model=BusinessOut)
def admin_create_business(data: BusinessCreate, db=Depends(get_db), user=Depends(require_superadmin)):
    if crud.get_business_by_username(db, data.owner_username):
        raise HTTPException(status_code=400, detail="Username already taken")
    return crud.create_business(db, data)


@app.patch("/admin/businesses/{business_id}", response_model=BusinessOut)
def admin_update_business(business_id: int, data: BusinessUpdate, db=Depends(get_db), user=Depends(require_superadmin)):
    b = crud.update_business(db, business_id, data)
    if not b:
        raise HTTPException(status_code=404, detail="Business not found")
    return b


@app.delete("/admin/businesses/{business_id}")
def admin_delete_business(business_id: int, db=Depends(get_db), user=Depends(require_superadmin)):
    b = crud.delete_business(db, business_id)
    if not b:
        raise HTTPException(status_code=404, detail="Business not found")
    return {"deleted": business_id}


@app.get("/admin/stats")
def admin_stats(db=Depends(get_db), user=Depends(require_superadmin)):
    businesses = crud.get_all_businesses(db)
    orders = db.query(models.Order).all()
    return {
        "businesses":        len(businesses),
        "active_businesses": sum(1 for b in businesses if b.is_active),
        "total_orders":      len(orders),
        "total_revenue":     round(sum(o.total_price or 0 for o in orders), 2),
    }


# ─── BUSINESS: PROFILE ───────────────────────────────────
@app.get("/me", response_model=BusinessOut)
def get_me(db=Depends(get_db), user=Depends(require_business)):
    b = crud.get_business_by_id(db, user["business_id"])
    if not b:
        raise HTTPException(status_code=404, detail="Not found")
    return b


@app.patch("/me", response_model=BusinessOut)
def update_me(data: BusinessUpdate, db=Depends(get_db), user=Depends(require_business)):
    data_dict = data.dict(exclude_none=True)
    data_dict.pop("is_active", None)
    return crud.update_business(db, user["business_id"], BusinessUpdate(**data_dict))


@app.get("/me/test-whatsapp")
def test_whatsapp_connection(db=Depends(get_db), user=Depends(require_business)):
    b = crud.get_business_by_id(db, user["business_id"])
    if not b or not b.whatsapp_phone_id:
        return {"ok": False, "reason": "No Phone Number ID saved"}
    token = crud.get_decrypted_token(b)
    if not token:
        return {"ok": False, "reason": "No access token saved (or decryption failed — check FERNET_KEY)"}
    try:
        resp = http_requests.get(
            f"https://graph.facebook.com/v18.0/{b.whatsapp_phone_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=5,
        )
        if resp.status_code == 200:
            return {"ok": True, "reason": "Connected"}
        err = resp.json().get("error", {}).get("message", "Unknown error")
        return {"ok": False, "reason": err}
    except Exception as exc:
        return {"ok": False, "reason": str(exc)}


# ─── PRODUCTS ────────────────────────────────────────────
@app.get("/products", response_model=List[ProductOut])
def get_products(db=Depends(get_db), user=Depends(get_current_user)):
    return crud.get_products(db, user["business_id"])


@app.post("/products", response_model=ProductOut)
def create_product(product: ProductCreate, db=Depends(get_db), user=Depends(require_business)):
    return crud.create_product(db, user["business_id"], product)


@app.delete("/products/{product_id}")
def delete_product(product_id: int, db=Depends(get_db), user=Depends(require_business)):
    p = crud.delete_product(db, product_id, user["business_id"])
    if not p:
        raise HTTPException(status_code=404, detail="Product not found")
    return {"deleted": product_id}


# ─── ORDERS ──────────────────────────────────────────────
@app.get("/orders", response_model=List[OrderOut])
def get_orders(db=Depends(get_db), user=Depends(require_business)):
    return crud.get_orders(db, user["business_id"])


# ─── LEGACY CONVERSATIONS ────────────────────────────────
@app.get("/conversations", response_model=List[ChatMessageOut])
def get_conversations(db=Depends(get_db), user=Depends(require_business)):
    return crud.get_conversations(db, user["business_id"])


@app.get("/conversations/{phone}", response_model=List[ChatMessageOut])
def get_chat(phone: str, db=Depends(get_db), user=Depends(require_business)):
    return crud.get_messages_for_phone(db, user["business_id"], phone)


# ─── BROADCAST ───────────────────────────────────────────
# FIX-5 — per-number tracking + detailed logs
class BroadcastRequest(BaseModel):
    message: str

    @validator("message")
    def message_valid(cls, v):
        v = v.strip()
        if len(v) < 3:    raise ValueError("Message too short")
        if len(v) > 1024: raise ValueError("Message too long (max 1024 characters)")
        return v


@app.post("/broadcast")
def broadcast(body: BroadcastRequest, db=Depends(get_db), user=Depends(require_business)):
    bid      = user["business_id"]
    business = crud.get_business_by_id(db, bid)
    token    = crud.get_decrypted_token(business)
    phones   = crud.get_all_customer_phones(db, bid)

    if not token:
        log.error("broadcast: no decrypted token for business %s — aborting", business.name)
        raise HTTPException(status_code=400, detail="WhatsApp token not configured or could not be decrypted.")

    if not business.whatsapp_phone_id:
        raise HTTPException(status_code=400, detail="WhatsApp Phone Number ID not configured.")

    if not phones:
        return {"sent": 0, "failed": 0, "total": 0, "message": "No customers found"}

    log.info("📢 Broadcast start: %d recipients — business=%s", len(phones), business.name)

    sent, failed, failed_numbers = 0, 0, []
    for phone in phones:
        try:
            result = send_whatsapp(business.whatsapp_phone_id, token, phone, body.message)
            if "error" in result:
                raise RuntimeError(result["error"])
            crud.log_message(db, bid, phone, "out", f"[BROADCAST] {body.message}")
            sent += 1
        except Exception as exc:
            log.error("broadcast: failed for %s — %s", phone, exc)
            failed += 1
            failed_numbers.append(phone)

    log.info("📢 Broadcast done: sent=%d failed=%d", sent, failed)
    return {
        "sent":           sent,
        "failed":         failed,
        "total":          len(phones),
        "failed_numbers": failed_numbers,
    }


@app.get("/customers")
def get_customers(db=Depends(get_db), user=Depends(require_business)):
    phones = crud.get_all_customer_phones(db, user["business_id"])
    return {"phones": phones, "total": len(phones)}


# ─── CHAT INBOX (CRM) ────────────────────────────────────
@app.get("/chat/customers")
def chat_customers(
    search: Optional[str] = Query(None),
    db=Depends(get_db),
    user=Depends(get_current_user),
):
    customers = crud.get_customers_for_business(db, user["business_id"], search=search)
    return [
        {
            "id":             c.id,
            "phone":          c.phone,
            "customer_since": c.created_at.isoformat() if c.created_at else None,
            "last_seen":      c.last_seen.isoformat() if c.last_seen else None,
            "unread_count":   c.unread_count or 0,
        }
        for c in customers
    ]


@app.get("/chat/conversations")
def chat_conversations(
    unread_only: bool = Query(False),
    db=Depends(get_db),
    user=Depends(get_current_user),
):
    return crud.get_chat_conversations(db, user["business_id"], filter_unread=unread_only)


@app.get("/chat/conversations/{phone}")
def chat_by_phone(phone: str, db=Depends(get_db), user=Depends(get_current_user)):
    customer = db.query(models.Customer).filter(
        models.Customer.phone == phone,
        models.Customer.business_id == user["business_id"],
    ).first()
    if not customer:
        return []
    return [
        {"id": m.id, "message": m.text, "direction": m.direction, "created_at": m.created_at}
        for m in crud.get_messages_by_customer(db, customer.id)
    ]


@app.get("/chat/messages/{customer_id}")
def chat_messages(
    customer_id: int,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db=Depends(get_db),
    user=Depends(get_current_user),
):
    customer = crud.get_customer_by_id(db, customer_id, user["business_id"])
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")

    msgs = crud.get_messages_by_customer(db, customer_id, limit=limit, offset=offset)
    return {
        "customer_id":   customer_id,
        "phone":         customer.phone,
        "total_fetched": len(msgs),
        "limit":         limit,
        "offset":        offset,
        "messages": [
            {
                "id":         m.id,
                "text":       m.text or "",
                "direction":  m.direction or "",
                "is_read":    bool(m.is_read),
                "status":     m.status or "sent",
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in msgs
        ],
    }


@app.post("/chat/read/{customer_id}")
def mark_read(customer_id: int, db=Depends(get_db), user=Depends(get_current_user)):
    customer = crud.get_customer_by_id(db, customer_id, user["business_id"])
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")
    crud.mark_messages_read(db, customer_id, user["business_id"])
    return {"ok": True, "customer_id": customer_id}


# ─── CHAT: SEND FROM DASHBOARD ───────────────────────────
# FIX-4 — logs token status before delivery
class ChatSendRequest(BaseModel):
    customer_id: int
    text: str

    @validator("text")
    def text_valid(cls, v):
        v = v.strip()
        if not v:        raise ValueError("Message cannot be empty")
        if len(v) > 4096: raise ValueError("Message too long")
        return v


@app.post("/chat/send")
async def chat_send(
    body: ChatSendRequest,
    db=Depends(get_db),
    user=Depends(require_business),
):
    bid      = user["business_id"]
    customer = crud.get_customer_by_id(db, body.customer_id, bid)
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")

    business = crud.get_business_by_id(db, bid)
    token    = crud.get_decrypted_token(business)

    # Persist to both tables
    crud.log_message(db, bid, customer.phone, "out", body.text)
    msg = crud.create_message(db, customer.id, bid, body.text, "outgoing")

    # FIX-4 — explicit pre-send diagnostics
    has_phone_id = bool(business.whatsapp_phone_id)
    has_token    = bool(token)
    log.info(
        "chat_send: customer=%s  phone=%s  has_phone_id=%s  has_token=%s  token_suffix=…%s",
        body.customer_id, customer.phone, has_phone_id, has_token,
        token[-6:] if has_token else "N/A",
    )

    result: dict = {}
    if has_token and has_phone_id:
        result = send_whatsapp(business.whatsapp_phone_id, token, customer.phone, body.text)
    else:
        missing = []
        if not has_phone_id: missing.append("whatsapp_phone_id")
        if not has_token:    missing.append("whatsapp_token")
        log.warning("chat_send: WhatsApp NOT sent — missing: %s", ", ".join(missing))
        result = {"error": f"WhatsApp credentials missing: {', '.join(missing)}"}

    # Push to WebSocket
    await manager.broadcast(bid, {
        "event":       "new_message",
        "customer_id": customer.id,
        "phone":       customer.phone,
        "message": {
            "id":         msg.id,
            "text":       msg.text,
            "direction":  msg.direction,
            "status":     msg.status,
            "is_read":    msg.is_read,
            "created_at": msg.created_at.isoformat() if msg.created_at else None,
        },
    })

    return {
        "ok":              True,
        "message_id":      msg.id,
        "whatsapp_result": result,
    }
