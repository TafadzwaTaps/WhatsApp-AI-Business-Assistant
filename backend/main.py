"""
main.py — WaziBot SaaS API bootstrap.

This file:
  1. Sets up sys.path
  2. Creates the FastAPI app + middleware
  3. Injects runtime config into route modules
  4. Registers all routers

All route logic lives in routes/. See routes/ for details.
"""

import os
import json
import logging
from datetime import datetime

import requests as http_requests
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

# ── sys.path setup ────────────────────────────────────────────────────────────
import sys as _sys, os as _os
_BACKEND = _os.path.dirname(_os.path.abspath(__file__))
if _BACKEND not in _sys.path:
    _sys.path.insert(0, _BACKEND)
_cwd_backend = _os.path.join(_os.getcwd(), "backend")
if _os.path.isdir(_cwd_backend) and _cwd_backend not in _sys.path:
    _sys.path.insert(0, _cwd_backend)

import crud
from core.crypto import TokenDecryptionError
from services.security import RateLimitExceeded, check as _rate_check

load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("wazibot")

# ── Sentry error monitoring (Feature 6) ───────────────────────────────────
# Optional — app starts normally if SENTRY_DSN is not set or sentry-sdk
# is not installed. Never raises. Add to requirements.txt: sentry-sdk>=2.0.0
_SENTRY_DSN = os.getenv("SENTRY_DSN", "").strip()
if _SENTRY_DSN:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration
        sentry_sdk.init(
            dsn=_SENTRY_DSN,
            integrations=[StarletteIntegration(), FastApiIntegration()],
            traces_sample_rate=0.1,   # 10% of requests for performance monitoring
            send_default_pii=False,   # never send PII to Sentry
        )
        log.info("✅ Sentry initialized (DSN configured)")
    except ImportError:
        log.warning("sentry-sdk not installed — add sentry-sdk>=2.0.0 to requirements.txt")
    except Exception as _se:
        log.warning("Sentry init failed (non-fatal): %s", _se)
else:
    log.info("Sentry not configured (SENTRY_DSN not set) — skipping")

# ── Runtime config ────────────────────────────────────────────────────────────
VERIFY_TOKEN           = os.getenv("VERIFY_TOKEN", "myverifytoken123")
WHATSAPP_APP_SECRET    = os.getenv("WHATSAPP_APP_SECRET", "").strip()
SHARED_PHONE_NUMBER_ID = os.getenv("SHARED_PHONE_NUMBER_ID", "").strip()
SHARED_WA_TOKEN        = os.getenv("SHARED_WA_TOKEN", "").strip()
SHARED_WA_PHONE        = os.getenv("SHARED_WA_PHONE", "WaziBot").strip()

# ── Static / invoice dirs ─────────────────────────────────────────────────────
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
# Export STATIC_DIR as env var so route files can find it without repeating the search
os.environ["WAZIBOT_STATIC_DIR"] = STATIC_DIR

INVOICES_DIR = os.path.join(_BASE, "invoices")
os.makedirs(INVOICES_DIR, exist_ok=True)

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="WaziBot API", docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"]  = "nosniff"
        response.headers["X-Frame-Options"]          = "SAMEORIGIN"
        response.headers["Referrer-Policy"]           = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"]        = "camera=(), microphone=(), geolocation=()"
        response.headers["X-XSS-Protection"]          = "1; mode=block"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com data:; "
            "img-src 'self' data: blob: https:; "
            "connect-src 'self' https://wazibot-api-assistant.onrender.com wss:; "
            "frame-ancestors 'self'; "
        )
        return response


app.add_middleware(SecurityHeadersMiddleware)


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request, exc: RateLimitExceeded):
    log.warning("rate_limit_exceeded  endpoint=%s  ip=%s", request.url.path,
                request.headers.get("x-forwarded-for", getattr(request.client, "host", "?")))
    return JSONResponse(
        status_code=429,
        content={"detail": "Too many requests. Please wait and try again."},
        headers={"Retry-After": str(exc.retry_after)},
    )


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _html(name: str) -> FileResponse:
    path = os.path.join(STATIC_DIR, name)
    if not os.path.exists(path):
        from fastapi import HTTPException
        raise HTTPException(404, detail=f"{name} not found")
    return FileResponse(path)


# ── Static page routes ────────────────────────────────────────────────────────
@app.get("/")
def landing():    return _html("landing.html")
@app.get("/dashboard")
def dashboard():  return _html("dashboard.html")

# SaaS public pages — only static fallbacks for slug-based pages
# /onboarding and /directory GET routes are served by the routers below
# to avoid duplicate route registration conflicts
@app.get("/store/{slug}", include_in_schema=False)
def store_fallback(slug: str):    return _html("store.html")

@app.get("/menu/{slug}", include_in_schema=False)
def menu_fallback(slug: str):     return _html("store.html")

