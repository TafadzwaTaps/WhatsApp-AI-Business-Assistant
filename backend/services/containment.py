"""services/containment.py — Super Admin containment actions (security audit Stage 4d).

Every action needs a reason, is written to the audit trail and, when an incident is named, to its
timeline. Restrictions always expire and can be lifted early. Nothing here is automatic.
"""
from __future__ import annotations

import datetime as _dt
import ipaddress
import logging
from typing import Optional

from services import restrictions
from services.security_cases import CaseError, _audit, _one, _run, _table, _try_note

log = logging.getLogger(__name__)

MIN_MINUTES, MAX_MINUTES = 5, 30 * 24 * 60
# Cloudflare's published ranges: blocking one would block every visitor routed through it.
_CLOUDFLARE = tuple(ipaddress.ip_network(n) for n in (
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22", "141.101.64.0/18",
    "108.162.192.0/18", "190.93.240.0/20", "188.114.96.0/20", "197.234.240.0/22", "198.41.128.0/17",
    "162.158.0.0/15", "104.16.0.0/13", "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22",
    "2400:cb00::/32", "2606:4700::/32", "2803:f800::/32", "2405:b500::/32", "2405:8100::/32",
    "2a06:98c0::/29", "2c0f:f248::/32"))


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _blockable_address(target: str, admin_ip: Optional[str]) -> str:
    ip = restrictions.normalise_ip(target)
    if ip is None:
        raise CaseError(422, "Enter one IP address (ranges are not supported).")
    address = ipaddress.ip_address(ip)
    if not address.is_global or address.is_multicast:
        raise CaseError(422, "Private, local and reserved addresses cannot be blocked: they may be your own servers.")
    if any(address in network for network in _CLOUDFLARE if network.version == address.version):
        raise CaseError(422, "This is a Cloudflare address. Blocking it would block many visitors.")
    if ip == restrictions.normalise_ip(admin_ip):
        raise CaseError(422, "This is the address you are using now.")
    return ip


def _business(business_id: int) -> dict:
    import crud
    business = crud.get_business_by_id(business_id)
    if not business:
        raise CaseError(404, "No such business.")
    return business


def _note_incident(incident_id: Optional[int], admin: str, body: str) -> list:
    if incident_id is None or _try_note(incident_id, "containment", body, admin):
        return []
    return ["The incident timeline entry was not saved."]


def list_restrictions(include_ended: bool, page: int, page_size: int) -> tuple:
    query = _table("security_restrictions").select("*")
    if not include_ended:
        query = query.is_("revoked_at", "null").gt("expires_at", _now().isoformat())
    start = (page - 1) * page_size
    rows = _run(query.order("created_at", desc=True).order("id", desc=True).range(start, start + page_size),
                "read the restrictions")
    return rows[:page_size], len(rows) > page_size


def create_restriction(admin: str, *, kind: str, target: str, feature: Optional[str], minutes: int,
                       reason: str, incident_id: Optional[int], admin_ip: Optional[str]) -> dict:
    if kind == "ip":
        target, feature = _blockable_address(target, admin_ip), None
    else:
        if feature not in restrictions.FEATURES:
            raise CaseError(422, "Choose which feature to restrict.")
        if not str(target).isdigit():
            raise CaseError(422, "Enter the business ID.")
        target = str(_business(int(target))["id"])
    if not MIN_MINUTES <= minutes <= MAX_MINUTES:
        raise CaseError(422, "A restriction lasts between 5 minutes and 30 days.")
    if incident_id is not None:
        _one("security_incidents", incident_id, "incident")
    existing = _run(_table("security_restrictions").select("*").eq("kind", kind).eq("target", target)
                    .is_("revoked_at", "null"), "read the restrictions")
    now = _now()
    if any(r.get("feature") == feature and (restrictions.expiry_of(r.get("expires_at")) or now) > now
           for r in existing):
        raise CaseError(409, "This restriction is already active. Lift it first to change it.")
    expires = (now + _dt.timedelta(minutes=minutes)).isoformat()
    saved = _run(_table("security_restrictions").insert({
        "kind": kind, "target": target, "feature": feature, "reason": reason, "incident_id": incident_id,
        "created_by": admin, "created_at": now.isoformat(), "expires_at": expires}), "save the restriction")
    if not saved:
        raise CaseError(503, "Could not save the restriction.")
    warnings = [] if restrictions.refresh() else ["Saved, but it may not be enforced yet: the list could not be reloaded."]
    if kind == "ip" and not restrictions.ip_blocking_enabled():
        warnings.append("Saved, but address blocking is switched off on the server (SECURITY_IP_BLOCKING).")
    _audit(admin, "restriction.create", "security_restriction", saved[0]["id"], reason=reason, ip=admin_ip,
           details={"kind": kind, "target": target, "feature": feature, "expires_at": expires,
                    "incident_id": incident_id})
    what = f"Blocked address {target}" if kind == "ip" else f"Restricted {feature} for business {target}"
    warnings += _note_incident(incident_id, admin, f"{what} until {expires}. Reason: {reason}")
    return {"restriction": saved[0], "warnings": warnings}


def revoke_restriction(admin: str, restriction_id: int, reason: str, admin_ip: Optional[str]) -> dict:
    saved = _run(_table("security_restrictions").update(
        {"revoked_at": _now().isoformat(), "revoked_by": admin, "revoke_reason": reason})
        .eq("id", restriction_id).is_("revoked_at", "null"), "lift the restriction")
    if not saved:
        raise CaseError(404, "No active restriction with that number.")
    row = saved[0]
    warnings = [] if restrictions.refresh() else ["Lifted, but the server may enforce it for a little longer."]
    _audit(admin, "restriction.revoke", "security_restriction", restriction_id, reason=reason, ip=admin_ip,
           details={"kind": row.get("kind"), "target": row.get("target"), "feature": row.get("feature")})
    warnings += _note_incident(row.get("incident_id"), admin,
                               f"Lifted restriction {restriction_id} ({row.get('kind')} {row.get('target')}). Reason: {reason}")
    return {"restriction": row, "warnings": warnings}


def sign_out_business(admin: str, business_id: int, reason: str, incident_id: Optional[int],
                      admin_ip: Optional[str]) -> dict:
    """End every signed-in session of a business. The password is not changed: moving
    password_changed_at is the mechanism that already makes older tokens invalid."""
    _business(business_id)
    if incident_id is not None:
        _one("security_incidents", incident_id, "incident")
    saved = _run(_table("businesses").update({"password_changed_at": _now().isoformat()}).eq("id", business_id),
                 "end the sessions")
    if not saved:
        raise CaseError(503, "Could not end the sessions.")
    warnings = []
    try:                                   # drop cached sign-in state so it takes effect at once
        import core.auth as auth
        auth._pwd_ts_cache.pop(business_id, None)
        auth._active_cache.pop(business_id, None)
        from crud.businesses import _biz_cache_clear
        _biz_cache_clear()
    except Exception as exc:
        log.warning("containment: cache not cleared (%s)", type(exc).__name__)
        warnings.append("It may take a minute to take effect.")
    _audit(admin, "business.sessions_revoked", "business", business_id, reason=reason, ip=admin_ip,
           details={"password_changed": False, "incident_id": incident_id})
    warnings += _note_incident(incident_id, admin, f"Signed business {business_id} out everywhere. Reason: {reason}")
    return {"ok": True, "warnings": warnings}
