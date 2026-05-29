"""
services/security.py — Rate Limiting, Spam Prevention, Webhook Security

Phases 2, 3, 4 of the security hardening plan.

All implementations are:
  • Pure Python — no Redis, no Celery, no external services
  • In-process — resets on redeploy (acceptable for Render free/starter)
  • Non-blocking — never crash the request path
  • Render-compatible

RATE LIMITER
────────────
IP-based sliding window. Thread-safe using a lock.
Usage:
    from services.security import rate_limit, RateLimitExceeded
    rate_limit("login", request, max_calls=5, window_seconds=60)

WEBHOOK SIGNATURE VERIFIER
───────────────────────────
Meta sends X-Hub-Signature-256: sha256=<hex> on every webhook POST.
We verify this to prevent spoofed webhook calls.

SPAM / ABUSE DETECTION
────────────────────────
Message fingerprinting prevents:
  • Same customer sending identical message twice in 5s
  • Broadcast endpoint called multiple times in rapid succession
  • Login brute-force (tracked per IP)
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import threading
import time
from collections import defaultdict, deque
from typing import Optional

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# EXCEPTIONS
# ─────────────────────────────────────────────────────────────────────────────

class RateLimitExceeded(Exception):
    """Raised when a rate limit is hit. Caller converts to HTTP 429."""
    def __init__(self, limit_name: str, retry_after: int = 60):
        self.limit_name  = limit_name
        self.retry_after = retry_after
        super().__init__(f"Rate limit exceeded for {limit_name}")


# ─────────────────────────────────────────────────────────────────────────────
# RATE LIMITER — sliding window, in-process
# ─────────────────────────────────────────────────────────────────────────────

_rate_lock  = threading.Lock()
# Structure: { "limit_name:ip": deque([timestamp, ...]) }
_rate_store: dict[str, deque] = defaultdict(deque)


def _get_client_ip(request) -> str:
    """Extract real IP, respecting X-Forwarded-For from Render's proxy."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return getattr(request.client, "host", "unknown")


def rate_limit(
    limit_name:      str,
    request,
    max_calls:       int,
    window_seconds:  int,
    key_suffix:      str = "",
) -> None:
    """
    Sliding window rate limiter.

    Parameters
    ──────────
    limit_name      Identifier for this limit (e.g. "login", "broadcast")
    request         FastAPI Request object
    max_calls       Maximum allowed calls in the window
    window_seconds  Window size in seconds
    key_suffix      Optional extra key (e.g. username for per-user limits)

    Raises RateLimitExceeded if limit is hit.
    Never raises any other exception.
    """
    try:
        ip  = _get_client_ip(request)
        key = f"{limit_name}:{ip}:{key_suffix}"
        now = time.time()

        with _rate_lock:
            window = _rate_store[key]

            # Remove entries outside the window
            cutoff = now - window_seconds
            while window and window[0] < cutoff:
                window.popleft()

            if len(window) >= max_calls:
                log.warning(
                    "rate_limit: EXCEEDED  limit=%s  ip=%s  calls=%d  max=%d",
                    limit_name, ip, len(window), max_calls,
                )
                raise RateLimitExceeded(limit_name, retry_after=window_seconds)

            window.append(now)

    except RateLimitExceeded:
        raise
    except Exception as exc:
        # Rate limiter must never crash the endpoint
        log.debug("rate_limit error (non-fatal): %s", exc)


def get_rate_limit_headers(limit_name: str, request, max_calls: int, window_seconds: int) -> dict:
    """Return X-RateLimit-* headers for the response."""
    try:
        ip  = _get_client_ip(request)
        key = f"{limit_name}:{ip}:"
        now = time.time()
        with _rate_lock:
            window = _rate_store.get(key, deque())
            cutoff = now - window_seconds
            current = sum(1 for t in window if t >= cutoff)
        return {
            "X-RateLimit-Limit":     str(max_calls),
            "X-RateLimit-Remaining": str(max(0, max_calls - current)),
            "X-RateLimit-Reset":     str(int(now + window_seconds)),
        }
    except Exception:
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# LIMITS CONFIG — single place to tune all limits
# ─────────────────────────────────────────────────────────────────────────────

LIMITS = {
    "login":        {"max_calls": 5,  "window": 60},    # 5/min per IP
    "signup":       {"max_calls": 3,  "window": 60},    # 3/min per IP
    "ai_reply":     {"max_calls": 30, "window": 60},    # 30/min per IP
    "broadcast":    {"max_calls": 5,  "window": 60},    # 5/min per IP
    "campaign":     {"max_calls": 5,  "window": 60},    # 5/min per IP
    "payment_verify":{"max_calls": 10,"window": 60},    # 10/min per IP
    "webhook":      {"max_calls": 300,"window": 60},    # 300/min (Meta sends bursts)
    "reminders":    {"max_calls": 3,  "window": 300},   # 3 per 5min
}


