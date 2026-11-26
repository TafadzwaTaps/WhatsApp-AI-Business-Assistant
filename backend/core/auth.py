"""
auth.py — JWT authentication helpers.

Changes vs original:
  • create_refresh_token() added — issues 7-day tokens for /auth/refresh.
  • Login/signup callers must return both access_token + refresh_token.
  • Everything else is unchanged.
"""

import hmac
import os
import secrets
from datetime import datetime, timedelta

from jose import JWTError, jwt
from fastapi import Depends, HTTPException, status, Request
from fastapi.security import OAuth2PasswordBearer
from dotenv import load_dotenv

load_dotenv()

SECRET_KEY  = os.getenv("SECRET_KEY", "change_this_in_production_use_env_file")
_WEAK_KEYS = {"change_this_in_production_use_env_file", "secret", "dev", "", "mysecret"}
if SECRET_KEY in _WEAK_KEYS:
    # Fixed: previously this only logged a warning and then CONTINUED using
    # the known, hardcoded default — meaning every JWT this server issues
    # would be signed with a secret visible in the source code, letting
    # anyone forge a valid token for any business. A warning that doesn't
    # change behavior doesn't close the hole.
    #
    # Generating a random secret here instead means the hole is closed
    # immediately: no one can forge tokens without knowing this process's
    # in-memory secret, which was never written anywhere. The real cost is
    # that every restart/redeploy invalidates all existing sessions (forces
    # re-login) until SECRET_KEY is actually set in Render — a meaningful
    # but bounded inconvenience, not a security hole, and a strong signal
    # to actually go fix the env var.
    import secrets as _secrets
    SECRET_KEY = _secrets.token_hex(32)
    import logging as _kal; _kal.getLogger("wazibot.security").critical(
        "🚨 SECRET_KEY is unset or uses an insecure default value — generated "
        "a random one for this process instead of using the known default. "
        "ALL SESSIONS WILL BE INVALIDATED ON EVERY RESTART until you set a "
        "real SECRET_KEY in Render env vars. "
        "Generate: python -c \"import secrets; print(secrets.token_hex(32))\""
    )
elif len(SECRET_KEY) < 32:
    # Phase 4: WARNING ONLY. Replacing a short key would invalidate every existing session at the
    # next deploy, so the key is kept; set a 32+ character value (python -c "import secrets;
    # print(secrets.token_hex(32))") in Render at a time when a forced re-login is acceptable.
    import logging as _kal2; _kal2.getLogger("wazibot.security").warning(
        "SECRET_KEY is only %d characters; use at least 32 random characters. Rotating it logs "
        "everyone out once, so schedule it.", len(SECRET_KEY))
ALGORITHM   = "HS256"

ACCESS_TOKEN_EXPIRE_MINUTES  = 60           # 60 minutes (was 8 h); the dashboard refreshes silently via the refresh token
REFRESH_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 days
IMPERSONATION_TOKEN_EXPIRE_MINUTES = 30     # SuperAdmin 2.0 Phase 13 — deliberately
# much shorter than a normal login session: a bounded support-tool window,
# not a standing session. There is no refresh path for this token type
# (create_refresh_token is never called for it) — when it expires, a
# superadmin who still needs access starts a new, freshly audit-logged
# impersonation rather than silently extending the old one forever.

