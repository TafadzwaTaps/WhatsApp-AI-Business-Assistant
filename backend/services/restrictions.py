"""services/restrictions.py — enforcing temporary restrictions (security audit Stage 4d).

Restrictions are rows in security_restrictions. Every check reads an in-memory copy, refreshed in
the background at most every CACHE_SECONDS, so no request waits on the database.

FAIL OPEN: if the list cannot be read, the last good copy is used for STALE_SECONDS, after which
nothing is restricted and a high-severity security event is recorded. Without the database change
there are no restrictions. Expiry is checked at the moment of use.

IP blocks never apply to the payment and WhatsApp webhooks or the health check, nor to a request
carrying a valid Super Admin token. SECURITY_IP_BLOCKING=0 switches IP blocking off entirely.
"""
from __future__ import annotations

import datetime as _dt
import ipaddress
import logging
import os
import threading
import time
from typing import Optional

from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse

log = logging.getLogger(__name__)

CACHE_SECONDS = 30
STALE_SECONDS = 300
FEATURES = {
    "ai_replies": "AI replies to customers",
    "campaigns": "Campaigns and broadcasts",
    "uploads": "Image uploads",
}
EXEMPT_PATHS = frozenset({"/webhook", "/payment/webhook", "/payments/paypal/webhook", "/billing/webhook",
                          "/healthz"})
_NOTHING = {"ips": {}, "features": {}}

_lock = threading.Lock()
_snapshot: dict = _NOTHING
_tried_at: Optional[float] = None
_failing_since: Optional[float] = None


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def normalise_ip(value) -> Optional[str]:
    """One canonical spelling per address ("::ffff:1.2.3.4" is 1.2.3.4); None if not an address."""
    try:
        address = ipaddress.ip_address(str(value).strip())
    except ValueError:
        return None
    mapped = getattr(address, "ipv4_mapped", None)
    return str(mapped or address)


def expiry_of(value) -> Optional[_dt.datetime]:
    if not value:
        return None
    try:
        parsed = _dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=_dt.timezone.utc)


def _build(rows: list) -> dict:
    ips, features = {}, {}
    for row in rows:
        if row.get("revoked_at"):
            continue
        expires = expiry_of(row.get("expires_at"))
        if row.get("expires_at") and expires is None:
            continue                                   # unreadable expiry: never restrict forever
        if row.get("kind") == "ip":
            ip = normalise_ip(row.get("target"))
            if ip and expires:                         # an address block always ends
                ips[ip] = expires
        elif row.get("kind") == "feature":
            features[(str(row.get("target")), row.get("feature"))] = expires
    return {"ips": ips, "features": features}


def refresh() -> bool:
    """Read the list now. Returns False if it could not be read."""
    global _snapshot, _failing_since
    try:
        from core.db import supabase
        rows = (supabase.table("security_restrictions").select("kind,target,feature,expires_at,revoked_at")
                .is_("revoked_at", "null").order("created_at", desc=True).limit(1000).execute()).data or []
        _snapshot, _failing_since = _build(rows), None
        return True
    except Exception as exc:
        if str(getattr(exc, "code", "")) in ("42P01", "PGRST205"):      # database change not applied
            _snapshot, _failing_since = _NOTHING, None
            return True
        now = time.monotonic()
        if _failing_since is None:
            _failing_since = now
        log.warning("restrictions: list unreadable (%s)", type(exc).__name__)
        if now - _failing_since >= STALE_SECONDS:
            from services.security_events import emit
            emit("restriction_check_failed", rule="list_unreadable")
        return False


def _refresh_in_background() -> None:
    threading.Thread(target=refresh, name="restrictions-refresh", daemon=True).start()


def _current() -> dict:
    global _tried_at
    now = time.monotonic()
    with _lock:
        due = _tried_at is None or now - _tried_at >= CACHE_SECONDS
        if due:
            _tried_at = now
    if due:
        _refresh_in_background()
    if _failing_since is not None and now - _failing_since >= STALE_SECONDS:
        return _NOTHING
    return _snapshot


def _in_force(expires: Optional[_dt.datetime]) -> bool:
    return expires is None or expires > _now()


def ip_blocking_enabled() -> bool:
    return os.getenv("SECURITY_IP_BLOCKING", "1").strip().lower() not in ("0", "false", "no", "off")


def feature_blocked(business_id, feature: str) -> bool:
    """True while this business's feature is restricted. Never raises (False on any problem)."""
    try:
        features = _current()["features"]
        key = (str(business_id), feature)
        if key not in features or not _in_force(features[key]):
            return False
        from services.security_events import emit
        emit("restricted_feature_blocked", business_id=business_id, rule=feature)
        return True
    except Exception as exc:
        log.warning("restrictions: feature check failed (%s)", type(exc).__name__)
        return False


def _is_super_admin(connection: HTTPConnection) -> bool:
    header = connection.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return False
    try:
        from core.auth import decode_token
        payload = decode_token(header[7:].strip())
        return payload.get("role") == "superadmin" and payload.get("type") != "refresh"
    except Exception:
        return False


def _refused(scope) -> bool:
    try:
        if not ip_blocking_enabled():
            return False
        blocked = _current()["ips"]
        if not blocked or (scope.get("path", "").rstrip("/") or "/") in EXEMPT_PATHS:
            return False
        connection = HTTPConnection(scope)
        from services.security import get_client_ip
        ip = normalise_ip(get_client_ip(connection))
        if ip not in blocked or not _in_force(blocked[ip]) or _is_super_admin(connection):
            return False
        from services.security_events import emit
        emit("ip_blocked_request", ip=ip, endpoint=scope.get("path"))
        return True
    except Exception as exc:
        log.warning("restrictions: address check failed (%s)", type(exc).__name__)
        return False


class IPRestrictionMiddleware:
    """Refuses requests and sockets from blocked addresses; everything else passes untouched."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket") and _refused(scope):
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 4003})
                return
            response = JSONResponse({"detail": "Access from this address is temporarily restricted."},
                                    status_code=403)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def _reset_for_tests() -> None:
    global _snapshot, _tried_at, _failing_since
    _snapshot, _tried_at, _failing_since = _NOTHING, None, None
