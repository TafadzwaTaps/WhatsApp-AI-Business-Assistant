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
# payments.py — see payments.py for available functions

load_dotenv()

# ── Event logger (non-blocking, structured) ───────────────────────────────────
# Used to record AI requests, WhatsApp events, and payment events.
# Import is lazy inside functions to avoid circular import with services/.
def _log_event(event_type: str, **fields) -> None:
    """
    Log a structured business event.  Never raises — observability must
    not affect the main request path.  Fields are logged as key=value pairs.

    event_type examples:
      "wa.sent"        — WhatsApp message successfully sent
      "wa.failed"      — WhatsApp send failure
      "ai.request"     — AI generate_reply called
      "ai.suppressed"  — AI returned empty reply (intentional)
      "payment.event"  — Payment status change
      "order.event"    — Order lifecycle change
    """
    try:
        kv = "  ".join(f"{k}={v!r}" for k, v in fields.items())
        log.info("EVENT %s  %s", event_type, kv)
    except Exception:
        pass   # logging must never crash the caller


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("wazibot")

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "myverifytoken123")

# ── Shared WhatsApp number platform config ─────────────────────────────────────
# When SHARED_PHONE_NUMBER_ID is set, all businesses share ONE WhatsApp number.
# The webhook routes messages to the correct business via customer session.
SHARED_PHONE_NUMBER_ID = os.getenv("SHARED_PHONE_NUMBER_ID", "").strip()
SHARED_WA_TOKEN        = os.getenv("SHARED_WA_TOKEN", "").strip()
SHARED_WA_PHONE        = os.getenv("SHARED_WA_PHONE", "WaziBot").strip()

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
    # Note: event logging happens at the call sites (STEP 8) to include context.
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

def _send_direct(phone_number_id: str, token: str, to: str, message: str) -> None:
    """
    Send a WhatsApp message directly (bypassing the normal reply flow).
    Used for the business picker, switch confirmations, etc.
    Logs errors but never raises.
    """
    if not token:
        log.warning("_send_direct: no token set — cannot send to %s", to)
        return
    try:
        result = send_whatsapp(phone_number_id, token, to, message)
        if "error" in result:
            log.error("_send_direct error: %s", result["error"])
        else:
            log.info("_send_direct OK  to=%s", to)
    except Exception as exc:
        log.error("_send_direct exception: %s", exc)


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
    business_name:      str
    username:           str
    password:           str
    whatsapp_phone_id:  str = ""        # optional — leave empty to use shared number
    whatsapp_token:     str = ""        # optional — leave empty to use shared number
    # New optional onboarding fields
    category:          str = ""        # e.g. "Food & Beverage", "Electronics"
    contact_phone:     str = ""        # business contact number (not WhatsApp API)
    use_shared_number: bool = True     # defaults to True — no Meta setup needed

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
        # New onboarding fields
        category           = data.category.strip() if data.category else ""
        contact_phone      = data.contact_phone.strip() if data.contact_phone else ""
        use_shared_number  = data.use_shared_number

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