SUPER_ADMIN_USERNAME = os.getenv("SUPER_ADMIN_USERNAME", "superadmin")
SUPER_ADMIN_PASSWORD = os.getenv("SUPER_ADMIN_PASSWORD", "superadmin123")
_WEAK_ADMIN_PWS = {"superadmin123", "admin", "password", "admin123", "wazibot", ""}
# Fixed: previously this only warned — the default "superadmin"/"superadmin123"
# credential kept working for login regardless. verify_password() supports
# plaintext comparison for backward compatibility, so this wasn't even a
# hash anyone would need to crack — the literal password from this file
# would work as-is. SUPER_ADMIN_LOGIN_DISABLED is checked by auth_routes.py
# to refuse ALL superadmin logins outright until a real password is set —
# a credential (unlike SECRET_KEY) can't be safely auto-randomized, since
# the admin needs to know it to log in at all.
SUPER_ADMIN_LOGIN_DISABLED = SUPER_ADMIN_PASSWORD in _WEAK_ADMIN_PWS
if SUPER_ADMIN_LOGIN_DISABLED:
    import logging as _kal2; _kal2.getLogger("wazibot.security").critical(
        "🚨 SUPER_ADMIN_PASSWORD is weak or default — superadmin login is now "
        "DISABLED until you set a strong SUPER_ADMIN_PASSWORD in Render env vars."
    )

class _LoggingOAuth2PasswordBearer(OAuth2PasswordBearer):
    """
    OAuth2PasswordBearer subclass that logs WHY a request was rejected
    before raising "Not authenticated" — this is the 401 that occurs
    when the Authorization header is missing or malformed entirely
    (i.e. before decode_token() ever runs).
    """
    async def __call__(self, request: Request):
        auth_header = request.headers.get("Authorization")
        try:
            return await super().__call__(request)
        except HTTPException as exc:
            # Cookie sessions (Stage 1, flag OFF by default): with no usable Authorization header, an
            # HttpOnly access cookie may authenticate. Bearer always wins when it is present.
            if not auth_header:
                try:
                    from core import cookie_auth as _ck
                    if _ck.enabled():
                        _c = request.cookies.get(_ck.ACCESS_COOKIE)
                        if _c:
                            request.state.auth_from_cookie = True
                            return _c
                except Exception as _ce:
                    import logging as _cl
                    _cl.getLogger("wazibot.auth").debug("cookie auth lookup skipped: %s", type(_ce).__name__)
            import logging as _al
            _al.getLogger("wazibot.auth").warning(
                "AUTH FAILED: reason=missing_or_malformed_auth_header  "
                "path=%s  method=%s  header_present=%s",
                request.url.path,
                request.method,
                bool(auth_header),
            )
            raise


oauth2_scheme = _LoggingOAuth2PasswordBearer(tokenUrl="/auth/login")


def verify_password(plain: str, stored: str) -> bool:
    """
    Verify a password against a stored value.
    Supports both bcrypt hashes ($2b$... / $2a$...) and legacy plaintext
    (backward-compat for existing accounts).  New signups always use bcrypt.
    Uses the 'bcrypt' library directly — avoids passlib version conflicts.
    """
    if stored and stored.startswith(("$2b$", "$2a$", "$2y$")):
        try:
            import bcrypt as _bcrypt
            return _bcrypt.checkpw(
                plain.encode("utf-8")[:72],
                stored.encode("utf-8"),
            )
        except Exception:
            return False
    # Legacy plaintext — constant-time comparison prevents timing attacks
    try:
        return hmac.compare_digest(plain.encode("utf-8"), stored.encode("utf-8"))
    except Exception:
        return False


def hash_password(plain: str) -> str:
    """Hash a password with bcrypt. Uses bcrypt directly — no passlib."""
    import bcrypt as _bcrypt
    pw_bytes = plain.encode("utf-8")[:72]   # bcrypt max is 72 bytes
    return _bcrypt.hashpw(pw_bytes, _bcrypt.gensalt(rounds=12)).decode("utf-8")


