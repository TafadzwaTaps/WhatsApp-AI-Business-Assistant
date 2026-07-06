"""
routes/password_reset_routes.py — Forgot Password & Reset Password API

Endpoints:
  POST /auth/forgot-password     — request a reset link (rate limited)
  POST /auth/validate-reset-token — check if a token is still valid
  POST /auth/reset-password      — set new password using token

Security:
  - Rate limited: 5 requests per IP per hour on forgot-password
  - Never reveals whether an email exists (enumeration protection)
  - Token validated server-side on every reset attempt
  - Password strength enforced server-side
"""
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel
from routes._deps import log

router = APIRouter()


class ForgotPasswordRequest(BaseModel):
    email: str


class ValidateTokenRequest(BaseModel):
    token: str


class ResetPasswordRequest(BaseModel):
    token:            str
    new_password:     str
    confirm_password: str


@router.post("/auth/forgot-password")
def forgot_password(data: ForgotPasswordRequest, request: Request):
    """
    Initiate password reset.
    Rate limited: 5 requests per IP per hour.
    Always returns 200 with a generic message (email enumeration protection).
    """
    # Rate limit: 5 per IP per hour
    try:
        from services.security import rate_limit
        rate_limit("forgot_password", request, max_calls=5, window_seconds=3600)
    except Exception as e:
        if "RateLimitExceeded" in type(e).__name__ or "429" in str(e):
            raise HTTPException(
                429,
                "Too many password reset requests. Please wait an hour before trying again."
            )

    ip = _get_ip(request)
    ua = request.headers.get("user-agent", "")[:250]

    from services.password_reset_service import request_password_reset
    request_password_reset(data.email, ip_address=ip, user_agent=ua)

    # Always return the same message — never reveal if email exists
    return {
        "ok":      True,
        "message": "If an account with that email exists, we've sent a password reset link. Check your inbox (and spam folder)."
    }


@router.post("/auth/validate-reset-token")
def validate_reset_token(data: ValidateTokenRequest):
    """
    Check whether a reset token is valid, unexpired, and unused.
    Called by the reset-password page on load to show valid/expired state.
    Returns {"valid": bool, "reason"?: str}
    """
    from services.password_reset_service import validate_reset_token as _validate
    result = _validate(data.token or "")
    # Don't expose business_id to frontend
    return {
        "valid":  result.get("valid", False),
        "reason": result.get("reason", "") if not result.get("valid") else "",
    }


@router.post("/auth/reset-password")
def reset_password(data: ResetPasswordRequest, request: Request):
    """
    Complete the password reset.
    Validates token, checks password strength and match, updates password.
    """
    # Rate limit resets too (prevent brute force on tokens)
    try:
        from services.security import rate_limit
        rate_limit("reset_password", request, max_calls=10, window_seconds=3600)
    except Exception as e:
        if "RateLimitExceeded" in type(e).__name__ or "429" in str(e):
            raise HTTPException(429, "Too many attempts. Please wait before trying again.")

    # Validate passwords match
    if data.new_password != data.confirm_password:
        raise HTTPException(400, "Passwords do not match")

    # Enforce minimum strength (server-side — never trust frontend)
    pw = data.new_password or ""
    errors = []
    if len(pw) < 8:
        errors.append("at least 8 characters")
    if not any(c.isupper() for c in pw):
        errors.append("an uppercase letter")
    if not any(c.islower() for c in pw):
        errors.append("a lowercase letter")
    if not any(c.isdigit() for c in pw):
        errors.append("a number")
    if errors:
        raise HTTPException(400, f"Password must contain {', '.join(errors)}")

    from services.password_reset_service import complete_password_reset
    result = complete_password_reset(data.token, data.new_password)

    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "Password reset failed"))

    log.info("password_reset: success ip=%s", _get_ip(request))
    return {"ok": True, "message": "Password updated successfully. You can now log in with your new password."}


def _get_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:45]
    return getattr(getattr(request, "client", None), "host", "")[:45]