@app.get("/debug/supabase")
def debug_supabase():
    """
    Test Supabase connectivity without requiring a login token.
    Visit: https://wazibot-api-assistant.onrender.com/debug/supabase
    Returns clear diagnostics if the connection is broken.
    """
    import os, re
    url = os.getenv("SUPABASE_URL", "").strip()
    key = os.getenv("SUPABASE_KEY", "").strip()

    issues = []

    if not url:
        issues.append("SUPABASE_URL is not set")
    elif "xxxx" in url.lower():
        issues.append(f"SUPABASE_URL still contains placeholder: {url!r}")
    elif not url.startswith("https://"):
        issues.append(f"SUPABASE_URL must start with https://. Got: {url!r}")
    elif not re.search(r"https://[a-z0-9]+\.supabase\.co", url.rstrip("/")):
        issues.append(f"SUPABASE_URL format invalid: {url!r}")

    if not key:
        issues.append("SUPABASE_KEY is not set")
    elif not key.startswith("eyJ"):
        issues.append(f"SUPABASE_KEY does not look like a JWT (prefix: {key[:8]!r})")

    if issues:
        return {
            "ok":     False,
            "issues": issues,
            "action": "Fix the above in Render → Your Service → Environment, then redeploy.",
        }

    # Try an actual query — select one row from businesses
    try:
        from db import supabase as _sb
        res = _sb.table("businesses").select("id").limit(1).execute()
        return {
            "ok":      True,
            "message": "Supabase connection working ✅",
            "url":     url[:50] + "…",
            "rows":    len(res.data),
        }
    except Exception as exc:
        return {
            "ok":     False,
            "error":  str(exc),
            "url":    url[:50] + "…",
            "action": (
                "Connection failed even though env vars look correct. "
                "Check that your Supabase project is active (not paused) at "
                "https://supabase.com/dashboard"
            ),
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


def _get_customer_state_for_log(phone: str, business_id: int) -> str:
    """Lightweight state read for logging — never raises."""
    try:
        from db import supabase as _sb
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

        # Supported message types
        SUPPORTED_TYPES = ("text", "image", "document", "sticker", "audio")
        if msg_type not in SUPPORTED_TYPES:
            log.info("Webhook: skipping unsupported message type=%s", msg_type)
            return {"status": "ok"}

        # Handle image/document — customer may be sending payment proof
        if msg_type in ("image", "document", "sticker"):
            img_obj = msg_obj.get(msg_type, {})
            text = img_obj.get("caption", "").strip() or "[image]"

        # Handle audio / voice notes — architecture hook for future Whisper integration
        elif msg_type == "audio":
            audio_obj = msg_obj.get("audio", {})
            audio_id  = audio_obj.get("id", "")
            log.info("Webhook: voice note received  audio_id=%s  from=%s", audio_id, customer_phone)
            # For now: acknowledge the voice note gracefully.
            # When OPENAI_API_KEY is set, POST to /voice/transcribe to process.
            # The customer gets a friendly reply asking them to type their order.
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

    # ── STEP 2: Find business (shared-number-aware) ──────────────────────
    try:
        from tenant_router import (
            is_shared_number, resolve_business_for_shared_number,
            is_switch_request,
        )

        if is_shared_number(phone_number_id):
            # ── SHARED NUMBER PATH ────────────────────────────────────────────
            log.info("📋 STEP 2 — shared number path  phone=%s", customer_phone)
            active_businesses = crud.get_active_businesses()

            business, direct_reply = resolve_business_for_shared_number(
                phone=customer_phone,
                text=text,
                active_businesses=active_businesses,
            )

            if direct_reply:
                # Send the picker or error message directly — no AI needed
                _send_direct(phone_number_id, SHARED_WA_TOKEN, customer_phone, direct_reply)
                # Save messages for inbox
                try:
                    cust_any = crud.get_or_create_customer(customer_phone, 0)
                    crud.create_message(cust_any["id"], 0, text, "incoming",
                                        wa_message_id=wa_message_id)
                    crud.create_message(cust_any["id"], 0, direct_reply, "outgoing")
                except Exception:
                    pass
                return {"status": "ok"}

            if not business:
                log.error("📋 STEP 2 — no business resolved for shared number")
                return {"status": "ok"}

            # Token for shared number is the platform token
            token = SHARED_WA_TOKEN
            log.info("📋 STEP 2 OK (shared)  biz_id=%s  name=%s", business["id"], business["name"])

        else:
            # ── PER-BUSINESS NUMBER PATH (existing logic, unchanged) ──────────
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
    try:
        if not is_shared_number(phone_number_id):
            # For per-business numbers, token was set in step 2 above only for shared.
            # Load it now for per-business path.
            token = ""
        if "token" not in dir():
            token = ""
    except Exception:
        token = ""

    if not is_shared_number(phone_number_id):
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

        # Detect image/media messages — customer may be sending payment proof
        message_has_image = (
            msg_type in ("image", "document", "sticker")
            or "image" in value.get("messages", [{}])[0]
        )
        # Detect voice notes — special handling
        is_voice_note = (msg_type == "audio")
        if is_voice_note and text == "[voice_note]":
            # Return a friendly voice note response without calling generate_reply
            # (Full Whisper integration available via /voice/transcribe endpoint)
            reply = (
                "🎤 I heard your voice note! Unfortunately I can't process audio yet.\n\n"
                "Could you type your order instead? Type *menu* to get started! 😊"
            )
            log.info("📤 voice note fallback sent  phone=%s", customer_phone)
            # Skip to STEP 7 (save outgoing) and STEP 8 (send)
            # We set products=[] so we can skip straight to send
            try:
                out_msg = crud.create_message(
                    customer["id"], business["id"], reply, "outgoing",
                    sender_type="ai",
                )
                log.info("💾 STEP 7 voice-note-reply  id=%s", out_msg.get("id", "?"))
            except Exception as exc:
                log.warning("STEP 7 voice-note-reply failed: %s", exc)
            if token:
                _r = send_whatsapp(phone_number_id, token, customer_phone, reply)
                _log_event("wa.voice_reply", phone=customer_phone, biz=business["id"])
            return {"status": "ok"}

        # Detect agent-echo messages.
        # Meta webhooks already filter delivery `statuses` events at line 519,
        # so by the time we get here all messages are genuine customer-sent.
        # message_is_from_agent=True only when the message `from` field exactly
        # matches the business's registered WhatsApp phone_number_id — this
        # happens when an agent sends directly from the business WhatsApp account
        # and Meta echoes it back through the webhook.
        business_phone_id = business.get("whatsapp_phone_id", "")
        msg_from          = msg_obj.get("from", "")
        is_from_agent     = bool(
            business_phone_id
            and msg_from
            and msg_from == business_phone_id
        )
        if is_from_agent:
            log.info(
                "📩 STEP 6 — agent-echo detected  from=%s  biz_phone_id=%s",
                msg_from, business_phone_id,
            )

        reply = generate_reply(
            message=text,
            phone=customer_phone,
            business_id=business["id"],
            business_name=business["name"],
            products=products,
            message_has_image=message_has_image,
            message_is_from_agent=is_from_agent,
        )
        log.info("🤖 STEP 6 — AI reply generated  len=%d", len(reply))
        _log_event(
            "ai.request" if reply else "ai.suppressed",
            phone=customer_phone,
            biz=business["id"],
            msg_len=len(text),
            reply_len=len(reply),
        )

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
        out_msg = crud.create_message(
            customer["id"], business["id"], reply, "outgoing",
            sender_type="ai",   # AI-generated — shown with AI badge in inbox
        )
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
    # Empty reply handling
    if not reply:
        # Log the suppression with state context for debugging
        log.info(
            "📤 STEP 8 SKIP — empty reply  phone=%s  state=%s  is_from_agent=%s  "
            "reason=agent_echo_or_handoff_silent",
            customer_phone,
            _get_customer_state_for_log(customer_phone, business["id"]),
            is_from_agent,
        )
        return {"status": "ok"}

    if token:
        result = send_whatsapp(phone_number_id, token, customer_phone, reply)
        if "error" in result:
            log.error("📤 STEP 8 FAIL — WhatsApp error: %s", result["error"])
            _log_event("wa.failed", phone=customer_phone, biz=business["id"],
                       error=result["error"])
        else:
            msg_id = (result.get("messages") or [{}])[0].get("id", "?")
            log.info("📤 STEP 8 OK  msg_id=%s", msg_id)
            _log_event("wa.sent", phone=customer_phone, biz=business["id"],
                       msg_id=msg_id, reply_len=len(reply))
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

class PaymentConfirmRequest(BaseModel):
    reference: str   # e.g. ORDER-12
    amount: float


@app.post("/payment/webhook")
async def payment_webhook(data: PaymentConfirmRequest):
    """
    Manual payment confirmation endpoint.
    Business owner (or a future gateway) calls this to mark an order as paid.

    Payload: { "reference": "ORDER-12", "amount": 10.00 }
    """
    from order_lifecycle import get_order

    reference = (data.reference or "").strip().upper()
    if not reference.startswith("ORDER-"):
        raise HTTPException(400, f"Invalid reference format. Expected ORDER-{{id}}, got: {reference}")

    try:
        order_id = int(reference.split("-")[1])
    except (IndexError, ValueError):
        raise HTTPException(400, f"Cannot parse order ID from reference: {reference}")

    order = get_order(order_id)
    if not order:
        raise HTTPException(404, f"Order {order_id} not found")

    # Validate amount
    order_total = float(order.get("total_price") or 0)
    if round(float(data.amount), 2) != round(order_total, 2):
        raise HTTPException(400,
            f"Amount mismatch: expected ${order_total:.2f}, received ${data.amount:.2f}")

    if order.get("payment_status") == "paid":
        return {"success": True, "message": f"Order {order_id} is already marked paid.", "order_id": order_id}

    # Mark as paid
    biz_id = order.get("business_id")
    crud.update_order_payment(order_id, biz_id, {
        "payment_status":    "paid",
        "payment_reference": reference,
    })
    try:
        from order_lifecycle import update_order_status_supabase
        update_order_status_supabase(order_id, "paid")
    except Exception:
        pass

    # Notify customer via WhatsApp
    phone = order.get("customer_phone", "")
    if phone and biz_id:
        try:
            business = crud.get_business_by_id(biz_id)
            if business:
                token    = crud.get_decrypted_token(business)
                phone_id = business.get("whatsapp_phone_id")
                biz_name = business.get("name", "")
                if token and phone_id:
                    send_whatsapp(phone_id, token, phone,
                        f"✅ *Payment Confirmed!*\n\n"
                        f"Thank you! Your payment for *{reference}* has been verified.\n\n"
                        f"💰 Amount: ${order_total:.2f}\n"
                        f"📦 Your order is now being prepared. Thank you for shopping with *{biz_name}*! 🙏"
                    )
        except Exception as exc:
            log.exception("payment_webhook notify error: %s", exc)

    log.info("payment confirmed  order=%s  amount=%.2f", order_id, data.amount)
    return {"success": True, "order_id": order_id, "reference": reference,
            "message": f"Payment confirmed for {reference}"}


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
# ORDER LIFECYCLE — business pushes status updates + notifies customer via WA
# ─────────────────────────────────────────────────────────────────────────────

class OrderLifecycleUpdate(BaseModel):
    order_id:         int
    status:           str   # preparing | ready | out_for_delivery | delivered | completed
    message:          Optional[str] = None    # custom message to customer (optional)
    estimated_minutes: Optional[int] = None  # ETA in minutes for delivery/prep


# Human-readable status messages sent to the customer automatically
_LIFECYCLE_MESSAGES = {
    "preparing": (
        "👨‍🍳 *Your order is being prepared!*\n\n"
        "📦 Order: *{ref}*\n\n"
        "We're working on it now. Estimated preparation time: *{eta}*\n\n"
        "_We'll let you know when it's ready! 😊_"
    ),
    "ready": (
        "🎉 *Your order is ready!*\n\n"
        "📦 Order: *{ref}*\n\n"
        "Your order is ready for *pickup* or will be dispatched for delivery shortly.\n\n"
        "_Please come collect or wait for your delivery rider. 🙌_"
    ),
    "out_for_delivery": (
        "🛵 *Your order is on the way!*\n\n"
        "📦 Order: *{ref}*\n\n"
        "Your delivery rider has been assigned and is heading your way.\n"
        "{eta}"
        "\n_Please be available to receive your order. 😊_"
    ),
    "delivered": (
        "✅ *Order delivered! Enjoy your meal!*\n\n"
        "📦 Order: *{ref}*\n\n"
        "Thank you for ordering from *{biz}*! 🙏\n\n"
        "_We hope you love it! Type *menu* to order again anytime._"
    ),
    "completed": (
        "✅ *Order completed! Thank you!*\n\n"
        "📦 Order: *{ref}*\n\n"
        "We hope you enjoyed your order from *{biz}*! 🙏\n\n"
        "_Type *menu* to order again._"
    ),
}


@app.post("/orders/{order_id}/lifecycle")
async def push_lifecycle_update(
    order_id: int,
    data: OrderLifecycleUpdate,
    user=Depends(require_business),
):
    """
    Business-facing endpoint: push an order lifecycle status update.

    Automatically:
      1. Updates order status in Supabase
      2. Sends a WhatsApp message to the customer with the new status
      3. Triggers end-of-conversation survey if status is delivered/completed

    Status values: preparing | ready | out_for_delivery | delivered | completed
    """
    from order_lifecycle import get_order

    bid   = user["business_id"]
    order = get_order(order_id)

    if not order:
        raise HTTPException(404, f"Order {order_id} not found")
    if order.get("business_id") != bid:
        raise HTTPException(403, "Access denied")

    new_status = data.status.lower().strip()
    valid_push_statuses = {
        "preparing", "ready", "out_for_delivery", "delivered", "completed"
    }
    if new_status not in valid_push_statuses:
        raise HTTPException(422,
            f"Invalid status '{new_status}'. Valid: {sorted(valid_push_statuses)}")

    # ── Update DB ─────────────────────────────────────────────────────────────
    try:
        crud.update_order_payment(order_id, bid, {"payment_status": "paid"})
    except Exception:
        pass  # may already be paid

    # Map our push status to the VALID_STATUSES in order_lifecycle
    status_map = {
        "preparing":        "confirmed",
        "ready":            "confirmed",
        "out_for_delivery": "confirmed",
        "delivered":        "delivered",
        "completed":        "delivered",
    }
    db_status = status_map.get(new_status, "confirmed")
    try:
        from order_lifecycle import update_order_status_supabase
        update_order_status_supabase(order_id, db_status)
    except Exception as exc:
        log.warning("lifecycle: db status update failed: %s", exc)

    # ── Build customer message ────────────────────────────────────────────────
    ref      = f"ORDER-{order_id}"
    biz_name = order.get("business_name", "")
    try:
        biz_row  = crud.get_business_by_id(bid)
        biz_name = biz_row.get("name", "") if biz_row else biz_name
    except Exception:
        pass

    if data.message:
        customer_msg = data.message
    else:
        template = _LIFECYCLE_MESSAGES.get(new_status, "📦 Order *{ref}* status updated.")
        eta_text = ""
        if data.estimated_minutes:
            eta_text = f"Estimated arrival: *{data.estimated_minutes} minutes* ⏱\n"
        elif new_status == "preparing" and data.estimated_minutes:
            eta_text = f"*{data.estimated_minutes} minutes*"
        else:
            eta_text = "*shortly*" if new_status == "preparing" else ""

        customer_msg = template.format(ref=ref, biz=biz_name, eta=eta_text)

    # ── Send WhatsApp notification ────────────────────────────────────────────
    phone = order.get("customer_phone", "")
    wa_result: dict = {}
    if phone:
        try:
            biz_row  = crud.get_business_by_id(bid)
            token    = crud.get_decrypted_token(biz_row) if biz_row else ""
            phone_id = biz_row.get("whatsapp_phone_id", "") if biz_row else ""
            if token and phone_id:
                wa_result = send_whatsapp(phone_id, token, phone, customer_msg)
                log.info("lifecycle: WA sent  order=%s  status=%s  phone=%s",
                         order_id, new_status, phone)
        except Exception as exc:
            log.error("lifecycle: WA send failed: %s", exc)
            wa_result = {"error": str(exc)}

    # ── Trigger survey if order is complete ───────────────────────────────────
    if new_status in ("delivered", "completed") and phone:
        try:
            from ai import _set_survey_state
            _set_survey_state(phone, bid)
            log.info("lifecycle: survey triggered  phone=%s", phone)
        except Exception as exc:
            log.warning("lifecycle: survey trigger failed: %s", exc)

    log.info("lifecycle update  order=%s  status=%s  by=%s", order_id, new_status, user["username"])
    return {
        "ok":          True,
        "order_id":    order_id,
        "new_status":  new_status,
        "customer_notified": bool(phone and not wa_result.get("error")),
        "message_sent": customer_msg[:80] + "..." if len(customer_msg) > 80 else customer_msg,
    }


# ─────────────────────────────────────────────────────────────────────────────
# ADMIN ORDER MANAGEMENT — approve/reject/lifecycle controls for dashboard
# ─────────────────────────────────────────────────────────────────────────────

class OrderAdminAction(BaseModel):
    note: Optional[str] = None         # optional admin note / rejection reason


@app.post("/orders/{order_id}/approve-payment")
async def admin_approve_payment(
    order_id: int,
    data: OrderAdminAction,
    user=Depends(require_business),
):
    """
    Approve a pending payment proof (EcoCash / manual PayPal).
    Marks order as confirmed and sends a WhatsApp confirmation to the customer.
    """
    from order_lifecycle import get_order, update_order_status_supabase

    bid   = user["business_id"]
    order = get_order(order_id)
    if not order or order.get("business_id") != bid:
        raise HTTPException(404, f"Order {order_id} not found")

    # Mark payment as paid and order as confirmed
    crud.update_order_payment(order_id, bid, {"payment_status": "paid"})
    try:
        update_order_status_supabase(order_id, "confirmed")
    except Exception as exc:
        log.warning("approve_payment: status update failed: %s", exc)

    ref   = f"ORDER-{order_id}"
    phone = order.get("customer_phone", "")
    total = float(order.get("total_price") or 0)
    note_line = f"\n_Note: {data.note}_" if data.note else ""

    await _notify_customer_payment(order, (
        f"✅ *Payment Confirmed!*\n\n"
        f"Your payment for *{ref}* has been verified.\n\n"
        f"💰 Amount: ${total:.2f}\n"
        f"📍 Status: *CONFIRMED*{note_line}\n\n"
        f"We're now preparing your order! 🙌"
    ))

    # Trigger fulfillment question if not yet set
    if phone and not order.get("fulfillment_method"):
        try:
            from ai import _set_awaiting_fulfillment, _get_state
            if _get_state(phone, bid) == "browsing":
                _set_awaiting_fulfillment(phone, bid, order_id=order_id, reference=ref)
                biz      = crud.get_business_by_id(bid)
                token    = crud.get_decrypted_token(biz) if biz else ""
                phone_id = biz.get("whatsapp_phone_id", "") if biz else ""
                if token and phone_id:
                    send_whatsapp(phone_id, token, phone, (
                        f"🚚 *One more step!*\n\n"
                        f"How would you like to receive *{ref}*?\n\n"
                        f"  1️⃣  *Delivery* — we bring it to you\n"
                        f"  2️⃣  *Pickup* — collect from us\n\n"
                        f"_Reply *1* or *delivery* / *2* or *pickup*_"
                    ))
        except Exception as exc:
            log.warning("approve_payment: fulfillment trigger failed: %s", exc)

    log.info("approve_payment  order=%s  by=%s", order_id, user["username"])
    return {"ok": True, "order_id": order_id, "status": "confirmed",
            "message": f"Payment for {ref} approved and customer notified."}


@app.post("/orders/{order_id}/reject-proof")
async def admin_reject_proof(
    order_id: int,
    data: OrderAdminAction,
    user=Depends(require_business),
):
    """
    Reject a payment proof — marks order as awaiting_payment again and
    asks the customer to re-submit proof.
    """
    from order_lifecycle import get_order

    bid   = user["business_id"]
    order = get_order(order_id)
    if not order or order.get("business_id") != bid:
        raise HTTPException(404, f"Order {order_id} not found")

    crud.update_order_payment(order_id, bid, {"payment_status": "awaiting_payment"})

    ref    = f"ORDER-{order_id}"
    reason = data.note or "The proof was unclear or did not match the order amount."

    await _notify_customer_payment(order, (
        f"⚠️ *Payment Proof Not Accepted*\n\n"
        f"We could not verify your payment for *{ref}*.\n\n"
        f"Reason: _{reason}_\n\n"
        f"Please send a clearer screenshot or the correct transaction ID.\n"
        f"_Reply *paid* to submit new proof._"
    ))

    log.info("reject_proof  order=%s  reason=%r  by=%s", order_id, reason, user["username"])
    return {"ok": True, "order_id": order_id, "message": "Proof rejected, customer notified."}


@app.post("/orders/{order_id}/cancel")
async def admin_cancel_order(
    order_id: int,
    data: OrderAdminAction,
    user=Depends(require_business),
):
    """Cancel an order from the admin side."""
    from order_lifecycle import get_order, update_order_status_supabase

    bid   = user["business_id"]
    order = get_order(order_id)
    if not order or order.get("business_id") != bid:
        raise HTTPException(404, f"Order {order_id} not found")

    try:
        update_order_status_supabase(order_id, "cancelled")
    except Exception as exc:
        raise HTTPException(422, str(exc))

    crud.update_order_payment(order_id, bid, {"payment_status": "cancelled"})

    ref    = f"ORDER-{order_id}"
    reason = data.note or "Your order has been cancelled."

    await _notify_customer_payment(order, (
        f"❌ *Order Cancelled*\n\n"
        f"*{ref}* has been cancelled.\n\n"
        f"_{reason}_\n\n"
        f"Type *menu* to place a new order. 😊"
    ))

    log.info("admin_cancel_order  order=%s  by=%s", order_id, user["username"])
    return {"ok": True, "order_id": order_id, "message": f"{ref} cancelled."}


@app.post("/orders/{order_id}/refund")
async def admin_refund_order(
    order_id: int,
    data: OrderAdminAction,
    user=Depends(require_business),
):
    """Mark an order as refunded and notify the customer."""
    from order_lifecycle import get_order, update_order_status_supabase

    bid   = user["business_id"]
    order = get_order(order_id)
    if not order or order.get("business_id") != bid:
        raise HTTPException(404, f"Order {order_id} not found")

    try:
        update_order_status_supabase(order_id, "refunded")
    except Exception as exc:
        raise HTTPException(422, str(exc))

    crud.update_order_payment(order_id, bid, {"payment_status": "refunded"})

    ref   = f"ORDER-{order_id}"
    note  = data.note or "Your refund has been processed."
    total = float(order.get("total_price") or 0)

    await _notify_customer_payment(order, (
        f"💳 *Refund Processed*\n\n"
        f"*{ref}* — ${total:.2f}\n\n"
        f"_{note}_\n\n"
        f"Please allow 3–5 business days for the refund to appear. 🙏"
    ))

    log.info("admin_refund  order=%s  by=%s", order_id, user["username"])
    return {"ok": True, "order_id": order_id, "message": f"{ref} marked refunded."}


@app.get("/orders/{order_id}/status")
def get_order_status(order_id: int, user=Depends(require_business)):
    """
    Get full status details for an order — for dashboard display.
    Returns status, payment_status, fulfillment_method, delivery_address, progress_bar.
    """
    from order_lifecycle import get_order, format_order_status, get_progress_bar, next_order_stage

    bid   = user["business_id"]
    order = get_order(order_id)
    if not order or order.get("business_id") != bid:
        raise HTTPException(404, f"Order {order_id} not found")

    status  = order.get("status", "pending")
    payment = order.get("payment_status", "pending")

    return {
        "order_id":          order_id,
        "status":            status,
        "status_label":      format_order_status(status),
        "payment_status":    payment,
        "payment_label":     format_order_status(payment),
        "progress_bar":      get_progress_bar(status),
        "next_stage":        next_order_stage(status),
        "fulfillment_method": order.get("fulfillment_method", ""),
        "delivery_address":  order.get("delivery_address", ""),
        "total_price":       float(order.get("total_price") or 0),
        "customer_phone":    order.get("customer_phone", ""),
        "created_at":        order.get("created_at", ""),
    }


# ─────────────────────────────────────────────────────────────────────────────
# PLATFORM / SUPER-ADMIN ENDPOINTS — multi-tenant management
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/platform/businesses")
def platform_list_businesses(user=Depends(require_superadmin)):
    """
    Super admin: list all businesses on the platform.
    Returns full details including status, shared-number flag.
    """
    businesses = crud.get_all_businesses()
    return {
        "count":       len(businesses),
        "businesses":  businesses,
    }


@app.get("/platform/businesses/active")
def platform_active_businesses():
    """
    Public: list active businesses for the business picker.
    Returns minimal safe info (id, name, category).
    """
    businesses = crud.get_active_businesses()
    return [
        {"id": b["id"], "name": b["name"], "category": b.get("category", "")}
        for b in businesses
    ]


class BusinessStatusUpdate(BaseModel):
    is_active:      Optional[bool] = None
    use_shared_number: Optional[bool] = None
    display_order:  Optional[int]  = None
    category:       Optional[str]  = None


@app.patch("/platform/businesses/{business_id}")
def platform_update_business(
    business_id: int,
    data: BusinessStatusUpdate,
    user=Depends(require_superadmin),
):
    """
    Super admin: update a business — approve, suspend, reorder, categorise.
    """
    biz = crud.get_business_by_id(business_id)
    if not biz:
        raise HTTPException(404, f"Business {business_id} not found")

    updates: dict = {}
    if data.is_active is not None:
        updates["is_active"] = data.is_active
    if data.use_shared_number is not None:
        updates["use_shared_number"] = data.use_shared_number
    if data.display_order is not None:
        updates["display_order"] = data.display_order
    if data.category is not None:
        updates["category"] = data.category.strip()

    if not updates:
        raise HTTPException(422, "No fields to update")

    class _D:
        def dict(self, **_): return updates

    updated = crud.update_business(business_id, _D())
    log.info("platform_update_business  id=%s  fields=%s  by=%s",
             business_id, list(updates.keys()), user.get("sub"))
    return {"ok": True, "business_id": business_id, "updates": updates}


@app.post("/platform/businesses/{business_id}/suspend")
def platform_suspend_business(business_id: int, user=Depends(require_superadmin)):
    """Super admin: suspend a business (is_active=False)."""
    biz = crud.get_business_by_id(business_id)
    if not biz:
        raise HTTPException(404, f"Business {business_id} not found")
    class _D:
        def dict(self, **_): return {"is_active": False}
    crud.update_business(business_id, _D())
    log.info("business suspended  id=%s  by=%s", business_id, user.get("sub"))
    return {"ok": True, "message": f"Business {business_id} suspended."}


@app.post("/platform/businesses/{business_id}/activate")
def platform_activate_business(business_id: int, user=Depends(require_superadmin)):
    """Super admin: activate a suspended business."""
    biz = crud.get_business_by_id(business_id)
    if not biz:
        raise HTTPException(404, f"Business {business_id} not found")
    class _D:
        def dict(self, **_): return {"is_active": True}
    crud.update_business(business_id, _D())
    log.info("business activated  id=%s  by=%s", business_id, user.get("sub"))
    return {"ok": True, "message": f"Business {business_id} activated."}


@app.get("/platform/stats")
def platform_stats(user=Depends(require_superadmin)):
    """Super admin: platform-wide statistics."""
    try:
        from db import supabase as _sb
        businesses  = crud.get_all_businesses()
        active_biz  = [b for b in businesses if b.get("is_active")]
        orders_res  = _sb.table("orders").select("id,total_price,status,payment_status,business_id").execute()
        orders      = orders_res.data or []
        revenue     = sum(float(o.get("total_price") or 0) for o in orders if o.get("payment_status") == "paid")
        customers_r = _sb.table("customers").select("id").execute()

        return {
            "total_businesses":  len(businesses),
            "active_businesses": len(active_biz),
            "total_orders":      len(orders),
            "total_revenue":     round(revenue, 2),
            "total_customers":   len(customers_r.data or []),
            "shared_number_id":  SHARED_PHONE_NUMBER_ID or "not configured",
        }
    except Exception as exc:
        log.error("platform_stats error: %s", exc)
        raise HTTPException(500, str(exc))


@app.get("/platform/customer/{phone}/session")
def platform_customer_session(phone: str, user=Depends(require_superadmin)):
    """
    Super admin: inspect a customer's current business selection and state.
    Useful for debugging routing issues.
    """
    from tenant_router import get_selected_business_id, get_selected_business_name
    bid  = get_selected_business_id(phone)
    name = get_selected_business_name(phone)
    return {
        "phone":                 phone,
        "selected_business_id":  bid,
        "selected_business_name": name,
        "has_selection":         bid is not None,
    }


@app.delete("/platform/customer/{phone}/session")
def platform_clear_customer_session(phone: str, user=Depends(require_superadmin)):
    """Super admin: clear a customer's business selection (forces re-pick)."""
    from tenant_router import clear_selected_business
    clear_selected_business(phone)
    return {"ok": True, "phone": phone, "message": "Session cleared."}


# ─────────────────────────────────────────────────────────────────────────────
# PAYMENT REMINDER ENDPOINTS (Phase 6)
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/payments/reminders/pending")
def reminders_pending(user=Depends(require_business)):
    """
    Return all stale payment orders for this business.
    Used by the dashboard to show a badge count and order list.

    Query stale orders using the FIRST_REMINDER_HOURS threshold so the
    dashboard reflects exactly which orders a reminder run would target.
    """
    from workflows.payment_reminder import FIRST_REMINDER_HOURS, get_reminder_tier
    bid   = user["business_id"]
    stale = crud.get_stale_payment_orders(bid, older_than_hours=FIRST_REMINDER_HOURS)

    enriched = []
    for order in stale:
        tier = get_reminder_tier(order)
        enriched.append({
            "order_id":        order.get("id"),
            "customer_phone":  order.get("customer_phone"),
            "total_price":     float(order.get("total_price") or 0),
            "payment_method":  order.get("payment_method", ""),
            "payment_status":  order.get("payment_status", ""),
            "payment_reference": order.get("payment_reference", ""),
            "created_at":      order.get("created_at", ""),
            "reminder_tier":   tier,          # 1 | 2 | 3 | None
        })

    return {
        "count":  len(enriched),
        "orders": enriched,
    }


@app.post("/payments/reminders/send")
async def reminders_send(
    dry_run: bool = False,
    user=Depends(require_business),
):
    """
    Send payment reminders to all stale-payment customers for this business.

    Set dry_run=true to preview messages without sending.

    Designed to be called:
      - Manually from the dashboard (business owner clicks "Send Reminders")
      - By a Render cron job: curl -X POST .../payments/reminders/send
        with the Authorization header set

    Rate-limited per order by COOLDOWN_MINUTES (default 55 min) so calling
    this endpoint multiple times in quick succession is safe.
    """
    from workflows.payment_reminder import run_reminders_for_business
    bid    = user["business_id"]
    result = run_reminders_for_business(bid, dry_run=dry_run)
    log.info(
        "reminders_send  biz=%s  sent=%s  failed=%s  dry=%s",
        bid, result.get("sent"), result.get("failed"), dry_run,
    )
    return result


@app.post("/payments/reminders/{order_id}/nudge")
async def reminder_nudge(
    order_id: int,
    dry_run:  bool = False,
    user=Depends(require_business),
):
    """
    Re-send a payment reminder for one specific order.

    Use this from the dashboard order detail view — business owner can
    manually nudge a specific customer without triggering bulk reminders.

    Bypasses the in-process cooldown (allows intentional re-send).
    """
    from workflows.payment_reminder import (
        send_reminder, get_reminder_tier, build_reminder_message,
        FIRST_REMINDER_HOURS,
    )
    from workflows.order_lifecycle import get_order

    bid   = user["business_id"]
    order = get_order(order_id)
    if not order or order.get("business_id") != bid:
        raise HTTPException(404, f"Order {order_id} not found")

    pstatus = order.get("payment_status", "")
    if pstatus not in ("awaiting_payment", "payment_review", "pending_cash"):
        raise HTTPException(
            422,
            f"Order {order_id} has payment_status={pstatus!r}. "
            "Reminders only apply to awaiting_payment or payment_review orders.",
        )

    business = crud.get_business_by_id(bid)
    if not business:
        raise HTTPException(404, "Business not found")

    # For manual nudge: use tier based on age, default to tier 1 for very new orders
    tier = get_reminder_tier(order) or 1

    # Clear cooldown for this specific nudge (intentional manual action)
    from workflows.payment_reminder import _last_reminder_sent
    _last_reminder_sent.pop(order_id, None)

    result = send_reminder(order, business, tier, dry_run=dry_run)
    return result


@app.get("/payments/reminders/{order_id}/preview")
def reminder_preview(order_id: int, user=Depends(require_business)):
    """
    Preview the reminder message that would be sent for an order,
    without actually sending it. Useful for the dashboard.
    """
    from workflows.payment_reminder import (
        build_reminder_message, get_reminder_tier,
    )
    from workflows.order_lifecycle import get_order

    bid   = user["business_id"]
    order = get_order(order_id)
    if not order or order.get("business_id") != bid:
        raise HTTPException(404, f"Order {order_id} not found")

    business = crud.get_business_by_id(bid)
    biz_name = business.get("name", "WaziBot") if business else "WaziBot"
    tier     = get_reminder_tier(order) or 1

    return {
        "order_id":      order_id,
        "tier":          tier,
        "customer_phone": order.get("customer_phone", ""),
        "payment_status": order.get("payment_status", ""),
        "preview_message": build_reminder_message(order, biz_name, tier),
    }


# ─────────────────────────────────────────────────────────────────────────────
# BUSINESS INTELLIGENCE ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/analytics/stats")
def analytics_stats(user=Depends(require_business)):
    """
    Lightweight business stats for the dashboard analytics cards.
    Returns: total_orders, paid_orders, total_revenue, pending_orders,
             active_customers, ai_handled, human_handled.
    """
    return crud.get_business_stats(user["business_id"])


@app.get("/analytics/top-customers")
def analytics_top_customers(limit: int = 10, user=Depends(require_business)):
    """Top customers by order count from user_memory."""
    return crud.get_top_customers(user["business_id"], limit=limit)


@app.get("/analytics/low-stock")
def analytics_low_stock(user=Depends(require_business)):
    """
    Products at or below their low_stock_threshold.
    Used for dashboard alert badges and business-owner notifications.
    """
    return crud.get_low_stock_products(user["business_id"])


@app.post("/analytics/notify-low-stock")
async def notify_low_stock_to_owner(user=Depends(require_business)):
    """
    Send a WhatsApp message to the business owner listing low-stock products.
    Business owner must have a contact_phone set in their profile.
    """
    bid  = user["business_id"]
    biz  = crud.get_business_by_id(bid)
    if not biz:
        raise HTTPException(404, "Business not found")

    owner_phone = biz.get("contact_phone", "").strip()
    if not owner_phone:
        return {"ok": False, "message": "No contact_phone set for this business."}

    low = crud.get_low_stock_products(bid)
    if not low:
        return {"ok": True, "message": "All products are well-stocked! ✅"}

    lines = [f"  • *{p['name']}* — {p.get('stock', 0)} left" for p in low]
    msg = (
        f"⚠️ *Low Stock Alert — {biz.get('name', 'Your Store')}*\n\n"
        + "\n".join(lines)
        + "\n\n_Please restock soon to avoid lost sales._"
    )

    try:
        token    = crud.get_decrypted_token(biz)
        phone_id = biz.get("whatsapp_phone_id", "")
        if not token or not phone_id:
            # Try shared number
            if SHARED_WA_TOKEN and SHARED_PHONE_NUMBER_ID:
                token, phone_id = SHARED_WA_TOKEN, SHARED_PHONE_NUMBER_ID
            else:
                return {"ok": False, "message": "No WhatsApp token configured."}
        result = send_whatsapp(phone_id, token, owner_phone, msg)
        ok = "error" not in result
        log.info("notify_low_stock  biz=%s  sent=%s  products=%d", bid, ok, len(low))
        return {"ok": ok, "products_alerted": len(low)}
    except Exception as exc:
        log.error("notify_low_stock error: %s", exc)
        raise HTTPException(500, str(exc))


# ─────────────────────────────────────────────────────────────────────────────
# VOICE NOTE PROCESSING (architecture hook — Whisper integration ready)
# ─────────────────────────────────────────────────────────────────────────────

class VoiceTranscribeRequest(BaseModel):
    """
    Request to process a voice note.
    In production, `audio_url` points to the WhatsApp media URL.
    The backend downloads it, calls the transcription provider (Whisper or
    another STT), then feeds the transcript to generate_reply().
    """
    customer_id:  int
    audio_url:    str
    phone:        str
    language:     str = "en"   # "en", "sn" (Shona), "auto"


@app.post("/voice/transcribe")
async def voice_transcribe(body: VoiceTranscribeRequest, user=Depends(require_business)):
    """
    Architecture hook for voice note processing.

    Current implementation:
      - Downloads the audio from WhatsApp media URL
      - Returns a placeholder (Whisper integration pending)

    To activate Whisper:
      - Set OPENAI_API_KEY in Render env vars
      - Uncomment the OpenAI section below

    This endpoint is called by the webhook when msg_type == "audio".
    """
    import os
    bid      = user["business_id"]
    customer = crud.get_customer_by_id(body.customer_id, bid)
    if not customer:
        raise HTTPException(404, "Customer not found")

    openai_key = os.getenv("OPENAI_API_KEY", "").strip()

    transcript = None

    if openai_key:
        try:
            import httpx, tempfile, pathlib
            biz   = crud.get_business_by_id(bid)
            token = crud.get_decrypted_token(biz) if biz else ""

            # Download audio from WhatsApp
            async with httpx.AsyncClient() as client:
                headers = {"Authorization": f"Bearer {token}"}
                r = await client.get(body.audio_url, headers=headers, timeout=30)
                r.raise_for_status()
                audio_bytes = r.content

            # Call Whisper API
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    "https://api.openai.com/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {openai_key}"},
                    files={"file": ("voice.ogg", audio_bytes, "audio/ogg")},
                    data={"model": "whisper-1", "language": body.language if body.language != "auto" else ""},
                    timeout=60,
                )
                resp.raise_for_status()
                transcript = resp.json().get("text", "").strip()
                log.info("voice_transcribe OK  phone=%s  transcript=%r", body.phone, transcript[:60])

        except Exception as exc:
            log.warning("voice_transcribe whisper error: %s", exc)
            transcript = None

    if not transcript:
        # Graceful fallback — ask customer to type their order
        log.info("voice_transcribe: no transcript  phone=%s", body.phone)
        return {
            "ok":         False,
            "transcript": None,
            "reply":      "🎤 Sorry, I couldn't process your voice note. Could you type your order instead? Type *menu* to get started! 😊",
        }

    # Process the transcript as a normal text message
    biz      = crud.get_business_by_id(bid)
    products = crud.get_products(bid)
    reply    = generate_reply(
        message=transcript,
        phone=body.phone,
        business_id=bid,
        business_name=biz.get("name", "") if biz else "",
        products=products,
        voice_transcript=transcript,
    )
    return {"ok": True, "transcript": transcript, "reply": reply}


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
    # Legacy payment fields (kept for backward compat)
    payment_number:    Optional[str]  = None
    payment_name:      Optional[str]  = None
    # New dedicated fields — set via /me/payment-settings
    ecocash_number:    Optional[str]  = None
    ecocash_name:      Optional[str]  = None
    paypal_email:      Optional[str]  = None


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
    """
    Create a new product for the authenticated business.
    Returns the created product dict including its Supabase-generated id.
    """
    bid = user["business_id"]
    log.info(
        "📦 create_product  business_id=%s  name=%r  price=%s  stock=%s",
        bid, product.name, product.price, product.stock,
    )

    # Validate before hitting the DB
    if not product.name or not product.name.strip():
        raise HTTPException(422, "Product name cannot be empty")
    if product.price < 0:
        raise HTTPException(422, "Product price must be a non-negative number")

    try:
        created = crud.create_product(bid, product)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    except RuntimeError as exc:
        log.error("create_product runtime error: %s", exc)
        raise HTTPException(500, str(exc))
    except Exception as exc:
        log.exception("create_product unexpected error: %s", exc)
        raise HTTPException(500, "Failed to create product — please try again")

    return created