def create_access_token(data: dict) -> str:
    payload = data.copy()
    payload["exp"]  = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    payload["type"] = "access"
    payload["jti"]  = secrets.token_urlsafe(16)      # unique id so a single token can be revoked
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def create_refresh_token(data: dict) -> str:
    """Long-lived token used by /auth/refresh to issue a fresh access token."""
    payload = data.copy()
    payload["exp"]  = datetime.utcnow() + timedelta(minutes=REFRESH_TOKEN_EXPIRE_MINUTES)
    payload["type"] = "refresh"
    payload["jti"]  = secrets.token_urlsafe(16)
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def create_impersonation_token(business_id: int, owner_username: str, impersonated_by: str,
                                pwd_ts: str | None = None) -> str:
    """
    SuperAdmin 2.0 (Phase 13) — "View as Business" support tool.

    Issues a normal role="business" token (existing business-scoped routes
    need no changes to accept it) with two differences from a real login:
      - a much shorter expiry (IMPERSONATION_TOKEN_EXPIRE_MINUTES, 30 min
        by default) instead of the usual 8 hours
      - an "impersonated_by" claim carrying the superadmin's own username,
        so get_current_user() can surface it to callers/UI and so the
        token is self-describing evidence of who was acting, even before
        cross-referencing the audit log entry routes/saas_admin_routes.py
        writes when this token is minted.
    pwd_ts is passed through unchanged so the existing password-change
    session-invalidation check keeps working during impersonation too —
    if the business owner resets their password while impersonated, that
    is a reasonable signal to end the impersonation session as well.
    """
    payload = {
        "sub": owner_username, "role": "business", "business_id": business_id,
        "impersonated_by": impersonated_by,
    }
    if pwd_ts:
        payload["pwd_ts"] = pwd_ts
    payload["exp"]  = datetime.utcnow() + timedelta(minutes=IMPERSONATION_TOKEN_EXPIRE_MINUTES)
    payload["type"] = "access"
    payload["jti"]  = secrets.token_urlsafe(16)
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token invalid or expired. Please log in again.",
        )


# ── Session invalidation on password reset ──────────────────────────────────
# The access token is issued with the business's password_changed_at
# timestamp embedded in it (as "pwd_ts"). If the business later resets
# their password, that column gets a new, later timestamp — so any token
# issued BEFORE the reset now has a stale "pwd_ts" and gets rejected here,
# even though its signature and expiry are both still perfectly valid.
# Without this, a stolen/leaked token would stay usable for its full
# 8-hour lifetime no matter what the account owner does about it.
#
# A short-lived cache avoids turning every single authenticated request
# into a database lookup — the auth check stays fast for the overwhelming
# majority of requests, at the cost of revocation taking up to
# _PWD_TS_CACHE_TTL seconds to actually take effect instead of being
# instant. That's a deliberate, bounded tradeoff: near-real-time
# revocation without a DB round-trip on every request.
#
# Fully backward compatible: a token with no "pwd_ts" claim (anything
# issued before this feature existed) skips the check entirely — no
# existing session is invalidated by deploying this.
_pwd_ts_cache: dict[int, tuple[str, float]] = {}
_active_cache: dict[int, tuple[bool, float]] = {}
_PWD_TS_CACHE_TTL = 60  # seconds


def _lookup_auth_state(business_id: int) -> tuple[str | None, bool] | None:
    """(password_changed_at, is_active) for a business, cached for
    _PWD_TS_CACHE_TTL. None on lookup failure or unknown business (callers
    fail open — a DB hiccup must not lock everyone out; signature/expiry
    checks still apply)."""
    import time as _time
    now = _time.time()
    cached_pwd = _pwd_ts_cache.get(business_id)
    cached_act = _active_cache.get(business_id)
    if (cached_pwd and cached_act
            and (now - cached_pwd[1]) < _PWD_TS_CACHE_TTL and (now - cached_act[1]) < _PWD_TS_CACHE_TTL):
        return cached_pwd[0], cached_act[0]
    try:
        from core.db import supabase
        res = (
            supabase.table("businesses")
            .select("password_changed_at, is_active")
            .eq("id", business_id)
            .limit(1)
            .execute()
        )
        if not res.data:
            return None
        row = res.data[0]
        pwd = row.get("password_changed_at")
        active = row.get("is_active") is not False       # NULL / missing column = active
    except Exception:
        return None
    _pwd_ts_cache[business_id] = (pwd, now)
    _active_cache[business_id] = (active, now)
    return pwd, active


def _get_current_password_changed_at(business_id: int) -> str | None:
    state = _lookup_auth_state(business_id)
    return state[0] if state else None


