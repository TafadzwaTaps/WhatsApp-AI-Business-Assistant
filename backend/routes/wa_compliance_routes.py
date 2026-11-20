"""
routes/wa_compliance_routes.py: owner API for WhatsApp marketing consent and the approved-template registry.

  GET  /me/whatsapp/consent              status of the feature and counts
  POST /me/whatsapp/consent              record consent (or an opt-out) the business has collected elsewhere
  GET  /me/whatsapp/templates            list registered templates and the allowed purposes
  POST /me/whatsapp/templates            register the metadata of a template that already exists in Meta
  POST /me/whatsapp/templates/{id}/status  mark it approved / disabled after checking Meta

Everything is tenant-scoped by the business id in the token. Writes are refused for support (view-as) sessions.
WaziBot never creates or submits templates: they are created and approved in Meta's own tools.
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from core.auth import require_business, require_not_impersonated
from routes._deps import log
from services.security import rate_limit

router = APIRouter()

# Sources a business may attest to from the dashboard (a customer's own STOP/SUBSCRIBE message is recorded
# by the webhook as "customer_message" and cannot be forged here).
_OWNER_SOURCES = {"dashboard", "website_form", "in_store", "phone_call", "qr", "import"}


class ConsentRecord(BaseModel):
    phones: List[str] = Field(..., min_length=1, max_length=500)
    status: str = Field(..., pattern="^(opted_in|opted_out)$")
    source: str = Field("dashboard", max_length=40)
    note: Optional[str] = Field(None, max_length=200)


class TemplateRegister(BaseModel):
    purpose: str = Field(..., max_length=40)
    name: str = Field(..., max_length=200)
    language: str = Field("en", max_length=10)
    param_spec: List[str] = Field(default_factory=list, max_length=10)


class TemplateStatus(BaseModel):
    status: str = Field(..., pattern="^(pending|approved|rejected|paused|disabled)$")


@router.get("/me/whatsapp/consent")
def consent_overview(user=Depends(require_business)):
    from services import wa_consent
    bid = user["business_id"]
    out = {"available": wa_consent.available(), "enforced": wa_consent.enforced(), "opted_in": 0, "opted_out": 0}
    if out["available"]:
        try:
            from core.db import supabase
            rows = (supabase.table("whatsapp_marketing_consent_current").select("status")
                    .eq("business_id", bid).limit(20000).execute().data) or []
            out["opted_in"] = sum(1 for r in rows if r.get("status") == "opted_in")
            out["opted_out"] = sum(1 for r in rows if r.get("status") == "opted_out")
        except Exception as exc:
            log.warning("consent overview failed: %s", type(exc).__name__)
    return out


@router.post("/me/whatsapp/consent")
def consent_record(body: ConsentRecord, request: Request, user=Depends(require_not_impersonated)):
    from services import wa_consent
    bid = user["business_id"]
    rate_limit("wa_consent", request, 20, 3600, key_suffix=str(bid))
    if body.source not in _OWNER_SOURCES:
        raise HTTPException(422, "source must be one of: " + ", ".join(sorted(_OWNER_SOURCES)))
    if not wa_consent.available():
        raise HTTPException(503, "Consent tracking is not enabled yet for this service.")
    if body.status == "opted_in" and not (body.note or "").strip():
        raise HTTPException(422, "Describe how this consent was collected (for example 'ticked the box on our order form').")
    done = rejected = 0
    for phone in body.phones:
        ok = wa_consent.record(bid, phone, body.status, body.source, (body.note or "")[:200], user.get("username") or "")
        done += 1 if ok else 0
        rejected += 0 if ok else 1
    log.info("wa consent recorded biz=%s status=%s source=%s ok=%d rejected=%d", bid, body.status, body.source, done, rejected)
    return {"ok": True, "recorded": done, "rejected": rejected}


@router.get("/me/whatsapp/templates")
def templates_list(user=Depends(require_business)):
    from services import wa_templates
    rows = []
    try:
        from core.db import supabase
        rows = (supabase.table("whatsapp_templates").select("*").eq("business_id", user["business_id"])
                .order("id", desc=True).limit(200).execute().data) or []
    except Exception as exc:
        log.debug("templates list unavailable: %s", type(exc).__name__)
    return {"templates": rows, "purposes": wa_templates.PURPOSES, "param_keys": sorted(wa_templates.CONTEXT_KEYS)}


@router.post("/me/whatsapp/templates")
def templates_register(body: TemplateRegister, request: Request, user=Depends(require_not_impersonated)):
    from services import wa_templates
    bid = user["business_id"]
    rate_limit("wa_templates", request, 30, 3600, key_suffix=str(bid))
    try:
        row = wa_templates.register(bid, body.purpose, body.name, body.language, body.param_spec)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    except Exception as exc:
        log.warning("template register failed biz=%s: %s", bid, type(exc).__name__)
        raise HTTPException(503, "Template registry is not enabled yet for this service.")
    return {"ok": True, "template": row, "note": "Registered as pending. Mark it approved only after Meta has approved it."}


@router.post("/me/whatsapp/templates/{template_id}/status")
def templates_status(template_id: int, body: TemplateStatus, user=Depends(require_not_impersonated)):
    from services import wa_templates
    try:
        ok = wa_templates.set_status(user["business_id"], template_id, body.status)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    if not ok:
        raise HTTPException(404, "Template not found")
    return {"ok": True, "status": body.status}
