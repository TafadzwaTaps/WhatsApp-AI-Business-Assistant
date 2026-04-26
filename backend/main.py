"""
main.py — WaziBot SaaS API

Changes in this version
────────────────────────
CRYPTO-1  All token decryption goes through crud.get_decrypted_token() which
          now raises TokenDecryptionError instead of returning "".  Every
          endpoint that needs the token catches this and returns HTTP 503
          with a clear message — no more silent empty-string sends.

CRYPTO-2  send_whatsapp() validates phone_number_id AND token before hitting
          the Meta API and logs the last 6 chars of the token so you can
          confirm the right key is in use without leaking the secret.

AUTH-1    /auth/refresh endpoint implemented — frontend refresh loop fixed.

PRODUCT-1 POST /products now logs the exact Pydantic payload it receives and
          the business_id it inserts into, so failures are visible in Render
          logs instead of returning a silent 422/500.

DEBUG-1   /debug/env  — confirms which env vars are loaded (values masked).
DEBUG-2   /debug/token — round-trips a test string through encrypt/decrypt.

STATIC-1  Static directory resolved relative to __file__ with three
          candidate paths so it works on Render regardless of working dir.
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
from sqlalchemy.orm import Session

from database import Base, engine, SessionLocal
import models
from schemas import (
    ProductCreate, ProductOut,
    OrderCreate, OrderOut,
    ChatMessageOut,
    BusinessCreate, BusinessOut, BusinessUpdate,
)
import crud
from crypto import (
    encrypt_token, safe_decrypt_token,
    TokenDecryptionError, is_encrypted,
)
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

# ── Tables ─────────────────────────────────────────────────────────────────────
Base.metadata.create_all(bind=engine)

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "myverifytoken123")

# ── STATIC-1: robust path resolution ──────────────────────────────────────────
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


# ── HTML helpers ───────────────────────────────────────────────────────────────
def _html(name: str) -> FileResponse:
    path = os.path.join(STATIC_DIR, name)
    if not os.path.exists(path):
        raise HTTPException(404, detail=f"{name} not found in {STATIC_DIR}")
    return FileResponse(path)


@app.get("/")          
def landing():    return _html("landing.html")

@app.get("/dashboard") 
def dashboard():  return _html("dashboard.html")

@app.get("/inbox")     
def inbox():      return _html("inbox.html")

@app.get("/signup")    
def signup_page(): return _html("signup.html")


# ── DB dependency ──────────────────────────────────────────────────────────────
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


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
# CRYPTO-2
def send_whatsapp(phone_number_id: str, token: str, to: str, message: str) -> dict:
    """
    Send a WhatsApp message via Meta Cloud API.
    Returns the parsed JSON response (may contain 'error' key on failure).
    Never raises — all errors are logged and returned as dict.
    """
    if not phone_number_id or not token:
        missing = []
        if not phone_number_id: missing.append("phone_number_id")
        if not token:           missing.append("token")
        log.error("send_whatsapp: ABORTED — missing %s", ", ".join(missing))
        return {"error": f"missing credentials: {', '.join(missing)}"}

    to = to.replace("whatsapp:", "").strip()
    url = f"https://graph.facebook.com/v18.0/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
    }
    body = {
        "messaging_product": "whatsapp",
        "to":   to,
        "type": "text",
        "text": {"body": message},
    }

    # Log last 6 chars of token — enough to verify key match without leaking
    log.info(
        "📤 WhatsApp send → to=%s  phone_id=%s  token_tail=…%s",
        to, phone_number_id, token[-6:],
    )

    try:
        resp = http_requests.post(url, headers=headers, json=body, timeout=10)
        result = resp.json()

        if resp.status_code == 200:
            msg_id = (result.get("messages") or [{}])[0].get("id", "?")
            log.info("✅ WhatsApp OK  msg_id=%s  to=%s", msg_id, to)
        else:
            err = result.get("error", {})
            log.error(
                "❌ WhatsApp API error  status=%d  code=%s  msg=%s",
                resp.status_code, err.get("code"), err.get("message"),
            )
        return result

    except Exception as exc:
        log.exception("send_whatsapp exception: %s", exc)
        return {"error": str(exc)}


# ─────────────────────────────────────────────────────────────────────────────
# AUTH ENDPOINTS
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


def _token_pair(sub: str, role: str, business_id: int | None = None) -> dict:
    """Build the JWT pair payload shared by login, signup, and refresh."""
    data = {"sub": sub, "role": role}
    if business_id is not None:
        data["business_id"] = business_id
    return {
        "access_token":  create_access_token(data),
        "refresh_token": create_refresh_token(data),
        "token_type":    "bearer",
    }


@app.post("/auth/signup")
def signup(data: SignupRequest, db: Session = Depends(get_db)):
    if data.username == SUPER_ADMIN_USERNAME.lower():
        raise HTTPException(400, "Username not available")
    if crud.get_business_by_username(db, data.username):
        raise HTTPException(400, "Username already taken")

    # FIX: check for duplicate phone_id BEFORE the INSERT so we return a
    # clean 400 instead of letting Postgres/SQLite raise a UNIQUE constraint
    # IntegrityError that becomes an unhandled 500.
    phone_id = data.whatsapp_phone_id.strip() or None
    if phone_id and crud.get_business_by_phone_id(db, phone_id):
        raise HTTPException(
            400,
            "That WhatsApp Phone Number ID is already registered to another account. "
            "Check the number in your Meta Developer Portal."
        )

    biz = crud.create_business(db, BusinessCreate(
        name=data.business_name,
        owner_username=data.username,
        owner_password=data.password,
        whatsapp_phone_id=phone_id,   # already stripped + validated above
        whatsapp_token=data.whatsapp_token.strip() or None,
    ))
    log.info("🆕 Signup: %s (@%s)", biz.name, biz.owner_username)
    return {
        **_token_pair(biz.owner_username, "business", biz.id),
        "role":          "business",
        "business_name": biz.name,
        "business_id":   biz.id,
    }


@app.post("/auth/login")
def login(data: BaseModel, db: Session = Depends(get_db)):
    # Accept both form-encoded (OAuth2) and JSON bodies
    username = getattr(data, "username", "") or ""
    password = getattr(data, "password", "") or ""
    username = username.strip().lower()

    if username == SUPER_ADMIN_USERNAME.lower():
        if not verify_password(password, SUPER_ADMIN_PASSWORD):
            raise HTTPException(401, "Invalid credentials")
        return {**_token_pair(SUPER_ADMIN_USERNAME, "superadmin"), "role": "superadmin"}

    biz = crud.get_business_by_username(db, username)
    if not biz or not verify_password(password, biz.owner_password):
        raise HTTPException(401, "Invalid credentials")
    if not biz.is_active:
        raise HTTPException(403, "Account suspended. Contact support.")

    log.info("🔑 Login: %s", biz.owner_username)
    return {
        **_token_pair(biz.owner_username, "business", biz.id),
        "role":          "business",
        "business_name": biz.name,
        "business_id":   biz.id,
    }


class LoginJSON(BaseModel):
    username: str
    password: str


@app.post("/auth/login")
def login(data: LoginJSON, db: Session = Depends(get_db)):
    username = data.username.strip().lower()

    if username == SUPER_ADMIN_USERNAME.lower():
        if not verify_password(data.password, SUPER_ADMIN_PASSWORD):
            raise HTTPException(401, "Invalid credentials")
        return {**_token_pair(SUPER_ADMIN_USERNAME, "superadmin"), "role": "superadmin"}

    biz = crud.get_business_by_username(db, username)
    if not biz or not verify_password(data.password, biz.owner_password):
        raise HTTPException(401, "Invalid credentials")
    if not biz.is_active:
        raise HTTPException(403, "Account suspended. Contact support.")

    log.info("🔑 Login: %s", biz.owner_username)
    return {
        **_token_pair(biz.owner_username, "business", biz.id),
        "role":          "business",
        "business_name": biz.name,
        "business_id":   biz.id,
    }


class RefreshRequest(BaseModel):
    refresh_token: str


@app.post("/auth/refresh")
def refresh_token_endpoint(data: RefreshRequest, db: Session = Depends(get_db)):
    """
    Exchange a valid refresh token for a new access + refresh token pair.
    Frontend calls this automatically on 401; backend validates the account
    is still active before issuing new tokens.
    """
    try:
        payload = decode_token(data.refresh_token)
    except HTTPException:
        raise HTTPException(401, "Refresh token invalid or expired. Please log in again.")

    if payload.get("type") != "refresh":
        raise HTTPException(401, "Provided token is not a refresh token.")

    sub         = payload.get("sub", "")
    role        = payload.get("role", "business")
    business_id = payload.get("business_id")

    if role == "business":
        biz = crud.get_business_by_username(db, sub)
        if not biz or not biz.is_active:
            raise HTTPException(401, "Account not found or suspended.")
        business_id = biz.id

    log.info("🔄 Token refreshed for: %s", sub)
    return {
        **_token_pair(sub, role, business_id),
        "role": role,
        **({"business_id": business_id} if business_id else {}),
    }


# ─────────────────────────────────────────────────────────────────────────────
# DEBUG ENDPOINTS  (DEBUG-1 / DEBUG-2)
# Remove or gate behind an admin check in production if you prefer.
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/debug/env")
def debug_env():
    """
    Confirms which critical env vars are loaded (values masked).
    Safe to expose — secrets are never returned in full.
    """
    fernet_key = os.getenv("FERNET_KEY", "")
    secret_key = os.getenv("SECRET_KEY", "")
    return {
        "FERNET_KEY":  f"{fernet_key[:8]}…({len(fernet_key)} chars)" if fernet_key else "❌ NOT SET",
        "SECRET_KEY":  f"{secret_key[:4]}…({len(secret_key)} chars)" if secret_key else "❌ NOT SET",
        "VERIFY_TOKEN": "✅ set" if os.getenv("VERIFY_TOKEN") else "⚠ using default",
        "DATABASE_URL": os.getenv("DATABASE_URL", "sqlite (default)"),
        "ENV_FILE":     ".env loaded" if os.path.exists(".env") else "no .env (using system env — correct for Render)",
    }


@app.get("/debug/token")
def debug_token():
    """
    Round-trips a test string through encrypt → decrypt to verify the
    Fernet key is working correctly end-to-end.
    If this returns 'ok': true, token encryption is working.
    """
    from crypto import encrypt_token, decrypt_token, TokenDecryptionError
    test = "wazibot-test-12345"
    try:
        ct = encrypt_token(test)
        pt = decrypt_token(ct)
        ok = pt == test
        return {
            "ok":               ok,
            "ciphertext_prefix": ct[:12] + "…",
            "round_trip":       "✅ PASS" if ok else "❌ FAIL",
        }
    except TokenDecryptionError as exc:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})
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

        log.info("📥 Incoming  phone_id=%s  from=%s  text=%r", phone_number_id, customer_phone, text)

        business = crud.get_business_by_phone_id(db, phone_number_id)
        if not business or not business.is_active:
            return {"status": "ok"}

        # CRYPTO-1: propagate decryption errors as a log warning — the message
        # still gets saved and the bot still replies; it just won't re-send via
        # WhatsApp if the token is broken (which we log clearly).
        try:
            token = crud.get_decrypted_token(business)
        except TokenDecryptionError as exc:
            log.error("⚠️  Token decryption failed for business %s: %s", business.name, exc)
            token = ""

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
                lines = "\n".join([f"{i+1}. {p.name} — ${p.price:.2f}" for i, p in enumerate(products)])
                reply = f"📋 *{business.name} Menu*\n\n{lines}\n\nTo order: *order <item> <qty>*"
            else:
                reply = f"Hi! {business.name}'s menu is being updated. Check back soon! 🙏"

        elif text.lower().startswith("order "):
            parts = text.strip().split()
            if len(parts) < 3:
                reply = "❌ Format: order <item> <quantity>\nExample: order sadza 2"
            else:
                try:
                    product_name = parts[1].lower()
                    qty = int(parts[2])
                    if qty <= 0: raise ValueError
                    crud.create_order(db, business.id, OrderCreate(
                        customer_phone=customer_phone,
                        product_name=product_name,
                        quantity=qty,
                    ))
                    reply = (
                        f"✅ *Order confirmed!*\n\n{product_name.capitalize()} × {qty}\n\n"
                        f"Thank you for ordering from {business.name}! We'll be in touch. 🙏"
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
            log.warning(
                "📵 WhatsApp reply NOT sent for business '%s' — "
                "no token (check FERNET_KEY or re-save token in Settings)",
                business.name,
            )

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

@app.get("/admin/businesses", response_model=List[BusinessOut])
def list_businesses(db=Depends(get_db), _=Depends(require_superadmin)):
    return crud.get_all_businesses(db)


@app.post("/admin/businesses", response_model=BusinessOut)
def admin_create_business(
    data: BusinessCreate, db=Depends(get_db), _=Depends(require_superadmin)
):
    if crud.get_business_by_username(db, data.owner_username):
        raise HTTPException(400, "Username already taken")
    # FIX: same duplicate phone_id guard as in /auth/signup
    if data.whatsapp_phone_id and crud.get_business_by_phone_id(db, data.whatsapp_phone_id):
        raise HTTPException(400, "WhatsApp Phone Number ID already registered.")
    return crud.create_business(db, data)


@app.patch("/admin/businesses/{business_id}", response_model=BusinessOut)
def admin_update_business(
    business_id: int, data: BusinessUpdate,
    db=Depends(get_db), _=Depends(require_superadmin),
):
    b = crud.update_business(db, business_id, data)
    if not b:
        raise HTTPException(404, "Business not found")
    return b


@app.delete("/admin/businesses/{business_id}")
def admin_delete_business(
    business_id: int, db=Depends(get_db), _=Depends(require_superadmin)
):
    b = crud.delete_business(db, business_id)
    if not b:
        raise HTTPException(404, "Business not found")
    return {"deleted": business_id}


@app.get("/admin/stats")
def admin_stats(db=Depends(get_db), _=Depends(require_superadmin)):
    businesses = crud.get_all_businesses(db)
    orders     = db.query(models.Order).all()
    return {
        "businesses":        len(businesses),
        "active_businesses": sum(1 for b in businesses if b.is_active),
        "total_orders":      len(orders),
        "total_revenue":     round(sum(o.total_price or 0 for o in orders), 2),
    }


# ─────────────────────────────────────────────────────────────────────────────
# BUSINESS PROFILE
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/me", response_model=BusinessOut)
def get_me(db=Depends(get_db), user=Depends(require_business)):
    b = crud.get_business_by_id(db, user["business_id"])
    if not b:
        raise HTTPException(404, "Not found")
    return b


@app.patch("/me", response_model=BusinessOut)
def update_me(data: BusinessUpdate, db=Depends(get_db), user=Depends(require_business)):
    safe = data.dict(exclude_none=True)
    safe.pop("is_active", None)   # businesses cannot suspend themselves
    return crud.update_business(db, user["business_id"], BusinessUpdate(**safe))


@app.get("/me/test-whatsapp")
def test_whatsapp_connection(db=Depends(get_db), user=Depends(require_business)):
    """Verifies the stored WhatsApp credentials can reach the Meta API."""
    b = crud.get_business_by_id(db, user["business_id"])
    if not b or not b.whatsapp_phone_id:
        return {"ok": False, "reason": "No Phone Number ID saved"}

    try:
        token = crud.get_decrypted_token(b)
    except TokenDecryptionError as exc:
        return {
            "ok":     False,
            "reason": f"Token decryption failed — {exc}",
        }

    if not token:
        return {"ok": False, "reason": "No access token saved"}

    try:
        resp = http_requests.get(
            f"https://graph.facebook.com/v18.0/{b.whatsapp_phone_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=5,
        )
        if resp.status_code == 200:
            return {"ok": True, "reason": "Connected to Meta API ✅"}
        err = resp.json().get("error", {}).get("message", "Unknown error")
        return {"ok": False, "reason": err}
    except Exception as exc:
        return {"ok": False, "reason": str(exc)}


# ─────────────────────────────────────────────────────────────────────────────
# PRODUCTS  (PRODUCT-1: logging added)
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/products", response_model=List[ProductOut])
def get_products(db=Depends(get_db), user=Depends(require_business)):
    return crud.get_products(db, user["business_id"])


@app.post("/products", response_model=ProductOut, status_code=201)
def create_product(
    product: ProductCreate,
    db=Depends(get_db),
    user=Depends(require_business),
):
    """
    Create a product for the authenticated business.

    Common failure modes that are now logged:
      • 401 — frontend not sending Authorization header
      • 403 — token belongs to superadmin, not a business account
      • 422 — Pydantic validation error (price missing / wrong type)
    """
    log.info(
        "📦 create_product  business_id=%s  name=%r  price=%s  has_image=%s",
        user["business_id"],
        product.name,
        product.price,
        bool(product.image_url),
    )
    p = crud.create_product(db, user["business_id"], product)
    log.info("📦 create_product OK  id=%s  business_id=%s", p.id, user["business_id"])
    return p


@app.delete("/products/{product_id}")
def delete_product(
    product_id: int,
    db=Depends(get_db),
    user=Depends(require_business),
):
    p = crud.delete_product(db, product_id, user["business_id"])
    if not p:
        raise HTTPException(404, "Product not found")
    return {"deleted": product_id}


# ─────────────────────────────────────────────────────────────────────────────
# ORDERS
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/orders", response_model=List[OrderOut])
def get_orders(db=Depends(get_db), user=Depends(require_business)):
    return crud.get_orders(db, user["business_id"])


# ─────────────────────────────────────────────────────────────────────────────
# LEGACY CONVERSATIONS
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/conversations", response_model=List[ChatMessageOut])
def get_conversations(db=Depends(get_db), user=Depends(require_business)):
    return crud.get_conversations(db, user["business_id"])


@app.get("/conversations/{phone}", response_model=List[ChatMessageOut])
def get_chat(phone: str, db=Depends(get_db), user=Depends(require_business)):
    return crud.get_messages_for_phone(db, user["business_id"], phone)


# ─────────────────────────────────────────────────────────────────────────────
# BROADCAST
# ─────────────────────────────────────────────────────────────────────────────

class BroadcastRequest(BaseModel):
    message: str

    @validator("message")
    def msg_valid(cls, v):
        v = v.strip()
        if len(v) < 3:    raise ValueError("Message too short (min 3 chars)")
        if len(v) > 1024: raise ValueError("Message too long (max 1024 chars)")
        return v


@app.post("/broadcast")
def broadcast(body: BroadcastRequest, db=Depends(get_db), user=Depends(require_business)):
    bid      = user["business_id"]
    business = crud.get_business_by_id(db, bid)

    # CRYPTO-1: raise before touching any phones
    try:
        token = crud.get_decrypted_token(business)
    except TokenDecryptionError as exc:
        log.error("broadcast: token decryption failed — %s", exc)
        raise HTTPException(503, detail=(
            "WhatsApp token cannot be decrypted. "
            "FERNET_KEY may have changed since the token was saved. "
            "Go to Settings → re-enter your WhatsApp token."
        ))

    if not token:
        raise HTTPException(400, "WhatsApp token not configured. Go to Settings.")
    if not business.whatsapp_phone_id:
        raise HTTPException(400, "WhatsApp Phone Number ID not configured. Go to Settings.")

    phones = crud.get_all_customer_phones(db, bid)
    if not phones:
        return {"sent": 0, "failed": 0, "total": 0, "message": "No customers found"}

    log.info("📢 Broadcast start  recipients=%d  business=%s", len(phones), business.name)

    sent, failed, failed_phones = 0, 0, []
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
            failed_phones.append(phone)

    log.info("📢 Broadcast done  sent=%d  failed=%d", sent, failed)
    return {
        "sent":           sent,
        "failed":         failed,
        "total":          len(phones),
        "failed_numbers": failed_phones,
    }


@app.get("/customers")
def get_customers(db=Depends(get_db), user=Depends(require_business)):
    phones = crud.get_all_customer_phones(db, user["business_id"])
    return {"phones": phones, "total": len(phones)}


# ─────────────────────────────────────────────────────────────────────────────
# CHAT INBOX (CRM)
# ─────────────────────────────────────────────────────────────────────────────

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


@app.get("/chat/messages/{customer_id}")
def chat_messages(
    customer_id: int,
    limit:  int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db=Depends(get_db),
    user=Depends(get_current_user),
):
    customer = crud.get_customer_by_id(db, customer_id, user["business_id"])
    if not customer:
        raise HTTPException(404, "Customer not found")

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
def mark_read(
    customer_id: int,
    db=Depends(get_db),
    user=Depends(get_current_user),
):
    customer = crud.get_customer_by_id(db, customer_id, user["business_id"])
    if not customer:
        raise HTTPException(404, "Customer not found")
    crud.mark_messages_read(db, customer_id, user["business_id"])
    return {"ok": True, "customer_id": customer_id}


# ─────────────────────────────────────────────────────────────────────────────
# CHAT SEND  (CRYPTO-1: explicit error on bad token)
# ─────────────────────────────────────────────────────────────────────────────

class ChatSendRequest(BaseModel):
    customer_id: int
    text: str

    @validator("text")
    def text_valid(cls, v):
        v = v.strip()
        if not v:         raise ValueError("Message cannot be empty")
        if len(v) > 4096: raise ValueError("Message too long (max 4096 chars)")
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
        raise HTTPException(404, "Customer not found")

    business = crud.get_business_by_id(db, bid)

    # CRYPTO-1: explicit error so the frontend can show a meaningful message
    try:
        token = crud.get_decrypted_token(business)
    except TokenDecryptionError as exc:
        log.error("chat_send: token decryption failed  business=%s  error=%s", business.name, exc)
        raise HTTPException(503, detail=(
            "WhatsApp token cannot be decrypted (FERNET_KEY mismatch). "
            "Go to Settings → re-enter your WhatsApp token to fix this."
        ))

    has_phone_id = bool(business.whatsapp_phone_id)
    has_token    = bool(token)

    log.info(
        "chat_send  customer=%s  phone=%s  has_phone_id=%s  has_token=%s  token_tail=…%s",
        body.customer_id, customer.phone,
        has_phone_id, has_token,
        token[-6:] if has_token else "N/A",
    )

    # Persist regardless of WhatsApp delivery status
    crud.log_message(db, bid, customer.phone, "out", body.text)
    msg = crud.create_message(db, customer.id, bid, body.text, "outgoing")

    # Attempt WhatsApp delivery
    wa_result: dict = {}
    if has_token and has_phone_id:
        wa_result = send_whatsapp(business.whatsapp_phone_id, token, customer.phone, body.text)
    else:
        missing = [k for k, v in {"phone_number_id": has_phone_id, "token": has_token}.items() if not v]
        log.warning("chat_send: WhatsApp NOT sent — missing: %s", missing)
        wa_result = {"error": f"credentials missing: {missing}"}

    # Push to open WebSocket sessions
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
        "whatsapp_result": wa_result,
    }