@app.get("/config/public")
def public_config():
    """Return public frontend config (Supabase anon key, URL).
    Only the anon key is exposed — never the service_role key.
    """
    import os
    return {
        "supabase_url":       os.getenv("SUPABASE_URL", ""),
        "supabase_anon_key":  os.getenv("SUPABASE_ANON_KEY", "")
                               or os.getenv("SUPABASE_KEY", ""),
    }
@app.get("/inbox")
def inbox():      return _html("inbox.html")
@app.get("/signup")
def signup_page(): return _html("signup.html")
@app.get("/privacy")
def privacy_page(): return _html("privacy.html")
@app.get("/terms")
def terms_page(): return _html("terms.html")

@app.get("/pricing")
def pricing_page(): return _html("pricing.html")

@app.get("/onboarding", include_in_schema=False)
def onboarding_page(): return _html("onboarding.html")

@app.get("/directory", include_in_schema=False)
def directory_page(): return _html("marketplace.html")


# ── WebSocket connection manager ──────────────────────────────────────────────
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


# ── Shared WhatsApp sender ────────────────────────────────────────────────────
def send_whatsapp(phone_number_id: str, token: str, to: str, message: str) -> dict:
    """
    Send a WhatsApp text message via the Meta Cloud API.

    Feature 7: Retry logic with exponential backoff.
    Retries on: timeouts, connection failures, 5xx responses.
    Does NOT retry on 4xx responses (bad credentials, invalid number, etc.).

    Retry schedule:
      Attempt 1 — immediate
      Attempt 2 — after 2 seconds
      Attempt 3 — after 5 seconds

    Return value and API behaviour are identical to the original function.
    """
    import time as _time

    if not phone_number_id or not token:
        missing = [k for k, v in {"phone_number_id": phone_number_id, "token": token}.items() if not v]
        log.error("send_whatsapp: ABORTED — missing %s", missing)
        return {"error": f"missing credentials: {missing}"}

    to  = to.replace("whatsapp:", "").strip()
    url = f"https://graph.facebook.com/v18.0/{phone_number_id}/messages"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    body    = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": message}}

    _RETRY_DELAYS = [0, 2, 5]   # seconds before each attempt (0 = immediate)

    last_exc:  Exception | None = None
    last_result: dict = {}

    for attempt, delay in enumerate(_RETRY_DELAYS, start=1):
        if delay > 0:
            _time.sleep(delay)
        try:
            resp   = http_requests.post(url, headers=headers, json=body, timeout=10)
            result = resp.json()

            if resp.status_code == 200:
                msg_id = (result.get("messages") or [{}])[0].get("id", "?")
                if attempt > 1:
                    log.info("✅ WhatsApp OK (attempt %d)  msg_id=%s  to=%s", attempt, msg_id, to)
                else:
                    log.info("✅ WhatsApp OK  msg_id=%s  to=%s", msg_id, to)
                return result

            # 4xx — do NOT retry (bad request, auth failure, invalid number)
            if 400 <= resp.status_code < 500:
                err = result.get("error", {})
                log.error(
                    "❌ WhatsApp API %d (4xx — no retry)  code=%s  msg=%s",
                    resp.status_code, err.get("code"), err.get("message"),
                )
                return result

            # 5xx — log and retry
            err = result.get("error", {})
            log.warning(
                "⚠️  WhatsApp API %d (attempt %d/%d) — will retry  code=%s  msg=%s",
                resp.status_code, attempt, len(_RETRY_DELAYS),
                err.get("code"), err.get("message"),
            )
            last_result = result

        except (http_requests.exceptions.Timeout,
                http_requests.exceptions.ConnectionError) as exc:
            log.warning(
                "⚠️  send_whatsapp network error (attempt %d/%d): %s",
                attempt, len(_RETRY_DELAYS), exc,
            )
            last_exc = exc

        except Exception as exc:
            # Unexpected error — log and abort (no retry)
            log.exception("send_whatsapp unexpected error: %s", exc)
            return {"error": str(exc)}

    # All retries exhausted
    if last_exc:
        log.error("❌ send_whatsapp: all %d attempts failed  to=%s  last_error=%s",
                  len(_RETRY_DELAYS), to, last_exc)
        return {"error": str(last_exc)}

    log.error("❌ send_whatsapp: all %d attempts failed  to=%s", len(_RETRY_DELAYS), to)
    return last_result or {"error": "all retry attempts failed"}


def _send_direct(phone_number_id: str, token: str, to: str, message: str) -> None:
    if not token:
        log.warning("_send_direct: no token — cannot send to %s", to)
        return
    try:
        result = send_whatsapp(phone_number_id, token, to, message)
        if "error" in result:
            log.error("_send_direct error: %s", result["error"])
    except Exception as exc:
        log.error("_send_direct exception: %s", exc)


def _log_event(event_type: str, **fields) -> None:
    try:
        kv = "  ".join(f"{k}={v!r}" for k, v in fields.items())
        log.info("EVENT %s  %s", event_type, kv)
    except Exception:
        pass


