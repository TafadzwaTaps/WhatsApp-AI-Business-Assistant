"""
services/email_verification_service.py — owner email verification (Phase 4 follow-up).

Design (see docs/ai-development/PHASE_4B_REPORT.md):
  - A 48-byte URL-safe random token is emailed; only its SHA-256 hash is stored.
  - Tokens expire in 24 hours, are single-use (claimed with a conditional UPDATE, so two clicks or
    two requests cannot both succeed) and are bound to the address they were sent to: if the
    owner's email changes before the link is used, the link no longer verifies anything.
  - The link opens a static page that POSTs the token; a mail scanner that merely fetches the link
    does not consume it.
  - Resending is authenticated and always sends to the address already on the account; no email
    is ever taken from the request, so it cannot be used to probe or spam arbitrary addresses.
  - Everything degrades to "feature off" when the migration has not been applied
    (`backend/migrations/email_verification.sql`): no gating, no new emails, no errors.
  - GATING RULE: an account is restricted only when email_verification_required is TRUE and
    email_verified_at is empty. Existing accounts keep required = FALSE (the migration default),
    so nobody loses access; they are NOT marked verified either.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone

log = logging.getLogger("wazibot")

TOKEN_EXPIRY_HOURS = 24

_ready: bool | None = None
_ready_checked_at = 0.0
_REPROBE_SECONDS = 60


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def needs_verification(biz: dict | None) -> bool:
    """True when this account must verify its email before marketing / trial emails and campaigns.
    A row without the new columns (migration not applied) is never gated."""
    if not isinstance(biz, dict):
        return False
    return biz.get("email_verification_required") is True and not biz.get("email_verified_at")


def is_restricted(business_id) -> bool:
    """Look the account up and apply needs_verification(). Fails OPEN (False) on any error or when
    the migration is not applied, so a database hiccup never blocks a legitimate business."""
    if business_id is None:
        return False
    try:
        from core.db import supabase
        res = supabase.table("businesses").select("email_verification_required,email_verified_at") \
            .eq("id", business_id).limit(1).execute()
        return needs_verification((res.data or [None])[0])
    except Exception as exc:
        log.debug("is_restricted: lookup skipped biz=%s: %s", business_id, type(exc).__name__)
        return False


def columns_ready() -> bool:
    """Whether the migration is applied (columns and token table exist). A positive answer is
    cached; a negative one is re-probed every minute so applying the migration takes effect
    without a restart."""
    global _ready, _ready_checked_at
    now = time.monotonic()
    if _ready is True:
        return True
    if _ready is False and (now - _ready_checked_at) < _REPROBE_SECONDS:
        return False
    try:
        from core.db import supabase
        supabase.table("businesses").select("email_verification_required,email_verified_at").limit(1).execute()
        supabase.table("email_verification_tokens").select("id").limit(1).execute()
        _ready = True
    except Exception as exc:
        log.info("email verification not active (migration not applied?): %s", type(exc).__name__)
        _ready = False
    _ready_checked_at = now
    return _ready


def _reset_probe_for_tests() -> None:
    global _ready, _ready_checked_at
    _ready, _ready_checked_at = None, 0.0


def issue_token(business_id: int, email: str, ip: str = "") -> str | None:
    """Create a fresh token for this address (older unused ones stop working). Returns the RAW
    token, or None if it could not be stored."""
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return None
    try:
        from core.db import supabase
        now = _now()
        try:
            supabase.table("email_verification_tokens").update({"used_at": now.isoformat()}) \
                .eq("business_id", business_id).is_("used_at", "null").execute()
        except Exception as exc:
            log.warning("email verification: could not retire old tokens biz=%s: %s", business_id, type(exc).__name__)
        raw = secrets.token_urlsafe(48)
        supabase.table("email_verification_tokens").insert({
            "business_id": business_id,
            "email": email,
            "token_hash": _hash(raw),
            "expires_at": (now + timedelta(hours=TOKEN_EXPIRY_HOURS)).isoformat(),
            "ip_address": (ip or "")[:100] or None,
        }).execute()
        return raw
    except Exception as exc:
        log.error("email verification: could not store token biz=%s: %s", business_id, type(exc).__name__)
        return None


def _verification_email(to_email: str, name: str, url: str) -> bool:
    try:
        from services.email_service import _send, _base_template, _h
        body = f"""
<h1>Confirm your email address</h1>
<p>Hi {_h(name)},</p>
<p>Please confirm that this is your email address so WaziBot can send you trial reminders,
   reports and campaign tools for your business.</p>