@app.patch("/products/{product_id}")
def update_product(product_id: int, data: dict, user=Depends(require_business)):
    """Update specific fields of a product (name, price, stock, image_url, etc.)."""
    bid = user["business_id"]
    # Prevent updating business_id
    data.pop("business_id", None)
    data.pop("id", None)

    if not data:
        raise HTTPException(422, "No fields to update")

    updated = crud.update_product(product_id, bid, data)
    if not updated:
        raise HTTPException(404, f"Product {product_id} not found")

    log.info("update_product OK  id=%s  fields=%s  business=%s",
             product_id, list(data.keys()), bid)
    return updated


@app.delete("/products/{product_id}")
def delete_product(product_id: int, user=Depends(require_business)):
    """
    Delete a product by id. Returns { deleted: id, name: str }.
    Returns 404 if product not found or belongs to another business.
    """
    bid = user["business_id"]
    log.info("delete_product  id=%s  business=%s", product_id, bid)

    try:
        p = crud.delete_product(product_id, bid)
    except RuntimeError as exc:
        log.error("delete_product runtime error: %s", exc)
        raise HTTPException(500, str(exc))
    except Exception as exc:
        log.exception("delete_product unexpected error: %s", exc)
        raise HTTPException(500, "Failed to delete product — please try again")

    if not p:
        raise HTTPException(404, f"Product {product_id} not found or access denied")

    log.info("delete_product OK  id=%s  name=%r  business=%s", product_id, p.get("name"), bid)
    return {"deleted": product_id, "name": p.get("name", "")}


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
    message:      str
    phone_filter: list[str] | None = None   # If set, send ONLY to these phones.
                                             # If None/empty, send to ALL customers.

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

    all_phones = crud.get_all_customer_phones(bid)
    if not all_phones:
        return {"sent": 0, "failed": 0, "total": 0, "message": "No customers found"}

    # Apply phone_filter if provided — only send to the selected subset
    if body.phone_filter:
        # Normalise both sides for comparison (strip spaces, remove +)
        filter_set = {p.strip().lstrip("+") for p in body.phone_filter if p}
        phones     = [p for p in all_phones if p.strip().lstrip("+") in filter_set]
        log.info(
            "📢 Broadcast filtered  total=%d  selected=%d  business=%s",
            len(all_phones), len(phones), business["name"],
        )
    else:
        phones = all_phones

    if not phones:
        return {"sent": 0, "failed": 0, "total": len(all_phones),
                "message": "No recipients matched the filter"}

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
    # Mark as agent-sent so it can be visually differentiated from AI responses
    msg = crud.create_message(
        customer["id"], bid, body.text, "outgoing",
        sender_type="agent",   # "agent" | "ai" — stored in messages table if col exists
        sender_name=user.get("username", "Agent"),
    )

    wa_result: dict = {}
    if has_token and has_phone_id:
        wa_result = send_whatsapp(business["whatsapp_phone_id"], token, customer["phone"], body.text)
    elif SHARED_WA_TOKEN and SHARED_PHONE_NUMBER_ID:
        # Shared number path
        wa_result = send_whatsapp(SHARED_PHONE_NUMBER_ID, SHARED_WA_TOKEN, customer["phone"], body.text)
    else:
        missing = [k for k, v in {"phone_number_id": has_phone_id, "token": has_token}.items() if not v]
        log.warning("chat_send: WhatsApp NOT sent — missing: %s", missing)
        wa_result = {"error": f"credentials missing: {missing}"}

    # Record agent activity so auto-resume timeout resets correctly
    try:
        from human_handoff import record_agent_reply
        record_agent_reply(customer["phone"], bid)
    except Exception as exc:
        log.debug("record_agent_reply skipped: %s", exc)

    await manager.broadcast(bid, {
        "event":       "new_message",
        "customer_id": customer["id"],
        "phone":       customer["phone"],
        "message":     msg,
    })

    return {"ok": True, "message_id": msg["id"], "whatsapp_result": wa_result}