# ── Server-side token revocation (logout) ───────────────────────────────────
# Logout stores the token's `jti` in the revoked_tokens table (see
# backend/migrations/phase3_revoked_tokens.sql). Tokens issued before this feature carry
# no jti and are simply not revocable individually (password change still kills them).
# Revoked ids are also kept in-process so revocation is instant on this instance; other
# lookups are cached for _PWD_TS_CACHE_TTL seconds. A lookup failure (table missing, DB
# down) fails OPEN and is logged - same convention as the checks above - so a DB hiccup
# cannot lock every user out.
_revoked_local: dict[str, float] = {}                # jti -> exp (unix); revoked in this process
_revoked_checked: dict[str, tuple[bool, float]] = {}  # jti -> (revoked, checked_at)
_last_revoked_cleanup = 0.0


def is_token_revoked(jti: str | None) -> bool:
    import time as _time
    if not jti:
        return False
    now = _time.time()
    if jti in _revoked_local:
        return True
    hit = _revoked_checked.get(jti)
    if hit and (now - hit[1]) < _PWD_TS_CACHE_TTL:
        return hit[0]
    revoked = False
    try:
        from core.db import supabase
        res = supabase.table("revoked_tokens").select("jti").eq("jti", jti).limit(1).execute()
        revoked = bool(res.data)
    except Exception as exc:
        import logging as _l
        _l.getLogger("wazibot.auth").warning("revocation lookup failed (failing open): %s", type(exc).__name__)
    if len(_revoked_checked) > 5000:
        _revoked_checked.clear()
    _revoked_checked[jti] = (revoked, now)
    return revoked


def revoke_token(token: str) -> bool:
    """Revoke ONE token (access or refresh). Returns True if it was recorded. Never raises.
    Accepts expired tokens (nothing to do) and garbage (ignored)."""
    import time as _time
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM], options={"verify_exp": False})
    except Exception:
        return False
    jti = payload.get("jti")
    if not jti:
        return False
    exp = float(payload.get("exp") or 0)
    if exp and exp < _time.time():
        return True                                    # already expired: nothing to revoke
    now = _time.time()
    for k in [k for k, e in _revoked_local.items() if e and e < now]:    # prune expired entries
        _revoked_local.pop(k, None)
    _revoked_local[jti] = exp
    try:
        from core.db import supabase
        supabase.table("revoked_tokens").insert({
            "jti": jti,
            "business_id": payload.get("business_id"),
            "expires_at": datetime.utcfromtimestamp(exp).isoformat() + "Z" if exp else None,
        }).execute()
        global _last_revoked_cleanup
        if now - _last_revoked_cleanup > 3600:         # opportunistic cleanup, at most hourly
          _last_revoked_cleanup = now
          try:
            supabase.table("revoked_tokens").delete().lt("expires_at", datetime.utcnow().isoformat() + "Z").execute()
          except Exception as _exc:
            import logging as _l2
            _l2.getLogger("wazibot.auth").debug("revoked_tokens cleanup skipped: %s", type(_exc).__name__)
        return True
    except Exception as exc:
        import logging as _l
        _l.getLogger("wazibot.auth").error("could not persist token revocation: %s", type(exc).__name__)
        return False


# ── Support ("view as business") sessions are READ-ONLY (Phase 5 policy) ──────────────────────
# Enforced here, in the backend, for every route that authenticates through get_current_user:
# a session minted by /admin/saas/tenants/{id}/impersonate may only read. Any write, and any
# GET that reaches a paid or outbound service, is refused. Blocked attempts are audit-logged
# every time; read access is logged once per (support user, business) per 10 minutes.
# Exceptional writes need a separate, explicitly authorised and audited workflow (not built).
_SUPPORT_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_SUPPORT_POST_ALLOW = {"/campaigns/preview"}          # read-only: a dry run that sends nothing
_SUPPORT_GET_DENY_PREFIXES = ("/debug/", "/me/test-whatsapp", "/me/export", "/me/data-export",
                              "/billing/connect", "/billing/invoices", "/marketing/qr/download")