<p style="margin:28px 0;"><a href="{url}" class="btn">Confirm my email →</a></p>
<p style="font-size:13px;">This link expires in <strong>{TOKEN_EXPIRY_HOURS} hours</strong> and works once.</p>
<p style="font-size:13px;">If you did not create a WaziBot account, ignore this email and nothing will happen.</p>
<hr class="divider"/>
<p style="font-size:12px;">Can't click the button? Copy this link into your browser:<br/>
<a href="{url}" style="color:#22c55e;word-break:break-all;font-size:11px;">{url}</a></p>
"""
        return bool(_send(to_email, "Confirm your WaziBot email address",
                          _base_template("Confirm your email address", body)))
    except Exception as exc:
        log.error("email verification: send failed: %s", type(exc).__name__)
        return False


def send_verification(business_id: int, email: str, name: str, ip: str = "") -> bool:
    """Issue a token and email the link. True only if both worked."""
    raw = issue_token(business_id, email, ip)
    if not raw:
        return False
    try:
        from services.email_service import _BASE_URL
    except Exception:
        _BASE_URL = "https://wazibothq.com"
    return _verification_email((email or "").strip().lower(), name or "there",
                               f"{_BASE_URL}/static/verify-email.html?token={raw}")


def verify_token(raw: str) -> dict:
    """Redeem a token. Returns {"ok": True, "business_id": id, "first_time": bool} or
    {"ok": False, "reason": generic text}. Never says whether an account or address exists."""
    bad = {"ok": False, "reason": "This verification link is invalid or has expired. Request a new one from your dashboard."}
    raw = (raw or "").strip()
    if len(raw) < 20 or len(raw) > 200:
        return bad
    try:
        from core.db import supabase
        res = supabase.table("email_verification_tokens") \
            .select("id,business_id,email,expires_at,used_at").eq("token_hash", _hash(raw)).limit(1).execute()
        row = (res.data or [None])[0]
        if not row or row.get("used_at"):
            return bad
        try:
            exp = datetime.fromisoformat(str(row.get("expires_at")).replace("Z", "+00:00"))
        except Exception:
            return bad
        if _now() > exp:
            return bad

        # Single use: only one caller can flip used_at from NULL.
        claimed = supabase.table("email_verification_tokens").update({"used_at": _now().isoformat()}) \
            .eq("id", row["id"]).is_("used_at", "null").execute()
        if not claimed.data:
            return bad

        bid = row["business_id"]
        biz_res = supabase.table("businesses").select("id,owner_email,email_verified_at").eq("id", bid).limit(1).execute()
        biz = (biz_res.data or [None])[0]
        if not biz or (biz.get("owner_email") or "").strip().lower() != (row.get("email") or "").strip().lower():
            return bad                                   # the address changed after the link was sent
        first_time = not biz.get("email_verified_at")
        # Conditioned on the address still being the one the link was sent to (closes a narrow
        # race with an email change between the check above and this write).
        done = supabase.table("businesses").update({"email_verified_at": _now().isoformat()}) \
            .eq("id", bid).ilike("owner_email", (row.get("email") or "").strip()).execute()
        if not done.data:
            return bad
        try:
            from crud.businesses import _biz_cache_clear
            _biz_cache_clear()
        except Exception as exc:
            log.debug("verify: cache clear skipped: %s", type(exc).__name__)
        try:
            supabase.table("email_verification_tokens").update({"used_at": _now().isoformat()}) \
                .eq("business_id", bid).is_("used_at", "null").execute()
        except Exception as exc:
            log.debug("verify: leftover token cleanup skipped: %s", type(exc).__name__)
        return {"ok": True, "business_id": bid, "first_time": first_time}
    except Exception as exc:
        log.error("email verification: verify_token error: %s", type(exc).__name__)
        return {"ok": False, "reason": "Something went wrong. Please try again."}


def mark_required(business_id: int) -> bool:
    """Flag a (new, or email-changed) account as needing verification. False if columns missing."""
    try:
        from core.db import supabase
        supabase.table("businesses").update({"email_verification_required": True, "email_verified_at": None}) \
            .eq("id", business_id).execute()
        try:
            from crud.businesses import _biz_cache_clear
            _biz_cache_clear()
        except Exception as exc:
            log.debug("mark_required: cache clear skipped: %s", type(exc).__name__)
        return True
    except Exception as exc:
        log.warning("email verification: could not flag account biz=%s: %s", business_id, type(exc).__name__)
        return False


def status_for(biz: dict | None) -> dict:
    """Small, safe summary for the dashboard."""
    biz = biz or {}
    return {
        "active": "email_verification_required" in biz,
        "required": biz.get("email_verification_required") is True,
        "verified": bool(biz.get("email_verified_at")),
        "restricted": needs_verification(biz),
        "has_email": bool((biz.get("owner_email") or "").strip()),
    }