# ─────────────────────────────────────────────────────────────────────────────
# HUMAN HANDOFF MANAGEMENT
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/chat/handoff/{customer_id}/request")
async def request_human_handoff(customer_id: int, user=Depends(require_business)):
    """
    Manually flag a customer for human agent attention from the dashboard.
    Sets state to human_handoff and notifies the customer.
    """
    from ai import _set_human_handoff
    from human_handoff import notify_dashboard, handoff_acknowledgement

    bid      = user["business_id"]
    customer = crud.get_customer_by_id(customer_id, bid)
    if not customer:
        raise HTTPException(404, "Customer not found")

    phone = customer["phone"]
    business = crud.get_business_by_id(bid)
    biz_name = business.get("name", "") if business else ""

    _set_human_handoff(phone, bid)
    notify_dashboard(phone, bid, biz_name)

    # Notify customer via WhatsApp
    try:
        token    = crud.get_decrypted_token(business)
        phone_id = business.get("whatsapp_phone_id", "")
        if token and phone_id:
            send_whatsapp(phone_id, token, phone, handoff_acknowledgement(biz_name))
    except Exception as exc:
        log.warning("handoff request: WA notify failed: %s", exc)

    log.info("handoff requested  customer=%s  by=%s", customer_id, user["username"])
    return {"ok": True, "customer_id": customer_id, "phone": phone,
            "message": "Customer flagged for human support. AI is now paused."}