def check(name: str, request) -> None:
    """Convenience: check a named limit from LIMITS config."""
    cfg = LIMITS.get(name, {"max_calls": 60, "window": 60})
    rate_limit(name, request, cfg["max_calls"], cfg["window"])


# ─────────────────────────────────────────────────────────────────────────────
# FAILED LOGIN TRACKER
# ─────────────────────────────────────────────────────────────────────────────

_failed_login_lock  = threading.Lock()
_failed_logins: dict[str, list[float]] = defaultdict(list)
FAILED_LOGIN_WINDOW = 300   # 5 minutes
FAILED_LOGIN_MAX    = 10    # lock after 10 failures in window


def record_failed_login(ip: str, username: str) -> None:
    key = f"{ip}:{username.lower()}"
    now = time.time()
    with _failed_login_lock:
        _failed_logins[key].append(now)
        # Prune old entries
        _failed_logins[key] = [t for t in _failed_logins[key]
                                 if now - t < FAILED_LOGIN_WINDOW]
    log.warning("failed_login  ip=%s  username=%s  count=%d",
                ip, username, len(_failed_logins[key]))


def is_login_locked(ip: str, username: str) -> bool:
    key = f"{ip}:{username.lower()}"
    now = time.time()
    with _failed_login_lock:
        recent = [t for t in _failed_logins[key] if now - t < FAILED_LOGIN_WINDOW]
        _failed_logins[key] = recent
    return len(recent) >= FAILED_LOGIN_MAX


def clear_failed_logins(ip: str, username: str) -> None:
    key = f"{ip}:{username.lower()}"
    with _failed_login_lock:
        _failed_logins.pop(key, None)


# ─────────────────────────────────────────────────────────────────────────────
# MESSAGE FINGERPRINTING — duplicate / spam detection
# ─────────────────────────────────────────────────────────────────────────────

_msg_fp_lock  = threading.Lock()
_msg_seen: dict[str, float] = {}   # fingerprint → last_seen timestamp
MSG_DEDUP_WINDOW = 5               # seconds — same message within 5s = duplicate


def is_duplicate_message(phone: str, business_id: int, text: str) -> bool:
    """
    Returns True if this exact (phone, business_id, text) combination
    was seen within MSG_DEDUP_WINDOW seconds. Prevents rapid-fire spam.
    """
    try:
        fp = hashlib.sha256(f"{phone}:{business_id}:{text}".encode()).hexdigest()[:16]
        now = time.time()
        with _msg_fp_lock:
            # Prune old entries
            expired = [k for k, ts in _msg_seen.items() if now - ts > MSG_DEDUP_WINDOW * 10]
            for k in expired:
                del _msg_seen[k]

            if fp in _msg_seen and (now - _msg_seen[fp]) < MSG_DEDUP_WINDOW:
                log.debug("duplicate_message: suppressed  fp=%s  phone=%s", fp, phone)
                return True

            _msg_seen[fp] = now
            return False
    except Exception:
        return False   # never block on error


# ─────────────────────────────────────────────────────────────────────────────
# WEBHOOK SIGNATURE VERIFICATION (Phase 4)
# ─────────────────────────────────────────────────────────────────────────────

def verify_meta_signature(
    payload_bytes: bytes,
    signature_header: str,
    app_secret: str,
) -> bool:
    """
    Verify the X-Hub-Signature-256 header from Meta WebhooksAPI.

    Meta sends: X-Hub-Signature-256: sha256=<hex_digest>
    We recompute: HMAC-SHA256(app_secret, payload_bytes)
    and compare in constant time.

    Returns True if valid, False otherwise.
    If app_secret is not configured, returns True (dev mode — logged as warning).
    """
    if not app_secret:
        log.warning("webhook_signature: WHATSAPP_APP_SECRET not set — skipping signature verification")
        return True

    if not signature_header:
        log.warning("webhook_signature: X-Hub-Signature-256 header missing")
        return False

    if not signature_header.startswith("sha256="):
        log.warning("webhook_signature: unexpected signature format: %r", signature_header[:20])
        return False

    received_hex = signature_header[7:]   # strip "sha256="
    expected = hmac.new(
        app_secret.encode("utf-8"),
        payload_bytes,
        hashlib.sha256,
    ).hexdigest()

    valid = hmac.compare_digest(expected, received_hex)
    if not valid:
        log.error("webhook_signature: INVALID — possible spoofing attempt")
    return valid