# ── Inject runtime config into route modules ──────────────────────────────────
def _wire_routes():
    """Inject app-level objects into route modules after they're created."""
    import routes.webhook_routes as _wh
    import routes.admin_routes   as _ad
    import routes.business_routes as _biz
    import routes.chat_routes    as _ch

    # webhook_routes
    _wh.VERIFY_TOKEN        = VERIFY_TOKEN
    _wh.WHATSAPP_APP_SECRET = WHATSAPP_APP_SECRET
    _wh.SHARED_PHONE_NUMBER_ID = SHARED_PHONE_NUMBER_ID
    _wh.SHARED_WA_TOKEN     = SHARED_WA_TOKEN
    _wh.manager             = manager
    _wh.send_whatsapp       = send_whatsapp
    _wh._send_direct        = _send_direct
    _wh._log_event          = _log_event
    _wh.INVOICES_DIR        = INVOICES_DIR

    # admin_routes
    _ad.send_whatsapp         = send_whatsapp
    _ad.WHATSAPP_APP_SECRET   = WHATSAPP_APP_SECRET
    _ad.SHARED_PHONE_NUMBER_ID = SHARED_PHONE_NUMBER_ID

    # business_routes
    _biz.send_whatsapp          = send_whatsapp

    # Sprint 1: inject send_whatsapp into crud.orders for owner notifications
    try:
        import crud.orders as _crud_orders
        _crud_orders.send_whatsapp = send_whatsapp
    except Exception as _e:
        log.warning("Sprint1: could not inject send_whatsapp into crud.orders: %s", _e)
    _biz.SHARED_WA_TOKEN        = SHARED_WA_TOKEN
    _biz.SHARED_PHONE_NUMBER_ID = SHARED_PHONE_NUMBER_ID

    # chat_routes
    _ch.send_whatsapp          = send_whatsapp
    _ch.manager                = manager
    _ch.SHARED_WA_TOKEN        = SHARED_WA_TOKEN
    _ch.SHARED_PHONE_NUMBER_ID = SHARED_PHONE_NUMBER_ID


_wire_routes()

# ── Register routers ──────────────────────────────────────────────────────────
from routes.auth_routes     import router as auth_router
from routes.webhook_routes  import router as webhook_router
from routes.admin_routes    import router as admin_router
from routes.business_routes import router as business_router
from routes.chat_routes     import router as chat_router
from routes.growth_routes     import router as growth_router
from routes.expansion_routes  import router as expansion_router
from routes.ux_routes         import router as ux_router

# ── SaaS Extension Routers (optional — try/except so system works if missing) ─
billing_router     = None
saas_admin_router  = None
onboarding_router  = None
marketplace_router = None
_SAAS_ROUTERS_LOADED = False

try:
    from routes.billing_routes import router as billing_router
    log.info("SaaS billing_router loaded")
except Exception as _e:
    log.warning("SaaS billing_routes failed: %s: %s", type(_e).__name__, _e)

try:
    from routes.saas_admin_routes import router as saas_admin_router
    log.info("SaaS saas_admin_router loaded")
except Exception as _e:
    log.warning("SaaS saas_admin_routes failed: %s: %s", type(_e).__name__, _e)

try:
    from routes.onboarding_routes import router as onboarding_router
    log.info("SaaS onboarding_router loaded")
except Exception as _e:
    log.warning("SaaS onboarding_routes failed: %s: %s", type(_e).__name__, _e)

try:
    from routes.marketplace_routes import router as marketplace_router
    log.info("SaaS marketplace_router loaded")
except Exception as _e:
    log.warning("SaaS marketplace_routes failed: %s: %s", type(_e).__name__, _e)

_SAAS_ROUTERS_LOADED = any([billing_router, saas_admin_router, onboarding_router, marketplace_router])
if _SAAS_ROUTERS_LOADED:
    log.info("SaaS routers loaded successfully")
else:
    log.warning("No SaaS routers loaded — check warnings above")

app.include_router(auth_router)
app.include_router(webhook_router)
app.include_router(admin_router)
app.include_router(business_router)
app.include_router(chat_router)
app.include_router(growth_router)
app.include_router(expansion_router)
app.include_router(ux_router)

# SaaS extension routers — only registered if import succeeded
if billing_router:     app.include_router(billing_router)
if saas_admin_router:  app.include_router(saas_admin_router)
if onboarding_router:  app.include_router(onboarding_router)
if marketplace_router: app.include_router(marketplace_router)

# ── Feature 1: Weekly report scheduler ───────────────────────────────────
# Runs in a daemon thread — never blocks startup or requests.
# Fires every Monday at 08:00 UTC. Fails silently if email not configured.
try:
    from services.weekly_report_service import attach_weekly_report_scheduler
    attach_weekly_report_scheduler(app)
except Exception as _wrs_err:
    log.warning("weekly_report_scheduler: failed to start (non-fatal): %s", _wrs_err)

log.info("🚀 WaziBot API started — %d route modules registered", 8)
