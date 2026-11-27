"""routes/security_cases_routes.py — Super Admin incidents and vulnerabilities (Stage 4c).

Super Admin only. There is no delete: incidents, notes and findings are kept.
"""
from __future__ import annotations

from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.auth import require_superadmin
from services import security_cases as cases
from services.security import get_client_ip

router = APIRouter()

Severity = Literal["info", "low", "medium", "high", "critical"]
Classification = Literal["suspicious", "suspected_incident", "confirmed_incident", "confirmed_breach"]
Status = Literal["detected", "under_investigation", "contained", "remediation_in_progress",
                 "awaiting_verification", "resolved", "false_positive"]
Source = Literal["dependency_audit", "static_analysis", "secret_scan", "config_check", "staging_scan",
                 "manual_review"]
VulnStatus = Literal["open", "acknowledged", "investigating", "mitigated", "resolved", "accepted_risk",
                     "false_positive"]
CVE_PATTERN = r"^(CVE-\d{4}-\d{4,10})?$"
MAX_ID = 2**53


class _Body(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")


class IncidentFields(_Body):
    summary: Optional[str] = Field(None, max_length=5000)
    assigned_to: Optional[str] = Field(None, max_length=120)
    affected_business_ids: Optional[List[int]] = Field(None, max_length=200)
    affected_accounts: Optional[List[str]] = Field(None, max_length=200)
    data_categories: Optional[List[str]] = Field(None, max_length=50)

    @field_validator("affected_business_ids")
    @classmethod
    def _ids(cls, value):
        if value is not None and any(not 0 < v < MAX_ID for v in value):
            raise ValueError("Business IDs must be positive numbers.")
        return None if value is None else sorted(set(value))

    @field_validator("affected_accounts", "data_categories")
    @classmethod
    def _names(cls, value):
        if value is None:
            return None
        cleaned = [v.strip() for v in value if v.strip()]
        if any(len(v) > 120 for v in cleaned):
            raise ValueError("Each entry may be at most 120 characters.")
        return list(dict.fromkeys(cleaned))


class NewIncident(IncidentFields):
    title: str = Field(..., min_length=1, max_length=200)
    severity: Severity
    classification: Classification = "suspicious"
    event_ids: List[int] = Field(default_factory=list, max_length=200)


class IncidentChange(IncidentFields):
    reason: str = Field(..., min_length=1, max_length=1000)
    title: Optional[str] = Field(None, min_length=1, max_length=200)
    severity: Optional[Severity] = None
    classification: Optional[Classification] = None
    notification_assessment: Optional[str] = Field(None, max_length=5000)


class StatusChange(_Body):
    status: Status
    note: str = Field(..., min_length=1, max_length=5000)
    verification_evidence: Optional[str] = Field(None, max_length=5000)


class Note(_Body):
    kind: Literal["note", "containment", "verification"] = "note"
    body: str = Field(..., min_length=1, max_length=5000)


class EventLink(_Body):
    event_ids: List[int] = Field(..., min_length=1, max_length=200)


class Reason(_Body):
    reason: str = Field(..., min_length=1, max_length=1000)


class VulnerabilityFields(_Body):
    cve: Optional[str] = Field(None, max_length=30, pattern=CVE_PATTERN)
    affected_version: Optional[str] = Field(None, max_length=120)
    fixed_version: Optional[str] = Field(None, max_length=120)
    recommendation: Optional[str] = Field(None, max_length=5000)
    owner: Optional[str] = Field(None, max_length=120)


class NewVulnerability(VulnerabilityFields):
    title: str = Field(..., min_length=1, max_length=200)
    severity: Severity
    component: str = Field(..., min_length=1, max_length=200)
    source: Source
    external_ref: Optional[str] = Field(None, max_length=200)


class VulnerabilityChange(VulnerabilityFields):
    reason: str = Field(..., min_length=1, max_length=1000)
    title: Optional[str] = Field(None, min_length=1, max_length=200)
    severity: Optional[Severity] = None
    component: Optional[str] = Field(None, min_length=1, max_length=200)
    status: Optional[VulnStatus] = None
    evidence: Optional[str] = Field(None, max_length=5000)


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except cases.CaseError as exc:
        raise HTTPException(exc.status, exc.message)


def _sent(body: BaseModel, *, blank_clears: tuple = (), exclude: tuple = ("reason",)) -> dict:
    """Only the fields the caller sent. An empty text clears an optional field."""
    data = body.model_dump(exclude_unset=True, exclude=set(exclude))
    return {k: (None if v == "" and k in blank_clears else v) for k, v in data.items() if v is not None}


_CLEARABLE = ("summary", "assigned_to", "notification_assessment", "cve", "affected_version", "fixed_version",
              "recommendation", "owner", "evidence", "external_ref")


def _listing(key: str, fn, status, severity, page, page_size) -> dict:
    try:
        rows, has_more = fn(status, severity, page, page_size)
    except cases.CaseError as exc:
        return {"available": False, "problem": exc.message, key: [], "page": page, "has_more": False}
    return {"available": True, "problem": None, key: rows, "page": page, "has_more": has_more}


# ── Incidents ────────────────────────────────────────────────────────────────

@router.get("/admin/saas/security/incidents")
def incidents(status: Optional[Status] = None, severity: Optional[Severity] = None,
              page: int = Query(1, ge=1, le=1000), page_size: int = Query(25, ge=1, le=100),
              user=Depends(require_superadmin)):
    return _listing("incidents", cases.list_incidents, status, severity, page, page_size)


@router.post("/admin/saas/security/incidents", status_code=201)
def new_incident(body: NewIncident, request: Request, user=Depends(require_superadmin)):
    fields = _sent(body, blank_clears=_CLEARABLE, exclude=("event_ids",))
    fields.setdefault("classification", body.classification)
    return _call(cases.create_incident, user["username"], fields, body.event_ids, ip=get_client_ip(request))


@router.get("/admin/saas/security/incidents/{incident_id}")
def incident(incident_id: int, user=Depends(require_superadmin)):
    return _call(cases.get_incident, incident_id)


@router.patch("/admin/saas/security/incidents/{incident_id}")
def change_incident(incident_id: int, body: IncidentChange, request: Request, user=Depends(require_superadmin)):
    changes = body.model_dump(exclude_unset=True, exclude={"reason"})
    # "" clears an optional text; null means "leave as it is".
    changes = {k: (None if v == "" and k in _CLEARABLE else v) for k, v in changes.items() if v is not None}
    return _call(cases.update_incident, user["username"], incident_id, changes, body.reason,
                 ip=get_client_ip(request))


@router.post("/admin/saas/security/incidents/{incident_id}/status")
def incident_status(incident_id: int, body: StatusChange, request: Request, user=Depends(require_superadmin)):
    return _call(cases.change_status, user["username"], incident_id, body.status, body.note,
                 body.verification_evidence or None, ip=get_client_ip(request))


@router.post("/admin/saas/security/incidents/{incident_id}/notes", status_code=201)
def incident_note(incident_id: int, body: Note, request: Request, user=Depends(require_superadmin)):
    return _call(cases.add_note, user["username"], incident_id, body.kind, body.body, ip=get_client_ip(request))


@router.post("/admin/saas/security/incidents/{incident_id}/events")
def incident_link(incident_id: int, body: EventLink, request: Request, user=Depends(require_superadmin)):
    return _call(cases.link_events, user["username"], incident_id, body.event_ids, ip=get_client_ip(request))


@router.post("/admin/saas/security/incidents/{incident_id}/events/{event_id}/unlink")
def incident_unlink(incident_id: int, event_id: int, body: Reason, request: Request,
                    user=Depends(require_superadmin)):
    return _call(cases.unlink_event, user["username"], incident_id, event_id, body.reason,
                 ip=get_client_ip(request))


# ── Vulnerabilities ──────────────────────────────────────────────────────────

@router.get("/admin/saas/security/vulnerabilities")
def vulnerabilities(status: Optional[VulnStatus] = None, severity: Optional[Severity] = None,
                    page: int = Query(1, ge=1, le=1000), page_size: int = Query(25, ge=1, le=100),
                    user=Depends(require_superadmin)):
    return _listing("vulnerabilities", cases.list_vulnerabilities, status, severity, page, page_size)


@router.post("/admin/saas/security/vulnerabilities", status_code=201)
def new_vulnerability(body: NewVulnerability, request: Request, user=Depends(require_superadmin)):
    fields = {k: v for k, v in body.model_dump().items() if v not in (None, "")}
    return _call(cases.create_vulnerability, user["username"], fields, ip=get_client_ip(request))


@router.patch("/admin/saas/security/vulnerabilities/{vulnerability_id}")
def change_vulnerability(vulnerability_id: int, body: VulnerabilityChange, request: Request,
                         user=Depends(require_superadmin)):
    changes = body.model_dump(exclude_unset=True, exclude={"reason"})
    changes = {k: (None if v == "" and k in _CLEARABLE else v) for k, v in changes.items() if v is not None}
    return _call(cases.update_vulnerability, user["username"], vulnerability_id, changes, body.reason,
                 ip=get_client_ip(request))
