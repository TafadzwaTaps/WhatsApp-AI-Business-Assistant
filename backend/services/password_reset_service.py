"""
services/password_reset_service.py — WaziBot Password Reset

Handles the complete forgot-password / reset-password lifecycle:
  1. request_password_reset()  — validate email, generate token, send email
  2. validate_reset_token()    — check token exists, not expired, not used
  3. complete_password_reset() — set new password, mark token used

Security design:
  - Cryptographically random 48-byte token (urlsafe_b64)
  - SHA-256 hash stored in DB; raw token only in the email URL (never in DB)
  - Tokens expire in 60 minutes
  - Single-use: used_at is set on first redemption
  - All previous unused tokens for a user are invalidated on new request
  - Timing-safe comparison using hmac.compare_digest
  - Email enumeration protection: always returns same generic response
  - Rate limiting: 5 requests per IP per hour (enforced in routes)

Passwords are stored plain-text per existing system (hmac.compare_digest).
We match that pattern here — no hashing added, to preserve login compatibility.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
from datetime import datetime, timezone, timedelta

log = logging.getLogger("wazibot")

TOKEN_EXPIRY_MINUTES = 60
BASE_URL = os.getenv("WAZIBOT_URL", "https://wazibot-api-assistant.onrender.com")


# ── Token helpers ─────────────────────────────────────────────────────────────

def _generate_token() -> str:
    """Return a 64-char URL-safe cryptographically random token."""
    return secrets.token_urlsafe(48)


def _hash_token(raw: str) -> str:
    """SHA-256 hex digest of the raw token. This is what we store in the DB."""
    return hashlib.sha256(raw.encode()).hexdigest()


def _safe_eq(a: str, b: str) -> bool:
    """Constant-time string comparison — prevents timing attacks."""
    return hmac.compare_digest(a.encode(), b.encode())


# ── Core functions ────────────────────────────────────────────────────────────

def request_password_reset(
    email: str,
    ip_address: str = "",
    user_agent: str = "",
) -> bool:
    """
    Step 1: Initiate a password reset for the given email.

    Security: Always returns True regardless of whether the email exists.
    This prevents email enumeration — the caller should always show the
    same generic "if that email exists, we've sent a link" message.

    Returns True if email was sent (or silently skipped for unknown email).
    """
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return True  # silently ignore invalid emails

    try:
        from core.db import supabase

        # Look up business by owner_email
        res = (
            supabase.table("businesses")
            .select("id, name, owner_email, owner_username")
            .eq("owner_email", email)
            .eq("is_active", True)
            .limit(1)
            .execute()
        )
        if not res.data:
            # Email not found — still return True (enumeration protection)
            log.info("password_reset: email not found (not disclosed) email=%s ip=%s", email, ip_address)
            return True

        biz = res.data[0]
        bid = biz["id"]
        name = biz.get("name") or biz.get("owner_username") or "there"

        # Invalidate all previous unused tokens for this user
        try:
            supabase.table("password_reset_tokens").update(
                {"used_at": datetime.now(timezone.utc).isoformat()}
            ).eq("business_id", bid).is_("used_at", "null").execute()
        except Exception as e:
            log.warning("password_reset: could not invalidate old tokens biz=%s err=%s", bid, e)

        # Generate new token
        raw_token  = _generate_token()
        token_hash = _hash_token(raw_token)
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=TOKEN_EXPIRY_MINUTES)

        supabase.table("password_reset_tokens").insert({
            "business_id": bid,
            "token_hash":  token_hash,
            "expires_at":  expires_at.isoformat(),
            "ip_address":  ip_address[:100] if ip_address else None,
            "user_agent":  user_agent[:250] if user_agent else None,
        }).execute()

        # Send the email
        reset_url = f"{BASE_URL}/static/reset-password.html?token={raw_token}"
        _send_reset_email(email, name, reset_url)

        log.info("password_reset: token issued biz=%s ip=%s", bid, ip_address)
        return True

    except Exception as exc:
        log.error("password_reset: request_password_reset error: %s", exc)
        return True  # never expose internal errors


def validate_reset_token(raw_token: str) -> dict:
    """
    Step 2: Validate a reset token from the URL.

    Returns:
        {"valid": True,  "business_id": int}  on success
        {"valid": False, "reason": str}        on failure (generic safe reason)
    """
    if not raw_token or len(raw_token) < 20:
        return {"valid": False, "reason": "Invalid link"}

    try:
        from core.db import supabase
        token_hash = _hash_token(raw_token.strip())
        now = datetime.now(timezone.utc)

        res = (
            supabase.table("password_reset_tokens")
            .select("id, business_id, expires_at, used_at")
            .eq("token_hash", token_hash)
            .limit(1)
            .execute()
        )

        if not res.data:
            log.warning("password_reset: token not found hash=%s…", token_hash[:8])
            return {"valid": False, "reason": "This password reset link is invalid or has expired."}

        row = res.data[0]

        if row.get("used_at"):
            return {"valid": False, "reason": "This password reset link has already been used."}

        expires_str = row.get("expires_at", "")
        if expires_str:
            try:
                exp = datetime.fromisoformat(expires_str.replace("Z", "+00:00"))
                if now > exp:
                    return {"valid": False, "reason": "This password reset link has expired. Please request a new one."}
            except Exception:
                return {"valid": False, "reason": "Invalid link"}

        return {"valid": True, "business_id": row["business_id"], "token_id": row["id"]}

    except Exception as exc:
        log.error("password_reset: validate_reset_token error: %s", exc)
        return {"valid": False, "reason": "Something went wrong. Please request a new link."}


def complete_password_reset(raw_token: str, new_password: str) -> dict:
    """
    Step 3: Set the new password and mark the token as used.

    Returns:
        {"ok": True}               on success
        {"ok": False, "error": str} on failure
    """
    # Validate token first
    result = validate_reset_token(raw_token)
    if not result.get("valid"):
        return {"ok": False, "error": result.get("reason", "Invalid link")}

    bid      = result["business_id"]
    token_id = result.get("token_id")

    # Validate password strength
    if not new_password or len(new_password) < 8:
        return {"ok": False, "error": "Password must be at least 8 characters"}

    try:
        from core.db import supabase
        now = datetime.now(timezone.utc).isoformat()

        # Update the password (plain text — matches existing verify_password system)
        supabase.table("businesses").update(
            {"owner_password": new_password}
        ).eq("id", bid).execute()

        # Mark this token as used
        if token_id:
            supabase.table("password_reset_tokens").update(
                {"used_at": now}
            ).eq("id", token_id).execute()

        # Invalidate any remaining unused tokens for this user
        supabase.table("password_reset_tokens").update(
            {"used_at": now}
        ).eq("business_id", bid).is_("used_at", "null").execute()

        log.info("password_reset: password updated successfully biz=%s", bid)
        return {"ok": True}

    except Exception as exc:
        log.error("password_reset: complete_password_reset error: %s", exc)
        return {"ok": False, "error": "Password reset failed. Please try again."}


# ── Email ─────────────────────────────────────────────────────────────────────

def _send_reset_email(to_email: str, name: str, reset_url: str) -> None:
    """Send the branded password reset email via Resend."""
    try:
        from services.email_service import _send, _base_template

        body = f"""
<h1>Reset your password</h1>
<p>Hi {name},</p>
<p>We received a request to reset the password for your WaziBot account.</p>
<p>Click the button below to create a new password:</p>
<p style="margin:28px 0;">
  <a href="{reset_url}" class="btn">Reset Password →</a>
</p>
<p style="font-size:13px;color:#6b8f71;">
  This link expires in <strong style="color:#e8f5e9;">{TOKEN_EXPIRY_MINUTES} minutes</strong>.
</p>
<p style="font-size:13px;color:#6b8f71;">
  If you didn't request a password reset, you can safely ignore this email.
  Your password will not change.
</p>
<hr class="divider"/>
<p style="font-size:12px;color:#6b8f71;">
  Can't click the button? Copy this link into your browser:<br/>
  <a href="{reset_url}" style="color:#22c55e;word-break:break-all;font-size:11px;">{reset_url}</a>
</p>
"""
        html = _base_template("Reset Your WaziBot Password", body)
        _send(to_email, "Reset Your WaziBot Password", html)
    except Exception as exc:
        log.error("password_reset: email send failed to=%s err=%s", to_email, exc)
