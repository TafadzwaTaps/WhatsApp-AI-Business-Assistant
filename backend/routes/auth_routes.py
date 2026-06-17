"""
routes/auth_routes.py — Authentication endpoints.

Routes: POST /auth/signup, POST /auth/login, POST /auth/refresh
"""

from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel, validator

import crud
from core.auth import (
    verify_password,
    create_access_token, create_refresh_token,
    decode_token,
    get_current_user, require_superadmin, require_business,
    SUPER_ADMIN_USERNAME, SUPER_ADMIN_PASSWORD,
)
from services.security import (
    check as _rate_check,
    record_failed_login, is_login_locked, clear_failed_logins,
    check_password_strength,
)
from routes._deps import log

router = APIRouter()


def _token_pair(sub: str, role: str, business_id: int | None = None) -> dict:
    data: dict = {"sub": sub, "role": role}
    if business_id is not None:
        data["business_id"] = business_id
    return {
        "access_token":  create_access_token(data),
        "refresh_token": create_refresh_token(data),
        "token_type":    "bearer",
    }


class SignupRequest(BaseModel):
    business_name:      str
    username:           str
    password:           str
    email:             str = ""   # C1 fix: collected at signup, stored as owner_email
    whatsapp_phone_id:  str = ""
    whatsapp_token:     str = ""
    category:          str = ""
    contact_phone:     str = ""
    use_shared_number: bool = True
    ref_code:          str  = ""   # optional referral code from signup link
    # H4: passed from pricing page (?tier=growth&billing_period=annual)
    # Included in signup response so frontend can redirect to checkout
    tier:              str  = ""   # plan tier selected on pricing page
    billing_period:    str  = "monthly"   # "monthly" | "annual"

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


@router.post("/auth/signup")
def signup(data: SignupRequest, request: Request):
    _rate_check("signup", request)
    pw_ok, pw_msg = check_password_strength(data.password)
    if not pw_ok:
        raise HTTPException(400, pw_msg)
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
        owner_email       = data.email.strip().lower() if data.email else ""  # C1
        whatsapp_phone_id = phone_id
        whatsapp_token    = data.whatsapp_token.strip() or None
        category          = data.category.strip() if data.category else ""
        contact_phone     = data.contact_phone.strip() if data.contact_phone else ""
        use_shared_number = data.use_shared_number

    biz = crud.create_business(_Payload())
    log.info("🆕 Signup: %s (@%s)", biz["name"], biz["owner_username"])

    # Auto-start 14-day trial + generate referral code for every new signup
    try:
        from services.growth_service import start_trial
        start_trial(biz["id"])
    except Exception as exc:
        log.warning("trial start failed for biz %s: %s", biz["id"], exc)

    # Record referral if signup came via a referral link (?ref=CODE)
    ref_code = getattr(data, "ref_code", "").strip() if hasattr(data, "ref_code") else ""
    if ref_code:
        try:
            from services.growth_service import record_referral
            record_referral(biz["id"], ref_code)
        except Exception as exc:
            log.warning("referral record failed: %s", exc)

    # ── Feature 3: Send welcome email (non-blocking — signup succeeds regardless) ──
    try:
        from services.email_service import send_welcome_email
        _owner_email = getattr(data, "email", "").strip()
        if _owner_email:
            send_welcome_email(
                to_email=_owner_email,
                business_name=biz["name"],
                username=biz["owner_username"],
            )
    except Exception as _email_exc:
        log.warning("signup: welcome email failed (non-fatal): %s", _email_exc)

    # H4: pass back tier/billing_period so frontend can redirect to checkout
    # if the user arrived from the pricing page with a plan pre-selected
    _tier           = (data.tier or "").strip().lower()
    _billing_period = (data.billing_period or "monthly").strip().lower()

    return {
        **_token_pair(biz["owner_username"], "business", biz["id"]),
        "role":           "business",
        "business_name":  biz["name"],
        "business_id":    biz["id"],
        "selected_tier":  _tier or None,
        "billing_period": _billing_period if _tier else None,
    }


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/auth/login")
def login(data: LoginRequest, request: Request):
    _rate_check("login", request)
    ip = request.headers.get("x-forwarded-for", getattr(request.client, "host", "unknown")).split(",")[0].strip()
    if is_login_locked(ip, data.username):
        log.warning("login_locked  ip=%s  username=%s", ip, data.username)
        raise HTTPException(429, "Too many failed login attempts. Please try again in 5 minutes.")

    username = data.username.strip().lower()

    if username == SUPER_ADMIN_USERNAME.lower():
        if not verify_password(data.password, SUPER_ADMIN_PASSWORD):
            record_failed_login(ip, username)
            raise HTTPException(401, "Invalid credentials")
        clear_failed_logins(ip, username)
        return {**_token_pair(SUPER_ADMIN_USERNAME, "superadmin"), "role": "superadmin"}

    biz = crud.get_business_by_username(username)
    if not biz or not verify_password(data.password, biz["owner_password"]):
        record_failed_login(ip, username)
        raise HTTPException(401, "Invalid credentials")
    if not biz.get("is_active", True):
        raise HTTPException(403, "Account suspended. Contact support.")

    clear_failed_logins(ip, username)
    log.info("🔑 Login: %s", biz["owner_username"])
    return {
        **_token_pair(biz["owner_username"], "business", biz["id"]),
        "role":          "business",
        "business_name": biz["name"],
        "business_id":   biz["id"],
    }


class RefreshRequest(BaseModel):
    refresh_token: str


@router.post("/auth/refresh")
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
