"""routes/security_containment_routes.py — Super Admin containment (security audit Stage 4d).

Super Admin only. Disruptive actions need `confirm: true` and a reason. Suspending and restoring a
business use the existing /platform/businesses/{id}/suspend and /activate routes.
"""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from core.auth import require_superadmin
from services import containment, restrictions
from services.security import get_client_ip
from services.security_cases import CaseError

router = APIRouter()


class _Action(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    reason: str = Field(..., min_length=1, max_length=1000)


class _Confirmed(_Action):
    confirm: Literal[True]
    incident_id: Optional[int] = Field(None, ge=1)


class NewRestriction(_Confirmed):
    kind: Literal["ip", "feature"]
    target: str = Field(..., min_length=1, max_length=64)
    feature: Optional[Literal["ai_replies", "campaigns", "uploads"]] = None
    minutes: int = Field(..., ge=containment.MIN_MINUTES, le=containment.MAX_MINUTES)


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except CaseError as exc:
        raise HTTPException(exc.status, exc.message)


@router.get("/admin/saas/security/restrictions")
def restriction_list(request: Request, include_ended: bool = False, page: int = Query(1, ge=1, le=1000),
                     page_size: int = Query(25, ge=1, le=100), user=Depends(require_superadmin)):
    extra = {"your_address": get_client_ip(request), "ip_blocking_enabled": restrictions.ip_blocking_enabled(),
             "features": restrictions.FEATURES, "page": page}
    try:
        rows, has_more = containment.list_restrictions(include_ended, page, page_size)
    except CaseError as exc:
        return {"available": False, "problem": exc.message, "restrictions": [], "has_more": False, **extra}
    return {"available": True, "problem": None, "restrictions": rows, "has_more": has_more, **extra}


@router.post("/admin/saas/security/restrictions", status_code=201)
def restriction_create(body: NewRestriction, request: Request, user=Depends(require_superadmin)):
    return _call(containment.create_restriction, user["username"], kind=body.kind, target=body.target,
                 feature=body.feature, minutes=body.minutes, reason=body.reason, incident_id=body.incident_id,
                 admin_ip=get_client_ip(request))


@router.post("/admin/saas/security/restrictions/{restriction_id}/revoke")
def restriction_revoke(restriction_id: int, body: _Action, request: Request, user=Depends(require_superadmin)):
    return _call(containment.revoke_restriction, user["username"], restriction_id, body.reason,
                 get_client_ip(request))


@router.post("/admin/saas/security/businesses/{business_id}/sign-out")
def business_sign_out(business_id: int, body: _Confirmed, request: Request, user=Depends(require_superadmin)):
    return _call(containment.sign_out_business, user["username"], business_id, body.reason, body.incident_id,
                 get_client_ip(request))
