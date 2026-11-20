"""
core/cookie_auth.py — HttpOnly cookie sessions, STAGE 1 (server side only, behind a flag).

Design and rollback: docs/ai-development/COOKIE_AUTH_DESIGN.md. Decisions approved by the owner:
SameSite=Strict, Bearer-only mobile/API clients unchanged, same-origin browser only.

THE FLAG IS OFF BY DEFAULT, including in production. It is read from the environment on every call
(COOKIE_AUTH_ENABLED=1 turns it on; unset or anything else is off), so it can be switched off again
without a deploy. While it is off this module changes nothing: no cookie is set, no cookie is read.

When on:
  * A client opts in per request with the header  X-Auth-Mode: cookie  (login, signup, refresh). Any
    other client (the Flutter app, scripts, today's dashboard) gets exactly today's JSON, no cookies.
  * Cookies set for an opted-in client:
      __Host-wazi_at    access token    HttpOnly  Secure  SameSite=Strict  Path=/
      __Secure-wazi_rt  refresh token   HttpOnly  Secure  SameSite=Strict  Path=/auth  (refresh + logout only)
      __Host-wazi_csrf  CSRF token      readable  Secure  SameSite=Strict  Path=/       (double-submit)
    The body of an opted-in response carries NO access or refresh token: neither is exposed to JavaScript.
  * Authentication by cookie (no Authorization header) on a state-changing request (POST/PUT/PATCH/DELETE,
    and /auth/refresh) must also pass CSRF checks: X-CSRF-Token header equal to the csrf cookie and to the
    value derived from the session, AND a same-origin Origin (or Sec-Fetch-Site) signal. Requests that
    authenticate with Authorization: Bearer are exempt: they carry no ambient credential.
  * Support (impersonation) tokens are never accepted from a cookie.
"""
from __future__ import annotations

import hashlib
import hmac
import os
from urllib.parse import urlsplit

ACCESS_COOKIE = "__Host-wazi_at"
REFRESH_COOKIE = "__Secure-wazi_rt"
CSRF_COOKIE = "__Host-wazi_csrf"
REFRESH_COOKIE_PATH = "/auth"          # /auth/refresh and /auth/logout need it. The browser also sends it to the rest of
                                       # /auth/* (none of those read it, and it is HttpOnly); narrowing to /auth/refresh
                                       # would stop logout from revoking it, so /auth is the deliberate compromise.
SAMESITE = "strict"                    # approved: Strict
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def enabled() -> bool:
    return os.getenv("COOKIE_AUTH_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


def cookie_mode_requested(request) -> bool:
    """Flag on AND this request opted in with X-Auth-Mode: cookie."""
    try:
        return enabled() and (request.headers.get("x-auth-mode", "").strip().lower() == "cookie")
    except Exception:
        return False


def csrf_token_for(sub, business_id, role) -> str:
    """Derived from the session identity, not stored: valid for the account, useless without the cookie."""
    from core.auth import SECRET_KEY
    # A distinct derived key, so the CSRF value is never an HMAC under the raw signing key itself.
    key = hmac.new(SECRET_KEY.encode(), b"wazibot-csrf-key-v1", hashlib.sha256).digest()
    msg = f"csrf|{sub}|{business_id}|{role}".encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()[:48]


def set_session_cookies(response, access_token: str, refresh_token: str, sub, role, business_id) -> None:
    from core.auth import ACCESS_TOKEN_EXPIRE_MINUTES, REFRESH_TOKEN_EXPIRE_MINUTES
    common = dict(secure=True, samesite=SAMESITE)
    response.set_cookie(ACCESS_COOKIE, access_token, max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
                        path="/", httponly=True, **common)
    response.set_cookie(REFRESH_COOKIE, refresh_token, max_age=REFRESH_TOKEN_EXPIRE_MINUTES * 60,
                        path=REFRESH_COOKIE_PATH, httponly=True, **common)
    response.set_cookie(CSRF_COOKIE, csrf_token_for(sub, business_id, role),
                        max_age=REFRESH_TOKEN_EXPIRE_MINUTES * 60, path="/", httponly=False, **common)
    response.headers["Cache-Control"] = "no-store"


def clear_session_cookies(response) -> None:
    common = dict(secure=True, samesite=SAMESITE)
    response.delete_cookie(ACCESS_COOKIE, path="/", httponly=True, **common)
    response.delete_cookie(REFRESH_COOKIE, path=REFRESH_COOKIE_PATH, httponly=True, **common)
    response.delete_cookie(CSRF_COOKIE, path="/", httponly=False, **common)


def origin_ok(request) -> bool:
    """Same-origin signal for a cookie-authenticated, state-changing request."""
    h = request.headers
    sfs = (h.get("sec-fetch-site") or "").lower()
    if sfs == "cross-site" or sfs == "same-site":
        return False                                   # a sibling subdomain is not our page either
    origin = h.get("origin")
    host = (h.get("host") or "").lower()
    if origin:
        if origin == "null":
            return False
        try:
            netloc = urlsplit(origin).netloc.lower()
        except Exception:
            return False
        extra = {x.strip().rstrip("/").lower() for x in os.getenv("COOKIE_AUTH_ALLOWED_ORIGINS", "").split(",") if x.strip()}
        return (bool(netloc) and netloc == host) or origin.rstrip("/").lower() in extra
    return sfs in ("same-origin", "none") and bool(sfs)


def same_site_fetch_blocked(request) -> bool:
    """True when the browser says this request came from another site or a sibling subdomain.
    Applies to EVERY method for a cookie-authenticated request (Strict still sends the cookie to a
    same-site, cross-origin page such as a sibling subdomain). A missing header is tolerated."""
    return (request.headers.get("sec-fetch-site") or "").lower() in ("cross-site", "same-site")


def check_csrf(request, sub, business_id, role) -> bool:
    """True when the request carries a valid double-submit token AND a same-origin signal."""
    header = request.headers.get("x-csrf-token", "")
    cookie = request.cookies.get(CSRF_COOKIE, "")
    if not header or not cookie:
        return False
    expected = csrf_token_for(sub, business_id, role)
    ok = hmac.compare_digest(header.encode(), cookie.encode()) and hmac.compare_digest(header.encode(), expected.encode())
    return ok and origin_ok(request)