# Anything that exports or downloads data, or fetches an invoice, is refused for a support session
# wherever it lives (these are bulk customer data or outbound calls, not "looking at the dashboard").
_SUPPORT_GET_DENY_FRAGMENTS = ("/export", "/download", "/invoice")
_support_access_logged: dict = {}
_SUPPORT_ACCESS_LOG_INTERVAL = 600


def _audit_support(action: str, payload: dict, request, extra: dict | None = None) -> None:
    try:
        from crud.admin_audit import log_admin_action
        log_admin_action(
            actor_username=payload.get("impersonated_by"), action=action,
            target_type="business", target_id=payload.get("business_id"),
            business_id=payload.get("business_id"),
            metadata={"method": request.method, "path": request.url.path, **(extra or {})},
            ip_address=request.client.host if getattr(request, "client", None) else None,
            user_agent=request.headers.get("user-agent"),
        )
    except Exception as exc:
        import logging as _l
        _l.getLogger("wazibot.auth").warning("SUPPORT AUDIT NOT RECORDED (%s): %s %s admin=%s business=%s",
                                            type(exc).__name__, action, getattr(request, "method", "?"),
                                            payload.get("impersonated_by"), payload.get("business_id"))


def _enforce_support_read_only(request, payload: dict) -> None:
    import logging as _l
    method = (request.method or "").upper()
    path = request.url.path or ""
    denied = (method not in _SUPPORT_SAFE_METHODS and path not in _SUPPORT_POST_ALLOW) or \
             (method in _SUPPORT_SAFE_METHODS and (path.startswith(_SUPPORT_GET_DENY_PREFIXES)
                                                  or any(f in path for f in _SUPPORT_GET_DENY_FRAGMENTS)))
    if denied:
        _l.getLogger("wazibot.auth").warning(
            "SUPPORT SESSION BLOCKED: %s %s  admin=%s  business=%s",
            method, path, payload.get("impersonated_by"), payload.get("business_id"))
        _audit_support("support.write_blocked", payload, request)
        raise HTTPException(status_code=403,
                            detail="Support (view-as) sessions are read-only. This action is not available.")
    if method in _SUPPORT_SAFE_METHODS:
        import time as _t
        key = (payload.get("impersonated_by"), payload.get("business_id"))
        now = _t.time()
        if now - _support_access_logged.get(key, 0) > _SUPPORT_ACCESS_LOG_INTERVAL:
            if len(_support_access_logged) > 2000:
                _support_access_logged.clear()
            _support_access_logged[key] = now
            _audit_support("support.access", payload, request)