@app.post("/chat/handoff/{customer_id}/release")
async def release_human_handoff(customer_id: int, user=Depends(require_business)):
    """
    Return a customer to AI mode after human agent is done.
    Clears human_handoff state and notifies customer.
    """
    from ai import _reset_state as ai_reset_state
    from human_handoff import clear_handoff_flag, ai_resumed_message

    bid      = user["business_id"]
    customer = crud.get_customer_by_id(customer_id, bid)
    if not customer:
        raise HTTPException(404, "Customer not found")

    phone = customer["phone"]
    business = crud.get_business_by_id(bid)
    biz_name = business.get("name", "") if business else ""

    ai_reset_state(phone, bid)
    clear_handoff_flag(phone, bid)

    # Notify customer
    try:
        token    = crud.get_decrypted_token(business)
        phone_id = business.get("whatsapp_phone_id", "")
        if token and phone_id:
            send_whatsapp(phone_id, token, phone, ai_resumed_message(biz_name))
    except Exception as exc:
        log.warning("handoff release: WA notify failed: %s", exc)

    log.info("handoff released  customer=%s  by=%s", customer_id, user["username"])
    return {"ok": True, "customer_id": customer_id, "phone": phone,
            "message": "AI resumed. Customer will now interact with the AI assistant."}


@app.delete("/chat/conversations/{customer_id}")
async def delete_conversation(customer_id: int, user=Depends(require_business)):
    """
    Delete all messages for a customer conversation.
    The customer and order records are preserved — only messages are removed.
    Used by the inbox Delete button.
    """
    bid      = user["business_id"]
    customer = crud.get_customer_by_id(customer_id, bid)
    if not customer:
        raise HTTPException(404, f"Customer {customer_id} not found")

    try:
        from db import supabase as _sb
        # Delete from both messages tables
        _sb.table("messages").delete().eq("customer_id", customer_id).eq("business_id", bid).execute()
        try:
            _sb.table("chat_messages").delete().eq("customer_id", customer_id).eq("business_id", bid).execute()
        except Exception:
            pass  # chat_messages may not exist in all deployments
        log.info("delete_conversation  customer=%s  biz=%s  by=%s", customer_id, bid, user.get("username"))
        return {"ok": True, "customer_id": customer_id, "message": "Conversation deleted"}
    except Exception as exc:
        log.error("delete_conversation error: %s", exc)
        raise HTTPException(500, str(exc))


