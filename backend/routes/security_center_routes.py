"""routes/security_center_routes.py — read-only Super Admin Security Center API (Stage 4b).

GET only, Super Admin only. The audit trail uses the existing GET /admin/saas/audit-logs.
"""
from __future__ import annotations

import datetime as _dt
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from core.auth import require_superadmin

router = APIRouter()

# Characters allowed in the free-text search. Nothing here can change the meaning of the database
# filter it is placed in (no commas, brackets, quotes, backslashes or wildcards).
SEARCH_PATTERN = r"^[A-Za-z0-9 @._:\-]*$"


@router.get("/admin/saas/security/overview")
def security_overview(days: int = Query(7, ge=1, le=90), user=Depends(require_superadmin)):
    from services.security_center import overview
    return overview(days)


@router.get("/admin/saas/security/events")
def security_event_list(
    date_from:   Optional[_dt.date] = None,
    date_to:     Optional[_dt.date] = None,
    severity:    Optional[Literal["info", "low", "medium", "high", "critical"]] = None,
    event_type:  Optional[str] = Query(None, max_length=80),
    business_id: Optional[int] = Query(None, ge=1),
    username:    Optional[str] = Query(None, max_length=120),
    endpoint:    Optional[str] = Query(None, max_length=200),
    ip:          Optional[str] = Query(None, max_length=64),
    incident_id: Optional[int] = Query(None, ge=1),
    q:           Optional[str] = Query(None, max_length=80, pattern=SEARCH_PATTERN),
    page:        int = Query(1, ge=1, le=1000),
    page_size:   int = Query(25, ge=1, le=100),
    user=Depends(require_superadmin),
):
    """Stored security events, newest first, one page at a time."""
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "The start date is after the end date.")
    from crud.security_events import query_security_events
    from services.security_center import describe
    rows, has_more, problem = query_security_events(
        since_iso=date_from.isoformat() if date_from else None,
        before_iso=(date_to + _dt.timedelta(days=1)).isoformat() if date_to and date_to < _dt.date.max else None,
        severity=severity, event_type=event_type, business_id=business_id, username=username,
        endpoint=endpoint, ip=ip, incident_id=incident_id, search=(q or "").strip() or None,
        offset=(page - 1) * page_size, limit=page_size)
    return {"available": problem is None, "problem": problem, "page": page, "page_size": page_size,
            "has_more": has_more, "events": [describe(r) for r in rows]}