def get_current_user(token: str = Depends(oauth2_scheme), request: Request = None) -> dict:
    import logging as _al
    _log = _al.getLogger("wazibot.auth")
    try:
        payload = decode_token(token)
    except HTTPException as exc:
        _log.warning("AUTH FAILED: reason=invalid_or_expired_token  detail=%s", exc.detail)
        raise
    if is_token_revoked(payload.get("jti")):
        _log.warning("AUTH FAILED: reason=token_revoked")
        raise HTTPException(status_code=401, detail="Session ended. Please log in again.")
    username    = payload.get("sub")
    role        = payload.get("role", "business")
    business_id = payload.get("business_id")
    if not username:
        _log.warning("AUTH FAILED: reason=missing_sub_in_token")
        raise HTTPException(status_code=401, detail="Invalid token payload")

    # Cookie sessions: never for support (impersonation) tokens, and state-changing requests need the
    # CSRF double-submit token plus a same-origin signal (Bearer requests are exempt).
    if request is not None and getattr(request.state, "auth_from_cookie", False):
        if payload.get("impersonated_by") or payload.get("role") == "superadmin":
            _log.warning("AUTH FAILED: reason=support_or_admin_token_in_cookie")
            raise HTTPException(status_code=401, detail="Invalid session. Please log in again.")
        from core import cookie_auth as _ck
        if _ck.same_site_fetch_blocked(request):
            _log.warning("AUTH FAILED: reason=cookie_from_other_site  path=%s", request.url.path)
            raise HTTPException(status_code=403, detail="Cross-site request refused.")
        if (request.method or "").upper() not in ("GET", "HEAD", "OPTIONS"):
            if not _ck.check_csrf(request, username, business_id, role):
                _log.warning("AUTH FAILED: reason=csrf_check_failed  path=%s", request.url.path)
                raise HTTPException(status_code=403, detail="CSRF check failed.")

    # SECURITY: a refresh token (7-day lifetime) must never authenticate API
    # calls — it is only valid at /auth/refresh. Previously it was accepted
    # here, silently extending a stolen token's useful life from 8h to 7 days.
    if payload.get("type") == "refresh":
        _log.warning("AUTH FAILED: reason=refresh_token_used_as_access_token")
        raise HTTPException(status_code=401, detail="Invalid token type. Please log in again.")

    # A suspended business is locked out within the cache TTL instead of
    # keeping a working session until its token expires. Impersonation
    # (superadmin support tool) is deliberately exempt so support can still
    # inspect a suspended account.
    if role == "business" and business_id is not None and not payload.get("impersonated_by"):
        state = _lookup_auth_state(business_id)
        if state and state[1] is False:
            _log.warning("AUTH FAILED: reason=account_suspended  business_id=%s", business_id)
            raise HTTPException(status_code=403, detail="Account suspended. Contact support.")

    token_pwd_ts = payload.get("pwd_ts")
    # Phase 4: a business token WITHOUT pwd_ts (signup tokens, or logins before the first password
    # change) is also stale once a password change exists. Impersonation tokens never carry one.
    if business_id is not None and (token_pwd_ts or (role == "business" and not payload.get("impersonated_by"))):
        current_pwd_ts = _get_current_password_changed_at(business_id)
        if current_pwd_ts and current_pwd_ts != token_pwd_ts:
            _log.warning("AUTH FAILED: reason=password_changed_since_token_issued  business_id=%s", business_id)
            raise HTTPException(status_code=401, detail="Your password was changed. Please log in again.")

    result = {"username": username, "role": role, "business_id": business_id}
    impersonated_by = payload.get("impersonated_by")
    if impersonated_by:
        result["impersonated_by"] = impersonated_by
        if request is not None:                        # None only for direct calls (tests, tools)
            _enforce_support_read_only(request, payload)
    # Security audit Stage 3: per-account cap on state-changing calls (429 via the app handler).
    if request is not None and (request.method or "").upper() not in _SUPPORT_SAFE_METHODS:
        from services.security import check_account_write
        check_account_write(role, business_id if business_id is not None else username)
    return result


def require_superadmin(user: dict = Depends(get_current_user)):
    if user["role"] != "superadmin":
        raise HTTPException(status_code=403, detail="Superadmin access required")
    return user


def require_business(user: dict = Depends(get_current_user)):
    import logging as _al
    _al = _al.getLogger("wazibot.auth")
    if user["role"] not in ("business", "superadmin"):
        _al.warning("AUTH FAILED: reason=wrong_role  role=%s  user=%s",
                    user.get("role"), user.get("username"))
        raise HTTPException(status_code=403, detail="Business account required")
    return user


def require_not_impersonated(user: dict = Depends(require_business)):
    """Business login required AND not a support "view as business" token. Used on actions that
    move or redirect money, delete the account, or change who receives payments: support may
    look at a customer's dashboard but must not be able to do these as them."""
    if user.get("impersonated_by"):
        import logging as _al
        _al.getLogger("wazibot.auth").warning(
            "AUTH BLOCKED: impersonation token on a restricted action  admin=%s  business=%s",
            user.get("impersonated_by"), user.get("business_id"))
        raise HTTPException(status_code=403, detail="This action is not available while viewing as another business.")
    return user