@app.get("/chat/handoff/pending")
def list_pending_handoffs(user=Depends(require_business)):
    """
    List all customers currently in human_handoff mode for this business.
    Returns customer details sorted by most recent.
    """
    from db import supabase as _sb

    bid = user["business_id"]
    try:
        # Find carts with state_data.state == human_handoff for this business
        res = (
            _sb.table("carts")
            .select("phone, state_data, updated_at")
            .eq("business_id", bid)
            .execute()
        )
        pending = []
        for row in (res.data or []):
            sd = row.get("state_data") or {}
            if sd.get("state") == "human_handoff":
                customer = None
                try:
                    cres = (
                        _sb.table("customers")
                        .select("id, phone, unread_count, last_seen")
                        .eq("phone", row["phone"])
                        .eq("business_id", bid)
                        .limit(1)
                        .execute()
                    )
                    customer = cres.data[0] if cres.data else None
                except Exception:
                    pass
                pending.append({
                    "phone":      row["phone"],
                    "updated_at": row.get("updated_at"),
                    "customer":   customer,
                })
        return {"count": len(pending), "pending": pending}
    except Exception as exc:
        log.error("list_pending_handoffs error: %s", exc)
        raise HTTPException(500, "Failed to load pending handoffs")


# ─────────────────────────────────────────────────────────────────────────────
# HUMAN HANDOFF ADMIN ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/chat/handoff/pending")
def handoff_pending(user=Depends(require_business)):
    """
    List all customers currently in human_handoff mode for this business.
    Use in the dashboard to surface conversations that need a human reply.
    """
    from human_handoff import get_pending_handoffs
    return get_pending_handoffs(user["business_id"])


@app.post("/chat/handoff/{customer_id}/request")
async def handoff_request(customer_id: int, user=Depends(require_business)):
    """
    Manually put a customer into human_handoff mode.
    AI will pause for this customer until released.
    """
    bid      = user["business_id"]
    customer = crud.get_customer_by_id(customer_id, bid)
    if not customer:
        raise HTTPException(404, "Customer not found")

    from human_handoff import notify_dashboard
    from ai import _set_human_handoff

    phone     = customer["phone"]
    biz       = crud.get_business_by_id(bid)
    biz_name  = biz.get("name", "") if biz else ""

    _set_human_handoff(phone, bid)
    notify_dashboard(phone, bid, biz_name)

    log.info("handoff requested  customer=%s  phone=%s  by=%s", customer_id, phone, user["username"])
    return {"ok": True, "customer_id": customer_id, "phone": phone, "state": "human_handoff"}


@app.post("/chat/handoff/{customer_id}/release")
async def handoff_release(customer_id: int, user=Depends(require_business)):
    """
    Release a customer from human_handoff mode — AI resumes.
    Sends a WhatsApp message to the customer informing them AI is back.
    """
    bid      = user["business_id"]
    customer = crud.get_customer_by_id(customer_id, bid)
    if not customer:
        raise HTTPException(404, "Customer not found")

    from human_handoff import clear_handoff_flag, ai_resumed_message
    from ai import _reset_state

    phone    = customer["phone"]
    biz      = crud.get_business_by_id(bid)
    biz_name = biz.get("name", "") if biz else ""

    _reset_state(phone, bid)
    clear_handoff_flag(phone, bid)

    # Notify the customer via WhatsApp that AI is back
    try:
        token    = crud.get_decrypted_token(biz) if biz else ""
        phone_id = biz.get("whatsapp_phone_id", "") if biz else ""
        if token and phone_id:
            send_whatsapp(phone_id, token, phone, ai_resumed_message(biz_name))
    except Exception as exc:
        log.warning("handoff_release: WA notification failed: %s", exc)

    log.info("handoff released  customer=%s  phone=%s  by=%s", customer_id, phone, user["username"])
    return {"ok": True, "customer_id": customer_id, "phone": phone, "state": "browsing"}


# ─────────────────────────────────────────────────────────────────────────────
# CART MANAGEMENT (debug + admin)
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/cart/{phone}")
def get_cart(phone: str, user=Depends(require_business)):
    """View the current cart for a customer phone number."""
    cart = crud.get_cart(phone, user["business_id"])
    total = sum(i["qty"] * float(i["price"]) for i in cart)
    return {"phone": phone, "items": cart, "total": round(total, 2), "count": len(cart)}


@app.delete("/cart/{phone}")
def clear_cart(phone: str, user=Depends(require_business)):
    """Clear the cart for a customer (useful for testing / support)."""
    crud.clear_cart(phone, user["business_id"])
    log.info("cart cleared  phone=%s  by=%s", phone, user["username"])
    return {"ok": True, "phone": phone, "message": "Cart cleared"}


@app.get("/debug/schema")
def debug_schema(user=Depends(require_business)):
    """
    Show which optional columns exist in the orders table.
    Useful to confirm whether MIGRATION.sql has been run.
    """
    from order_lifecycle import _get_orders_columns, _invalidate_column_cache
    _invalidate_column_cache()   # force re-probe
    cols = _get_orders_columns()
    optional = ["items", "payment_status", "payment_reference"]
    return {
        "all_columns":      sorted(cols),
        "optional_columns": {c: (c in cols) for c in optional},
        "migration_needed": not all(c in cols for c in optional),
        "action": (
            "✅ Schema is up to date"
            if all(c in cols for c in optional)
            else "⚠️  Run MIGRATION.sql in Supabase SQL Editor to add missing columns"
        ),
    }


# ─────────────────────────────────────────────────────────────────────────────
# INBOX — MESSAGE DELETE / CLEAR  (required by inbox.js v3.0)
# ─────────────────────────────────────────────────────────────────────────────

@app.delete("/chat/messages/{message_id}")
def delete_single_message(message_id: int, user=Depends(require_business)):
    """
    Delete a single message by its DB id.
    Only deletes messages belonging to the authenticated business.
    """
    deleted = crud.delete_message(message_id, user["business_id"])
    if not deleted:
        raise HTTPException(404, "Message not found or access denied")
    return {"ok": True, "deleted_id": message_id}


@app.delete("/chat/clear/{customer_id}")
def clear_conversation(customer_id: int, user=Depends(require_business)):
    """
    Delete all messages for a customer conversation.
    Resets unread count to 0.
    """
    # Verify this customer belongs to the business
    customer = crud.get_customer_by_id(customer_id, user["business_id"])
    if not customer:
        raise HTTPException(404, "Customer not found")

    count = crud.clear_customer_messages(customer_id, user["business_id"])
    log.info("clear_conversation  customer=%s  deleted=%d  by=%s", customer_id, count, user["username"])
    return {"ok": True, "customer_id": customer_id, "deleted": count}


# ─────────────────────────────────────────────────────────────────────────────
# BUSINESS SETTINGS — PAYMENT DETAILS
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# PAYMENT SETTINGS — per-business EcoCash + PayPal configuration
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/me/payment-settings")
def get_payment_settings(user=Depends(require_business)):
    """
    Get all payment settings for the authenticated business.
    Returns EcoCash and PayPal configuration so the settings page can
    pre-populate form fields.
    """
    b = crud.get_business_by_id(user["business_id"])
    if not b:
        raise HTTPException(404, "Business not found")

    # Resolve with legacy fallbacks
    ecocash_number = b.get("ecocash_number") or b.get("payment_number") or ""
    ecocash_name   = b.get("ecocash_name")   or b.get("payment_name")  or ""
    paypal_email   = b.get("paypal_email") or ""

    return {
        "business_name":    b.get("name", ""),
        # EcoCash
        "ecocash_number":   ecocash_number,
        "ecocash_name":     ecocash_name,
        "ecocash_configured": bool(ecocash_number),
        # PayPal
        "paypal_email":     paypal_email,
        "paypal_configured": bool(paypal_email),
        # Legacy fields (kept for backward compat with existing code/frontend)
        "payment_number":   ecocash_number,
        "payment_name":     ecocash_name,
    }