# ─────────────────────────────────────────────────────────────────────────────
# INPUT VALIDATION HELPERS (Phase 6 — safe query inputs)
# ─────────────────────────────────────────────────────────────────────────────

_SAFE_ORDER_DIRS = {"asc", "desc"}
_SAFE_SORT_FIELDS = {
    "id", "created_at", "updated_at", "total_price",
    "status", "payment_status", "customer_phone",
}


def safe_sort_field(field: str, default: str = "created_at") -> str:
    """Validate sort field to prevent injection via dynamic Supabase .order() calls."""
    cleaned = field.strip().lower().replace("-", "_")
    if cleaned in _SAFE_SORT_FIELDS:
        return cleaned
    log.warning("safe_sort_field: rejected field=%r, using default=%r", field, default)
    return default


def safe_sort_dir(direction: str) -> bool:
    """Return True for desc, False for asc. Rejects invalid values."""
    return direction.strip().lower() not in {"asc", "ascending", "1", "false"}


def sanitize_string(value: str, max_length: int = 500) -> str:
    """Strip leading/trailing whitespace and enforce max length."""
    if not isinstance(value, str):
        return ""
    return value.strip()[:max_length]


def validate_phone(phone: str) -> str:
    """Strip non-digit characters from phone for safe DB queries."""
    import re
    cleaned = re.sub(r"[^\d+]", "", phone.strip())
    return cleaned[:20]   # max 20 chars


# ─────────────────────────────────────────────────────────────────────────────
# PASSWORD POLICY (Phase 8)
# ─────────────────────────────────────────────────────────────────────────────

def check_password_strength(password: str) -> tuple[bool, str]:
    """
    Check password meets minimum security requirements.

    Returns (ok: bool, message: str).
    Message is empty when ok=True.
    """
    if len(password) < 8:
        return False, "Password must be at least 8 characters"
    if len(password) > 128:
        return False, "Password too long (max 128 characters)"
    # Check for at least one letter and one digit
    has_letter = any(c.isalpha() for c in password)
    has_digit  = any(c.isdigit() for c in password)
    if not has_letter or not has_digit:
        return False, "Password must contain at least one letter and one number"
    return True, ""


# ─────────────────────────────────────────────────────────────────────────────
# FILE UPLOAD VALIDATION (Phase 7)
# ─────────────────────────────────────────────────────────────────────────────

ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic"}
ALLOWED_DOCUMENT_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png"}
MAX_UPLOAD_SIZE_BYTES = 10 * 1024 * 1024   # 10 MB

# Dangerous extensions — always reject
BLOCKED_EXTENSIONS = {
    ".exe", ".sh", ".bat", ".cmd", ".ps1", ".php", ".py", ".js",
    ".jar", ".dll", ".so", ".dylib", ".zip", ".tar", ".gz",
    ".svg",   # SVG can contain scripts
    ".html", ".htm",
}


def validate_upload(
    filename: str,
    content_type: str,
    size_bytes: int,
    images_only: bool = False,
) -> tuple[bool, str]:
    """
    Validate an uploaded file.

    Returns (ok: bool, error_message: str).
    error_message is empty when ok=True.
    """
    import os
    if not filename:
        return False, "No filename provided"

    # Sanitize filename — strip path separators
    safe_name = os.path.basename(filename.replace("\\", "/"))
    if not safe_name or safe_name.startswith("."):
        return False, "Invalid filename"

    ext = os.path.splitext(safe_name)[1].lower()

    if ext in BLOCKED_EXTENSIONS:
        log.warning("upload_validation: BLOCKED extension=%r  filename=%r", ext, filename)
        return False, f"File type '{ext}' is not allowed"

    allowed = ALLOWED_IMAGE_EXTENSIONS if images_only else ALLOWED_DOCUMENT_EXTENSIONS
    if ext not in allowed:
        return False, f"File type '{ext}' is not allowed. Allowed: {', '.join(sorted(allowed))}"

    if size_bytes > MAX_UPLOAD_SIZE_BYTES:
        mb = MAX_UPLOAD_SIZE_BYTES // (1024 * 1024)
        return False, f"File too large (max {mb}MB)"

    # Basic MIME type check
    safe_mime_prefixes = ("image/", "application/pdf")
    if not any(content_type.startswith(p) for p in safe_mime_prefixes):
        log.warning("upload_validation: suspicious MIME  content_type=%r  filename=%r",
                    content_type, filename)
        return False, f"Unexpected file type: {content_type}"

    return True, ""
