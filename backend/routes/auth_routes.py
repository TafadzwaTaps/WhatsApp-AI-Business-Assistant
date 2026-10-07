"""
routes/auth_routes.py — Authentication endpoints.

Routes: POST /auth/signup, POST /auth/login, POST /auth/refresh
"""

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, validator

import crud
from core.auth import (
    verify_password,
    hash_password,
    create_access_token, create_refresh_token,
    decode_token,
    get_current_user, require_superadmin, require_business,
    SUPER_ADMIN_USERNAME, SUPER_ADMIN_PASSWORD, SUPER_ADMIN_LOGIN_DISABLED,
)
from services.security import (
    check as _rate_check,
    record_failed_login, is_login_locked, clear_failed_logins,
    check_password_strength,
    check_signup_abuse, check_signup_success_limit, record_signup_success,
    is_disposable_email,
    get_client_ip,
    check_ip_login_limit, check_account_login_lockout,
    record_failed_login_ip, record_failed_login_account,
    clear_account_login_lockout,
)
from routes._deps import log

router = APIRouter()


def _token_pair(sub: str, role: str, business_id: int | None = None, pwd_ts: str | None = None) -> dict:
    """
    pwd_ts (optional) embeds the business's password_changed_at value at
    the moment this token pair is issued. core.auth.get_current_user()
    compares this against the business's CURRENT value on each request
    (via a short-lived cache) — if they've since reset their password,
    this embedded value goes stale and the token stops being accepted,
    even though its signature and expiry are both still valid. Omitted
    entirely for superadmin tokens (no business_id, nothing to compare).
    """
    data: dict = {"sub": sub, "role": role}
    if business_id is not None:
        data["business_id"] = business_id
    if pwd_ts:
        data["pwd_ts"] = pwd_ts
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
    # Legal/compliance fix: WaziBot is a B2B tool (business owners signing
    # up to run their own store), not a service directed at children, but
    # COPPA liability turns on whether a site *knowingly* collects data
    # from a user under 13 — an unchecked signup form has no way to know
    # that. Rather than collecting date-of-birth (itself more personal
    # data to store and protect), this is a simple, low-friction age/
    # authority attestation: must be explicitly true to sign up at all.
    age_confirmed:     bool = False

    @validator("username")
    def username_valid(cls, v):
        # Spaces are now allowed — usernames are purely a login credential
        # here (matched via .eq("owner_username", ...) in crud.py, never
        # used as a URL path segment or slug — that's derived separately
        # from the business name), so there's no technical reason to
        # forbid them. Uniqueness is still fully enforced below in
        # signup(), unchanged — this only removes the space restriction.
        v = v.strip().lower()
        if len(v) < 3: raise ValueError("Username must be ≥ 3 characters")
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
    # SuperAdmin 2.0 emergency control — checked before any other work
    # (rate limiting, abuse checks, DB lookups) so a paused platform does
    # the least possible work per rejected request. Fails open: if this
    # check itself errors, is_paused() already returns False internally,
    # so signup proceeds normally rather than being blocked by accident.
    from core.platform_controls import is_paused
    if is_paused("signups"):
        raise HTTPException(503, "New signups are temporarily paused. Please try again shortly.")

    if not data.age_confirmed:
        raise HTTPException(
            400,
            "Please confirm you are 18 or older and authorized to create this "
            "business account before signing up.",
        )

    _rate_check("signup", request)
    # Layered signup abuse protection — hourly/daily attempt limits per IP,
    # checked before any other work so a scripted client burns nothing but
    # a rejected request. See services/security.py for the full policy and
    # why this is separate from the short-window "signup" limit above.
    check_signup_abuse(request)
    if is_disposable_email(data.email):
        # Generic message, matching every other signup rejection here —
        # never reveals that the rejection reason was specifically the
        # email domain, which would help an attacker iterate toward a
        # domain that isn't blocked.
        raise HTTPException(400, "We couldn't create your account with that email address. Please use a different email.")

    pw_ok, pw_msg = check_password_strength(data.password)
    if not pw_ok:
        raise HTTPException(400, pw_msg)
    if data.username == SUPER_ADMIN_USERNAME.lower():
        raise HTTPException(400, "Username not available")
    if crud.get_business_by_username(data.username):
        raise HTTPException(400, "Username already taken. Please choose a different username.")

    # Email uniqueness — one account per email address
    _signup_email = (data.email or "").strip().lower()
    if _signup_email:
        try:
            from core.db import supabase as _sdb
            _ec = (
                _sdb.table("businesses")
                .select("id")
                .ilike("owner_email", _signup_email)
                .limit(1)
                .execute()
            )
            if _ec.data:
                raise HTTPException(
                    400,
                    "An account with that email address already exists. "
                    "Please log in or use a different email."
                )
        except HTTPException:
            raise
        except Exception as _ee:
            log.warning("signup: email uniqueness check failed (non-fatal): %s", _ee)

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
        owner_password    = hash_password(data.password)   # bcrypt hash
        owner_email       = data.email.strip().lower() if data.email else ""  # C1
        whatsapp_phone_id = phone_id
        whatsapp_token    = data.whatsapp_token.strip() or None
        category          = data.category.strip() if data.category else ""
        contact_phone     = data.contact_phone.strip() if data.contact_phone else ""
        use_shared_number = data.use_shared_number

    # Checked here — right before creation, not earlier — so it only ever
    # rejects an IP that's actually about to succeed for the second time
    # today, not one still working through unrelated validation errors.
    check_signup_success_limit(request)

    try:
        biz = crud.create_business(_Payload())
    except Exception as _dbe:
        _dbs = str(_dbe).lower()
        if "23505" in _dbs or "unique" in _dbs:
            if "username" in _dbs:
                raise HTTPException(400, "Username already taken. Please choose a different one.")
            if "email" in _dbs:
                raise HTTPException(400, "An account with that email already exists.")
        log.error("signup: DB error: %s", _dbe)
        raise HTTPException(500, "Signup failed. Please try again.")

    # Recorded only now that creation has genuinely succeeded — see
    # check_signup_success_limit's docstring for why this is a separate
    # step rather than folded into the check above.
    record_signup_success(request)
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
    # Bug fix: this endpoint previously computed its own inline IP extraction
    # ("x-forwarded-for"...split(",")[0]) instead of using the shared,
    # already-fixed get_client_ip() — meaning the proxy-spoofing fix from
    # the last round of this audit never actually reached the login
    # endpoint's rate limiting at all. Using the shared function here closes
    # that gap and matches the "reuse, don't duplicate" principle this
    # whole audit is built around.
    ip = get_client_ip(request)

    # Three independent dimensions, all must pass — closes two real gaps
    # in the original single IP+username tracker: an attacker trying many
    # DIFFERENT accounts from one IP (credential stuffing) never tripped
    # the old per-(ip,username) threshold since each pair only ever saw
    # one attempt; and an attacker rotating through many IPs against ONE
    # account (distributed brute force) never tripped it either, since
    # each IP+account pair looked fresh. Time-limited, not permanent —
    # a permanent account lock would let anyone lock a legitimate business
    # out of their own account just by failing a few login attempts.
    if is_login_locked(ip, data.username):
        log.warning("login_locked  ip_account=%s  username=%s", ip, data.username)
        raise HTTPException(429, "Too many failed login attempts. Please try again in 5 minutes.")
    check_ip_login_limit(ip)
    check_account_login_lockout(data.username)

    username = data.username.strip().lower()

    if username == SUPER_ADMIN_USERNAME.lower():
        if SUPER_ADMIN_LOGIN_DISABLED:
            # SUPER_ADMIN_PASSWORD is still the known default — refusing
            # login outright rather than letting a publicly-known password
            # from this codebase work as a real credential.
            log.error("superadmin login attempt blocked — SUPER_ADMIN_PASSWORD not configured  ip=%s", ip)
            raise HTTPException(403, "Superadmin login is disabled until a secure password is configured.")
        if not verify_password(data.password, SUPER_ADMIN_PASSWORD):
            record_failed_login(ip, username)
            record_failed_login_ip(ip)
            record_failed_login_account(username)
            raise HTTPException(401, "Invalid credentials")
        clear_failed_logins(ip, username)
        clear_account_login_lockout(username)
        return {**_token_pair(SUPER_ADMIN_USERNAME, "superadmin"), "role": "superadmin"}

    biz = crud.get_business_by_username(username)
    if not biz or not verify_password(data.password, biz["owner_password"]):
        record_failed_login(ip, username)
        record_failed_login_ip(ip)
        record_failed_login_account(username)
        raise HTTPException(401, "Invalid credentials")
    if not biz.get("is_active", True):
        raise HTTPException(403, "Account suspended. Contact support.")

    # Security hardening: core.auth.verify_password() still accepts legacy
    # plaintext passwords for backward compatibility (any account created
    # before bcrypt hashing was added). A plaintext match here means this
    # account's password has been sitting in the DB unhashed ever since —
    # transparently upgrading it to a bcrypt hash on the very next
    # successful login (the only moment we legitimately have the plaintext
    # password in hand) closes that without requiring a forced reset or
    # changing anything the business sees. Best-effort: never blocks login
    # if the write fails.
    stored_pw = biz.get("owner_password", "")
    if stored_pw and not stored_pw.startswith(("$2b$", "$2a$", "$2y$")):
        try:
            # crud.update_business accepts a plain dict (falls back to
            # dict(data) when the argument has no .dict() method).
            crud.update_business(biz["id"], {"owner_password": hash_password(data.password)})
            log.info("login: upgraded legacy plaintext password to bcrypt  business_id=%s", biz["id"])
        except Exception as exc:
            log.warning("login: legacy password upgrade failed (non-fatal)  business_id=%s  error=%s", biz["id"], exc)

    clear_failed_logins(ip, username)
    clear_account_login_lockout(username)
    log.info("🔑 Login: %s", biz["owner_username"])
    return {
        **_token_pair(biz["owner_username"], "business", biz["id"], biz.get("password_changed_at")),
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
    pwd_ts      = None

    if role == "business":
        biz = crud.get_business_by_username(sub)
        if not biz or not biz.get("is_active", True):
            raise HTTPException(401, "Account not found or suspended.")
        business_id = biz["id"]

        # Reuses the biz row already fetched above — no extra DB query.
        # get_current_user() enforces this same check for regular access
        # tokens, but /auth/refresh has its own separate decode path and
        # never calls get_current_user(), so a stolen refresh token could
        # otherwise keep minting fresh access tokens forever even after a
        # password reset. Checked here directly for that reason.
        current_pwd_ts = biz.get("password_changed_at")
        token_pwd_ts   = payload.get("pwd_ts")
        if token_pwd_ts and current_pwd_ts and current_pwd_ts != token_pwd_ts:
            log.warning("refresh_token rejected: password changed since issuance  business_id=%s", business_id)
            raise HTTPException(401, "Your password was changed. Please log in again.")
        pwd_ts = current_pwd_ts

    log.info("🔄 Token refreshed for: %s", sub)
    return {
        **_token_pair(sub, role, business_id, pwd_ts),
        "role": role,
        **({} if business_id is None else {"business_id": business_id}),
    }


# ── Marketing-email unsubscribe (CAN-SPAM 15 U.S.C. § 7704) ───────────────────
# Public, unauthenticated by design: the link is clicked straight out of an
# email client, often in a different browser/session than the business ever
# logged in with. The HMAC token (not the business's login credentials) is
# what proves the request is legitimate — see email_service._unsubscribe_token.
@router.get("/email/unsubscribe", response_class=HTMLResponse)
def email_unsubscribe(business_id: int, token: str):
    from services.email_service import verify_unsubscribe_token
    if not verify_unsubscribe_token(business_id, token):
        raise HTTPException(400, "Invalid or expired unsubscribe link.")

    # Fail-soft: persisting the opt-out must not crash this page even if the
    # DB write fails — the visible confirmation still renders either way,
    # matching this codebase's existing fail-open/fail-soft convention.
    try:
        from core.db import supabase
        res = (
            supabase.table("businesses")
            .select("features_json")
            .eq("id", business_id)
            .limit(1)
            .execute()
        )
        fj = (res.data[0].get("features_json") if res.data else None) or {}
        fj["marketing_emails_opt_out"] = True
        supabase.table("businesses").update({"features_json": fj}).eq("id", business_id).execute()
        log.info("email: business_id=%s unsubscribed from marketing emails", business_id)
    except Exception as exc:
        log.warning("email: unsubscribe persist failed business_id=%s: %s", business_id, exc)

    return (
        "<!DOCTYPE html><html><head><meta charset='UTF-8'/>"
        "<title>Unsubscribed — WaziBot</title>"
        "<style>body{font-family:Arial,sans-serif;background:#0a0f0d;color:#e8f5e9;"
        "display:flex;align-items:center;justify-content:center;height:100vh;margin:0;}"
        ".box{text-align:center;max-width:420px;padding:32px;}"
        "a{color:#22c55e;}</style></head><body><div class='box'>"
        "<h2>You're unsubscribed</h2>"
        "<p>You won't receive promotional emails from WaziBot at this address anymore. "
        "You'll still get essential account emails (receipts, payment and security notices).</p>"
        "<p><a href='/'>Return to WaziBot</a></p>"
        "</div></body></html>"
    )


# ── Monthly-report opt-out ────────────────────────────────────────────────────
# GET only shows a confirmation page (mail scanners/prefetchers follow links, so
# a GET must not change anything); the POST from that page performs the opt-out.
# Sets the same features_json.pref_weekly_reports flag the dashboard
# Preferences toggle uses, so both stay in sync.
def _report_page(title: str, body: str) -> str:
    return (
        "<!DOCTYPE html><html><head><meta charset='UTF-8'/>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'/>"
        f"<title>{title} — WaziBot</title>"
        "<style>body{font-family:Arial,sans-serif;background:#0a0f0d;color:#e8f5e9;"
        "display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;}"
        ".box{text-align:center;max-width:420px;padding:32px;}a{color:#22c55e;}"
        "button{background:#22c55e;color:#000;border:0;border-radius:9px;padding:12px 24px;"
        "font-weight:800;cursor:pointer;font-size:14px}</style></head><body><div class='box'>"
        f"{body}</div></body></html>"
    )


@router.get("/email/unsubscribe-reports", response_class=HTMLResponse)
def email_unsubscribe_reports_page(business_id: int, token: str):
    from services.email_service import verify_report_unsub_token
    if not verify_report_unsub_token(business_id, token):
        raise HTTPException(400, "Invalid or expired link.")
    return _report_page("Stop reports", (
        "<h2>Stop monthly reports?</h2>"
        "<p>You will no longer receive the monthly performance summary email. "
        "You can turn it back on any time in Dashboard → Profile → Preferences.</p>"
        f"<form method='post' action='/email/unsubscribe-reports?business_id={int(business_id)}&token={token}'>"
        "<button type='submit'>Yes, stop the reports</button></form>"
    ))


@router.post("/email/unsubscribe-reports", response_class=HTMLResponse)
def email_unsubscribe_reports(business_id: int, token: str):
    from services.email_service import verify_report_unsub_token
    if not verify_report_unsub_token(business_id, token):
        raise HTTPException(400, "Invalid or expired link.")
    try:
        from core.db import supabase
        res = (supabase.table("businesses").select("features_json")
               .eq("id", business_id).limit(1).execute())
        fj = (res.data[0].get("features_json") if res.data else None) or {}
        fj["pref_weekly_reports"] = False
        supabase.table("businesses").update({"features_json": fj}).eq("id", business_id).execute()
        log.info("email: business_id=%s opted out of monthly reports", business_id)
    except Exception as exc:
        log.warning("email: report opt-out persist failed business_id=%s: %s", business_id, exc)
        raise HTTPException(500, "Could not save your choice. Please turn reports off in Dashboard → Preferences.")
    return _report_page("Reports stopped", (
        "<h2>Reports turned off</h2>"
        "<p>You won't receive the monthly report anymore. Order and account emails are unaffected.</p>"
        "<p><a href='/'>Return to WaziBot</a></p>"
    ))