class EcoCashSettingsUpdate(BaseModel):
    ecocash_number: str    # e.g. +263771234567
    ecocash_name:   str    # e.g. Flavoury Foods (Pvt) Ltd


class PayPalSettingsUpdate(BaseModel):
    paypal_email: str      # e.g. payments@flavoury.com


class PaymentSettingsUpdate(BaseModel):
    """Combined update — all fields optional so frontend can patch any subset."""
    ecocash_number: Optional[str] = None
    ecocash_name:   Optional[str] = None
    paypal_email:   Optional[str] = None


@app.post("/me/payment-settings")
def update_payment_settings(data: PaymentSettingsUpdate, user=Depends(require_business)):
    """
    Update EcoCash and/or PayPal settings for the authenticated business.
    All fields are optional — send only the ones you want to update.

    EcoCash:
      { "ecocash_number": "+263771234567", "ecocash_name": "Flavoury Foods" }

    PayPal:
      { "paypal_email": "payments@flavoury.com" }

    Both at once:
      { "ecocash_number": "...", "ecocash_name": "...", "paypal_email": "..." }
    """
    bid     = user["business_id"]
    updates = {}
    errors  = []

    # ── Validate + collect EcoCash updates ────────────────────────────────────
    if data.ecocash_number is not None:
        number = data.ecocash_number.strip()
        if number and len(number) < 7:
            errors.append("EcoCash number is too short — include country code (e.g. +263771234567)")
        elif number:
            updates["ecocash_number"] = number
            updates["payment_number"] = number   # keep legacy field in sync
        else:
            # Empty string = clear the setting
            updates["ecocash_number"] = None
            updates["payment_number"] = None

    if data.ecocash_name is not None:
        name = data.ecocash_name.strip()
        updates["ecocash_name"] = name or None
        updates["payment_name"] = name or None   # keep legacy field in sync

    # ── Validate + collect PayPal updates ────────────────────────────────────
    if data.paypal_email is not None:
        email = data.paypal_email.strip().lower()
        if email and "@" not in email:
            errors.append("PayPal email address is invalid — must contain @")
        elif email:
            updates["paypal_email"] = email
        else:
            updates["paypal_email"] = None

    if errors:
        raise HTTPException(422, "; ".join(errors))

    if not updates:
        raise HTTPException(422, "No valid fields provided to update")

    class _D:
        def dict(self, **_):
            return updates

    b = crud.update_business(bid, _D())
    if not b:
        raise HTTPException(500, "Failed to update payment settings")

    log.info(
        "payment settings updated  business=%s  fields=%s",
        bid, list(updates.keys()),
    )

    # Return the full current state so the UI can refresh without a second GET
    ecocash_number = b.get("ecocash_number") or b.get("payment_number") or ""
    ecocash_name   = b.get("ecocash_name")   or b.get("payment_name")  or ""
    paypal_email   = b.get("paypal_email") or ""

    return {
        "ok":              True,
        "message":         "Payment settings saved successfully.",
        "ecocash_number":  ecocash_number,
        "ecocash_name":    ecocash_name,
        "paypal_email":    paypal_email,
        "ecocash_configured": bool(ecocash_number),
        "paypal_configured":  bool(paypal_email),
    }


@app.post("/me/payment-settings/ecocash")
def update_ecocash_settings(data: EcoCashSettingsUpdate, user=Depends(require_business)):
    """
    Dedicated endpoint for EcoCash-only updates.
    Useful for single-purpose settings forms.
    """
    number = data.ecocash_number.strip()
    name   = data.ecocash_name.strip()

    if not number:
        raise HTTPException(422, "EcoCash number is required")
    if len(number) < 7:
        raise HTTPException(422, "EcoCash number too short — include country code (e.g. +263771234567)")
    if not name:
        raise HTTPException(422, "Business name is required")

    bid = user["business_id"]

    class _D:
        def dict(self, **_):
            return {
                "ecocash_number": number,
                "ecocash_name":   name,
                "payment_number": number,   # legacy sync
                "payment_name":   name,     # legacy sync
            }

    b = crud.update_business(bid, _D())
    if not b:
        raise HTTPException(500, "Failed to save EcoCash settings")

    log.info("ecocash settings updated  business=%s  number=%s  name=%s", bid, number, name)
    return {
        "ok":             True,
        "message":        f"EcoCash number saved. Customers will now be instructed to send money to {number}.",
        "ecocash_number": number,
        "ecocash_name":   name,
    }


@app.post("/me/payment-settings/paypal")
def update_paypal_settings(data: PayPalSettingsUpdate, user=Depends(require_business)):
    """
    Dedicated endpoint for PayPal-only updates.
    The email entered here is the business's real PayPal account email
    where they receive money — no sandbox, no API keys required.
    """
    email = data.paypal_email.strip().lower()

    if not email:
        raise HTTPException(422, "PayPal email is required")
    if "@" not in email or "." not in email.split("@")[-1]:
        raise HTTPException(422, "Invalid PayPal email address")

    bid = user["business_id"]

    class _D:
        def dict(self, **_):
            return {"paypal_email": email}

    b = crud.update_business(bid, _D())
    if not b:
        raise HTTPException(500, "Failed to save PayPal settings")

    log.info("paypal settings updated  business=%s  email=%s", bid, email)
    return {
        "ok":           True,
        "message":      f"PayPal email saved. Customers will be instructed to send money to {email}.",
        "paypal_email": email,
    }


# ── Legacy endpoint — kept for backward compat with older frontend code ───────
@app.get("/me/payment")
def get_payment_settings_legacy(user=Depends(require_business)):
    """Deprecated — use GET /me/payment-settings instead."""
    return get_payment_settings(user)


@app.post("/me/payment")
def update_payment_legacy(user=Depends(require_business)):
    """Deprecated — use POST /me/payment-settings instead."""
    raise HTTPException(
        410,
        "This endpoint is deprecated. "
        "Use POST /me/payment-settings with { ecocash_number, ecocash_name, paypal_email }."
    )


# ─────────────────────────────────────────────────────────────────────────────
# PAYMENT CALLBACKS & WEBHOOKS
# ─────────────────────────────────────────────────────────────────────────────



async def _notify_customer_payment(order: dict, message: str) -> None:
    """Send a WhatsApp message to the customer after payment confirmation."""
    if not order:
        return
    biz_id = order.get("business_id")
    phone  = order.get("customer_phone", "")
    if not biz_id or not phone:
        return
    try:
        business = crud.get_business_by_id(biz_id)
        if not business:
            return
        token    = crud.get_decrypted_token(business)
        phone_id = business.get("whatsapp_phone_id")
        if token and phone_id:
            send_whatsapp(phone_id, token, phone, message)
            log.info("payment notification sent  phone=%s", phone)
    except Exception as exc:
        log.error("_notify_customer_payment error: %s", exc)






@app.get("/payments/paypal/success")
async def paypal_success(
    request: Request,
    token: str = "",            # PayPal order ID (set by PayPal in redirect)
    PayerID: str = "",          # PayPal payer ID
    reference: str = "",        # our ORDER-{id} reference (set in application_context)
):
    """
    Browser redirect after customer approves payment on PayPal.
    Captures the payment and confirms the order.
    NOTE: The PayPal webhook (/payments/paypal/webhook) is the primary
    confirmation path. This endpoint is a fallback for browser users.
    """
    from payments import capture_paypal_order
    from order_lifecycle import get_order

    log.info("paypal_success  token=%s  reference=%s  PayerID=%s", token, reference, PayerID)

    if not token:
        return {"status": "error", "detail": "Missing PayPal token (order ID)"}

    try:
        capture = capture_paypal_order(token)

        if not capture["paid"]:
            log.warning("paypal_success: capture not paid  error=%s", capture.get("error"))
            return {
                "status":  "capture_failed",
                "detail":  capture.get("error"),
                "message": "Payment not completed. Please try again or contact support.",
            }

        # Resolve the internal order ID
        ref = reference or capture.get("reference", "")
        internal_id = capture.get("internal_order_id")

        order = None
        if internal_id:
            order = get_order(internal_id)
        elif ref.startswith("ORDER-"):
            try:
                order = get_order(int(ref.split("-")[1]))
            except (ValueError, IndexError):
                pass

        if not order:
            log.error("paypal_success: order not found  ref=%s  internal_id=%s", ref, internal_id)
            return {"status": "error", "detail": "Order not found"}

        order_id = order["id"]
        biz_id   = order["business_id"]
        ref      = ref or f"ORDER-{order_id}"

        # Idempotency: skip if already paid
        if order.get("payment_status") == "paid":
            log.info("paypal_success: order %s already paid — skipping", order_id)
            return {"status": "ok", "paid": True, "reference": ref, "message": "Already confirmed."}

        # Mark as paid
        crud.update_order_payment(order_id, biz_id, {
            "payment_status":    "paid",
            "payment_reference": ref,
            "paypal_order_id":   token,
        })

        # Send WhatsApp confirmation
        await _notify_customer_payment(order, (
            f"✅ *PayPal Payment Confirmed!*\n\n"
            f"Thank you! Your payment for *{ref}* is complete.\n\n"
            f"💰 Amount: ${capture['amount']:.2f} USD\n"
            f"📦 Your order is now being prepared. Thank you! 🙏"
        ))

        log.info("paypal_success: confirmed  ref=%s  amount=%.2f", ref, capture["amount"])
        return {
            "status":    "ok",
            "paid":      True,
            "reference": ref,
            "amount":    capture["amount"],
            "message":   "Payment confirmed! You will receive a WhatsApp confirmation shortly.",
        }

    except Exception as exc:
        log.exception("paypal_success error: %s", exc)
        return {"status": "error", "detail": str(exc)}


