"""
routes/admin_outreach_routes.py — superadmin onboarding / outreach tools.

Purely additive. Three things:

  GET  /admin/outreach/never-activated        businesses with no products and no
                                              orders, days since signup, and
                                              ready-made WhatsApp / email links
  POST /admin/outreach/{id}/restart-trial     restart a trial (reason REQUIRED,
                                              audit entry written)
  POST /admin/outreach/{id}/send-email        send a support email to the owner
                                              through the portal (audited)

WhatsApp note: the Cloud API only allows free-text business-initiated messages
inside the 24h customer-service window, so "one-click WhatsApp" is a wa.me link
opened from the admin's own WhatsApp with the message pre-filled.

All state lives in existing columns / features_json — no migration needed.
"""
import html
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from core.auth import require_superadmin
from crud.admin_audit import log_admin_action

log = logging.getLogger(__name__)
router = APIRouter()

DEFAULT_TRIAL_DAYS = 30
MAX_TRIAL_DAYS = 60
_PAID = ("active", "paid")


def _db():
    from core.db import supabase
    return supabase


def _parse(dt_val) -> Optional[datetime]:
    if not dt_val:
        return None
    try:
        d = datetime.fromisoformat(str(dt_val).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _digits(phone) -> str:
    return re.sub(r"\D", "", str(phone or ""))


def wa_link(phone, text: str = "") -> str:
    d = _digits(phone)
    if len(d) < 7:
        return ""
    return f"https://wa.me/{d}" + (f"?text={quote(text)}" if text else "")


def mailto_link(email, subject: str = "", body: str = "") -> str:
    e = (email or "").strip()
    if "@" not in e:
        return ""
    return f"mailto:{e}?subject={quote(subject)}&body={quote(body)}"


def _ids_with_rows(table: str, ids: list) -> set:
    """Set of business_ids that have at least one row in `table`."""
    found = set()
    try:
        res = _db().table(table).select("business_id").in_("business_id", ids).limit(50000).execute()
        for r in (res.data or []):
            found.add(r.get("business_id"))
    except Exception as exc:
        # Fail loudly: showing someone as "never activated" when we could not
        # check would make admin contact active customers.
        log.error("outreach: %s lookup failed: %s", table, exc)
        raise HTTPException(500, f"Could not check {table}; try again.")
    return found


def _first_name(biz) -> str:
    return (biz.get("name") or "there").strip() or "there"


def _greeting(biz) -> str:
    return (f"Hi, this is the WaziBot team. I noticed {_first_name(biz)} signed up but "
            f"hasn't been set up yet. Can I help you add your products and get your "
            f"WhatsApp assistant working? It takes about 10 minutes.")


@router.get("/admin/outreach/never-activated")
def never_activated(min_days: int = 0, user=Depends(require_superadmin)):
    """Businesses with zero products AND zero orders, oldest signup first."""
    try:
        res = _db().table("businesses").select("*").limit(5000).execute()
    except Exception as exc:
        log.error("outreach: businesses query failed: %s", exc)
        raise HTTPException(500, "Could not load businesses.")
    bizs = [b for b in (res.data or []) if b.get("id") is not None]
    if not bizs:
        return {"count": 0, "items": []}

    ids = [b["id"] for b in bizs]
    with_products = _ids_with_rows("products", ids)
    with_orders = _ids_with_rows("orders", ids)
    now = datetime.now(timezone.utc)

    items = []
    for b in bizs:
        bid = b["id"]
        if bid in with_products or bid in with_orders:
            continue
        created = _parse(b.get("created_at")) or _parse(b.get("trial_started_at"))
        days = (now - created).days if created else None
        if days is not None and days < min_days:
            continue
        feats = b.get("features_json") or {}
        if not isinstance(feats, dict):
            feats = {}
        ends = _parse(b.get("trial_ends_at"))
        status = (b.get("billing_status") or "").lower()
        if status in _PAID:
            trial_state = "paid"
        elif ends and now >= ends:
            trial_state = "expired"
        elif status in ("trialing", "trial"):
            trial_state = "trialing"
        else:
            trial_state = status or "unknown"
        phone = b.get("contact_phone") or b.get("owner_phone") or ""
        email = b.get("owner_email") or ""
        items.append({
            "id": bid,
            "name": b.get("name"),
            "owner_email": email,
            "phone": phone,
            "signed_up": created.isoformat() if created else None,
            "days_since_signup": days,
            "trial_state": trial_state,
            "trial_ends_at": b.get("trial_ends_at"),
            "trial_restarts": int(feats.get("trial_restarts") or 0),
            "last_outreach": feats.get("last_outreach"),
            "whatsapp_link": wa_link(phone, _greeting(b)),
            "email_link": mailto_link(email, "Need a hand setting up WaziBot?", _greeting(b)),
        })
    items.sort(key=lambda i: (i["days_since_signup"] is None, -(i["days_since_signup"] or 0)))
    return {"count": len(items), "items": items}


class RestartTrialBody(BaseModel):
    reason: str = ""
    days: int = DEFAULT_TRIAL_DAYS


def _get_business(business_id: int) -> dict:
    try:
        res = _db().table("businesses").select("*").eq("id", business_id).limit(1).execute()
    except Exception as exc:
        log.error("outreach: business lookup failed: %s", exc)
        raise HTTPException(500, "Could not load business.")
    rows = res.data or []
    if not rows:
        raise HTTPException(404, "Business not found.")
    return rows[0]


def _save_features(business_id: int, feats: dict, extra: Optional[dict] = None):
    payload = {"features_json": feats}
    payload.update(extra or {})
    _db().table("businesses").update(payload).eq("id", business_id).execute()


def _client_meta(request: Request):
    return (request.client.host if request.client else None,
            request.headers.get("user-agent"))


@router.post("/admin/outreach/{business_id}/restart-trial")
def restart_trial(business_id: int, body: RestartTrialBody, request: Request,
                  user=Depends(require_superadmin)):
    reason = (body.reason or "").strip()
    if len(reason) < 5:
        raise HTTPException(422, "A reason (at least 5 characters) is required to restart a trial.")
    days = body.days if isinstance(body.days, int) else DEFAULT_TRIAL_DAYS
    if days < 1 or days > MAX_TRIAL_DAYS:
        raise HTTPException(422, f"Trial length must be between 1 and {MAX_TRIAL_DAYS} days.")

    biz = _get_business(business_id)
    if (biz.get("billing_status") or "").lower() in _PAID:
        raise HTTPException(409, "This business has a paid subscription; its trial cannot be restarted.")

    now = datetime.now(timezone.utc)
    ends = now + timedelta(days=days)
    feats = biz.get("features_json") if isinstance(biz.get("features_json"), dict) else {}
    feats = dict(feats)
    feats["trial_restarts"] = int(feats.get("trial_restarts") or 0) + 1
    feats["last_trial_restart"] = {"at": now.isoformat(), "by": user.get("username"),
                                   "reason": reason[:300], "days": days}
    try:
        _save_features(business_id, feats, {
            "billing_status": "trialing",
            "trial_started_at": now.isoformat(),
            "trial_ends_at": ends.isoformat(),
            "payment_failed_at": None,
        })
    except Exception as exc:
        log.error("outreach: restart trial failed biz=%s: %s", business_id, exc)
        raise HTTPException(500, "Could not restart the trial.")

    ip, ua = _client_meta(request)
    log_admin_action(
        actor_username=user.get("username"), action="business.trial_restart",
        target_type="business", target_id=business_id, business_id=business_id,
        reason=reason,
        metadata={"previous_status": biz.get("billing_status"),
                  "previous_trial_ends_at": biz.get("trial_ends_at"),
                  "new_trial_ends_at": ends.isoformat(), "days": days},
        ip_address=ip, user_agent=ua,
    )
    return {"ok": True, "business_id": business_id, "trial_ends_at": ends.isoformat(), "days": days}


class SendEmailBody(BaseModel):
    subject: str = ""
    message: str = ""


@router.post("/admin/outreach/{business_id}/send-email")
def send_outreach_email(business_id: int, body: SendEmailBody, request: Request,
                        user=Depends(require_superadmin)):
    subject = (body.subject or "").strip()
    message = (body.message or "").strip()
    if not subject or len(subject) > 150:
        raise HTTPException(422, "Subject is required (max 150 characters).")
    if len(message) < 5 or len(message) > 5000:
        raise HTTPException(422, "Message is required (5 to 5000 characters).")

    biz = _get_business(business_id)
    to = (biz.get("owner_email") or "").strip()
    if "@" not in to:
        raise HTTPException(422, "This business has no owner email on file.")

    from services.email_service import _send, _base_template
    safe = html.escape(message).replace("\n", "<br>")
    # business_id intentionally omitted: this is a one-to-one support email,
    # not marketing, so no unsubscribe footer.
    page = _base_template(subject, f'<p style="line-height:1.6">{safe}</p>')
    ok = _send(to, subject, page)

    ip, ua = _client_meta(request)
    log_admin_action(
        actor_username=user.get("username"),
        action="business.outreach_email_sent" if ok else "business.outreach_email_failed",
        target_type="business", target_id=business_id, business_id=business_id,
        reason=subject, metadata={"to": to, "chars": len(message)},
        ip_address=ip, user_agent=ua,
    )
    if not ok:
        raise HTTPException(502, "Email could not be sent (check RESEND_API_KEY / recipient).")

    try:
        feats = biz.get("features_json") if isinstance(biz.get("features_json"), dict) else {}
        feats = dict(feats)
        feats["last_outreach"] = {"at": datetime.now(timezone.utc).isoformat(),
                                  "channel": "email", "by": user.get("username")}
        _save_features(business_id, feats)
    except Exception as exc:
        log.warning("outreach: could not store last_outreach: %s", exc)
    return {"ok": True, "sent_to": to}


class MarkContactedBody(BaseModel):
    channel: str = "whatsapp"


@router.post("/admin/outreach/{business_id}/mark-contacted")
def mark_contacted(business_id: int, body: MarkContactedBody, request: Request,
                   user=Depends(require_superadmin)):
    """Record that the admin reached out via a wa.me / mailto link."""
    channel = body.channel if body.channel in ("whatsapp", "email", "phone") else "whatsapp"
    biz = _get_business(business_id)
    feats = biz.get("features_json") if isinstance(biz.get("features_json"), dict) else {}
    feats = dict(feats)
    feats["last_outreach"] = {"at": datetime.now(timezone.utc).isoformat(),
                              "channel": channel, "by": user.get("username")}
    try:
        _save_features(business_id, feats)
    except Exception as exc:
        log.error("outreach: mark contacted failed: %s", exc)
        raise HTTPException(500, "Could not save.")
    ip, ua = _client_meta(request)
    log_admin_action(actor_username=user.get("username"), action="business.outreach_contacted",
                     target_type="business", target_id=business_id, business_id=business_id,
                     reason=f"contacted via {channel}", ip_address=ip, user_agent=ua)
    return {"ok": True}