@app.get("/payments/paypal/cancel")
async def paypal_cancel(reference: str = ""):
    """User cancelled PayPal checkout — order stays in pending state."""
    log.info("paypal cancel  reference=%s", reference)
    return {
        "status":    "cancelled",
        "reference": reference,
        "message":   "Payment cancelled. Your cart is still saved — return to WhatsApp to try again.",
    }


# ─────────────────────────────────────────────────────────────────────────────
# PAYPAL WEBHOOK — Primary auto-confirmation path
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/payments/paypal/webhook")
async def paypal_webhook(request: Request):
    """
    Receive PayPal Instant Payment Notification (IPN) events.

    Register this URL in PayPal Developer Dashboard:
      Dashboard → My Apps → [Your App] → Webhooks → Add Webhook
      URL: https://wazibot-api-assistant.onrender.com/payments/paypal/webhook
      Events to subscribe:
        - PAYMENT.CAPTURE.COMPLETED
        - PAYMENT.CAPTURE.DENIED    (optional, for failed payment handling)

    Security:
      - Validates PayPal-Transmission-Sig using verify_paypal_webhook_signature()
      - Validates event_type == PAYMENT.CAPTURE.COMPLETED
      - Validates amount and currency match the stored order
      - Idempotent: skips already-paid orders

    Required ENV var:
      PAYPAL_WEBHOOK_ID — the Webhook ID from PayPal Developer Dashboard
    """
    from payments import verify_paypal_webhook_signature
    from order_lifecycle import get_order

    WEBHOOK_ID = os.getenv("PAYPAL_WEBHOOK_ID", "").strip()

    # ── 1. Read raw body (must be done before await request.json()) ───────────
    raw_body = await request.body()

    # ── 2. Verify signature (skip in dev if PAYPAL_WEBHOOK_ID not set) ───────
    if WEBHOOK_ID:
        is_valid = verify_paypal_webhook_signature(
            headers=dict(request.headers),
            raw_body=raw_body,
            webhook_id=WEBHOOK_ID,
        )
        if not is_valid:
            log.warning("paypal_webhook: invalid signature — rejecting")
            raise HTTPException(400, "Invalid PayPal webhook signature")
    else:
        log.warning("paypal_webhook: PAYPAL_WEBHOOK_ID not set — skipping signature verification (dev mode)")

    # ── 3. Parse event ────────────────────────────────────────────────────────
    try:
        event = await request.json()
    except Exception as exc:
        log.error("paypal_webhook: could not parse JSON body: %s", exc)
        raise HTTPException(400, "Invalid JSON body")

    event_type = event.get("event_type", "")
    log.info("paypal_webhook: event_type=%s", event_type)

    # ── 4. Handle PAYMENT.CAPTURE.COMPLETED ──────────────────────────────────
    if event_type == "PAYMENT.CAPTURE.COMPLETED":
        try:
            resource = event.get("resource", {})

            # Extract paypal_order_id from supplementary_data or links
            supplementary = resource.get("supplementary_data", {})
            related_ids    = supplementary.get("related_ids", {})
            paypal_order_id = related_ids.get("order_id", "")

            # Fallback: extract from links
            if not paypal_order_id:
                for link in resource.get("links", []):
                    if "orders" in link.get("href", ""):
                        paypal_order_id = link["href"].rstrip("/").split("/")[-1]
                        break

            if not paypal_order_id:
                log.error("paypal_webhook: could not extract paypal_order_id from event")
                return {"status": "error", "detail": "Could not extract order ID"}

            # Amount and currency validation
            amount_obj = resource.get("amount", {})
            paid_amount   = float(amount_obj.get("value", 0))
            paid_currency = amount_obj.get("currency_code", "USD").upper()

            if paid_currency != "USD":
                log.warning("paypal_webhook: unexpected currency %s", paid_currency)
                return {"status": "error", "detail": f"Unexpected currency: {paid_currency}"}

            # ── 5. Find the internal order ────────────────────────────────────
            order = crud.get_order_by_paypal_id(paypal_order_id)

            if not order:
                log.warning("paypal_webhook: order not found for paypal_id=%s — checking custom_id",
                            paypal_order_id)
                # Try via custom_id in purchase unit (stored as internal order ID)
                custom_id = resource.get("custom_id", "")
                if custom_id and custom_id.isdigit():
                    order = get_order(int(custom_id))

            if not order:
                log.error("paypal_webhook: no matching order for paypal_id=%s", paypal_order_id)
                return {"status": "error", "detail": "Order not found"}

            order_id = order["id"]
            biz_id   = order["business_id"]
            ref      = order.get("payment_reference") or f"ORDER-{order_id}"

            # ── 6. Idempotency ─────────────────────────────────────────────────
            if order.get("payment_status") == "paid":
                log.info("paypal_webhook: order %s already paid — skip", order_id)
                return {"status": "ok", "detail": "already_paid"}

            # ── 7. Validate amount ─────────────────────────────────────────────
            order_total = round(float(order.get("total_price") or 0), 2)
            if round(paid_amount, 2) != order_total:
                log.error(
                    "paypal_webhook: amount mismatch  expected=%.2f  received=%.2f  order=%s",
                    order_total, paid_amount, order_id,
                )
                # Don't reject outright — could be fees/rounding. Log and proceed if close.
                if abs(paid_amount - order_total) > 0.10:
                    raise HTTPException(400,
                        f"Amount mismatch: expected ${order_total:.2f}, received ${paid_amount:.2f}")

            # ── 8. Mark order as paid ──────────────────────────────────────────
            crud.update_order_payment(order_id, biz_id, {
                "payment_status":    "paid",
                "payment_reference": ref,
                "paypal_order_id":   paypal_order_id,
            })
            try:
                from order_lifecycle import update_order_status_supabase
                update_order_status_supabase(order_id, "paid")
            except Exception as exc:
                log.warning("paypal_webhook: status update failed: %s", exc)

            # ── 9. Reset customer's conversation state ─────────────────────────
            customer_phone = order.get("customer_phone", "")
            if customer_phone:
                try:
                    # Import ai state functions to reset the customer's flow
                    from ai import _reset_state as ai_reset_state
                    ai_reset_state(customer_phone, biz_id)
                except Exception as exc:
                    log.warning("paypal_webhook: state reset failed: %s", exc)

            # ── 10. Send WhatsApp confirmation ──────────────────────────────────
            await _notify_customer_payment(order, (
                f"✅ *Payment Received!*\n\n"
                f"Your PayPal payment of *${paid_amount:.2f} USD* has been confirmed.\n\n"
                f"📦 Order : *{ref}*\n"
                f"📍 Status: *CONFIRMED*\n\n"
                f"We're now preparing your order — we'll be in touch shortly! 🙌\n\n"
                f"_Thank you for choosing us!_ 🙏"
            ))

            log.info(
                "paypal_webhook: ✅ payment confirmed  order=%s  amount=%.2f  paypal_id=%s",
                order_id, paid_amount, paypal_order_id,
            )
            return {"status": "ok", "order_id": order_id, "paid": True}

        except HTTPException:
            raise
        except Exception as exc:
            log.exception("paypal_webhook: PAYMENT.CAPTURE.COMPLETED handler error: %s", exc)
            return {"status": "error", "detail": str(exc)}

    # ── Handle PAYMENT.CAPTURE.DENIED ────────────────────────────────────────
    elif event_type == "PAYMENT.CAPTURE.DENIED":
        try:
            resource    = event.get("resource", {})
            custom_id   = resource.get("custom_id", "")
            order       = get_order(int(custom_id)) if custom_id.isdigit() else None
            if order:
                crud.update_order_payment(order["id"], order["business_id"], {
                    "payment_status": "payment_failed",
                })
                await _notify_customer_payment(order, (
                    f"❌ *PayPal payment failed.*\n\n"
                    f"Your payment for *ORDER-{order['id']}* was declined.\n\n"
                    f"Please try again or choose a different payment method.\n"
                    f"Type *checkout* to try again."
                ))
                log.info("paypal_webhook: DENIED  order=%s", order["id"])
        except Exception as exc:
            log.error("paypal_webhook: DENIED handler error: %s", exc)
        return {"status": "ok"}

    # ── All other event types — acknowledge but don't process ─────────────────
    else:
        log.debug("paypal_webhook: unhandled event_type=%s — ignored", event_type)
        return {"status": "ok", "detail": f"event {event_type} not handled"}


@app.post("/payments/manual/confirm")
async def manual_payment_confirm(request: Request, user=Depends(require_business)):
    """
    Business owner manually confirms a payment (e.g. after EcoCash transfer is verified).

    Body: { "order_id": 42, "reference": "ORDER-42", "amount": 10.50 }
    """
    from order_lifecycle import get_order, update_order_status_supabase

    body = await request.json()
    order_id  = int(body.get("order_id", 0))
    reference = body.get("reference", f"ORDER-{order_id}")
    amount    = float(body.get("amount", 0))

    if not order_id:
        raise HTTPException(400, "order_id is required")

    order = get_order(order_id)
    if not order:
        raise HTTPException(404, f"Order {order_id} not found")
    if order.get("business_id") != user["business_id"]:
        raise HTTPException(403, "Access denied")

    # Mark as paid
    crud.update_order_payment(order_id, user["business_id"], {
        "payment_status":    "paid",
        "payment_reference": reference,
    })
    try:
        update_order_status_supabase(order_id, "paid")
    except Exception:
        pass

    # Notify customer
    await _notify_customer_payment(order, (
        f"✅ *Payment Confirmed!*\n\n"
        f"Your payment for *{reference}* has been manually verified.\n\n"
        f"💰 Amount: ${amount:.2f}\n"
        f"📦 Your order is confirmed and being prepared. Thank you! 🙏"
    ))

    log.info("manual payment confirmed  order=%s  by=%s", order_id, user["username"])
    return {
        "ok":        True,
        "order_id":  order_id,
        "reference": reference,
        "message":   "Payment confirmed and customer notified.",
    }
