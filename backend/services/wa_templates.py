"""
services/wa_templates.py: sending OFFICIAL, Meta-approved WhatsApp templates (Phase 6 follow-up).

What a template is for: outside WhatsApp's 24-hour customer-service window the ONLY thing a business may
send is a pre-approved template. This module sends one, and only when ALL of these hold:
  * the business has registered a template for the purpose AND marked it approved (it is created and approved
    in Meta's tools, never by this code; WaziBot never invents or submits templates);
  * for a MARKETING purpose, the customer has recorded marketing consent (services/wa_consent.py);
    UTILITY purposes (order updates, booking and payment reminders) are transactional and need no marketing
    consent;
  * the same (business, purpose, reference, customer) has not already been sent (idempotency ledger);
  * the per-run cap has not been reached.
Nothing here is wired to send anything live until a business registers an approved template: with an empty
registry (the state at deploy) every caller behaves exactly as before (skip when the window is closed).

The send is injectable (`sender=`) so everything is tested with mocks; no live template is ever sent by tests.
"""
from __future__ import annotations

import logging
import os
import re
import time

log = logging.getLogger("wazibot")

# purpose -> Meta category (a business rule: marketing needs consent, utility is transactional)
PURPOSES = {
    "order_update": "utility", "booking_reminder": "utility", "payment_reminder": "utility",
    "cart_recovery": "marketing", "reengagement": "marketing", "campaign": "marketing",
}
CONTEXT_KEYS = {"customer_name", "business_name", "order_id", "amount", "date", "time", "service", "link"}
STATUSES = {"pending", "approved", "rejected", "paused", "disabled"}

_RATE_CODES = {130429, 131056, 80007}
_cache: dict = {}
_CACHE_TTL = 60.0


def default_cap() -> int:
    try:
        return max(1, int(os.getenv("WA_TEMPLATE_MAX_PER_RUN", "1000")))
    except ValueError:
        return 1000


# ── registry ──────────────────────────────────────────────────────────────────────────────────────
def get_template(business_id: int, purpose: str) -> dict | None:
    """The newest APPROVED template registered for this purpose, or None (also when the table is missing)."""
    key = (business_id, purpose)
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < _CACHE_TTL:
        return hit[1]
    row = None
    try:
        from core.db import supabase
        res = (supabase.table("whatsapp_templates").select("*").eq("business_id", business_id)
               .eq("purpose", purpose).eq("status", "approved").order("id", desc=True).limit(1).execute())
        row = (res.data or [None])[0]
    except Exception as exc:
        log.debug("wa_templates: registry unavailable: %s", type(exc).__name__)
    if len(_cache) > 500:
        _cache.clear()
    _cache[key] = (time.monotonic(), row)
    return row


def clear_cache() -> None:
    _cache.clear()


def register(business_id: int, purpose: str, name: str, language: str, param_spec) -> dict | None:
    """Register (or update) the metadata of a template that already exists in Meta. Status starts as
    'pending'; the owner marks it 'approved' only after Meta approves it."""
    if purpose not in PURPOSES:
        raise ValueError("unknown purpose")
    name = (name or "").strip()
    language = (language or "en").strip()
    if not re.fullmatch(r"[a-z0-9_]{1,512}", name):
        raise ValueError("template names use lowercase letters, digits and underscores only (as in Meta)")
    if not re.fullmatch(r"[a-z]{2}(_[A-Z]{2})?", language):
        raise ValueError("language must be a Meta language code such as en, en_US, pl, fr")
    spec = [k for k in (param_spec or [])]
    if len(spec) > 10 or any(k not in CONTEXT_KEYS for k in spec):
        raise ValueError("param_spec may list up to 10 of: " + ", ".join(sorted(CONTEXT_KEYS)))
    from core.db import supabase
    row = {"business_id": business_id, "purpose": purpose, "name": name, "language": language,
           "category": PURPOSES[purpose], "param_spec": spec, "updated_at": "now()"}
    row.pop("updated_at")
    res = supabase.table("whatsapp_templates").upsert(row, on_conflict="business_id,name,language").execute()
    clear_cache()
    return (res.data or [None])[0]


def set_status(business_id: int, template_id: int, status: str) -> bool:
    if status not in STATUSES:
        raise ValueError("bad status")
    from core.db import supabase
    res = (supabase.table("whatsapp_templates").update({"status": status})
           .eq("id", template_id).eq("business_id", business_id).execute())
    clear_cache()
    return bool(res.data)


# ── payload ───────────────────────────────────────────────────────────────────────────────────────
def _param_text(value) -> str:
    """Template parameters may not contain newlines, tabs or runs of 4+ spaces (Meta rejects them)."""
    t = re.sub(r"[\r\n\t]+", " ", str(value if value is not None else ""))
    t = re.sub(r" {2,}", " ", t).strip()
    return (t or "-")[:1000]


def build_payload(to: str, template: dict, context: dict) -> dict:
    spec = template.get("param_spec") or []
    if isinstance(spec, str):
        import json
        try:
            spec = json.loads(spec)
        except ValueError:
            spec = []
    payload = {
        "messaging_product": "whatsapp", "recipient_type": "individual", "to": re.sub(r"\D", "", str(to)),
        "type": "template",
        "template": {"name": template["name"], "language": {"code": template.get("language") or "en"}},
    }
    if spec:
        payload["template"]["components"] = [{
            "type": "body",
            "parameters": [{"type": "text", "text": _param_text((context or {}).get(k))} for k in spec],
        }]
    return payload


# ── sending ───────────────────────────────────────────────────────────────────────────────────────
def resolve_credentials(business: dict | None) -> tuple[str, str]:
    """(phone_number_id, token): the business's own number, else the shared WaziBot number."""
    token = phone_id = ""
    try:
        import crud
        token = crud.get_decrypted_token(business) if business else ""
        phone_id = (business or {}).get("whatsapp_phone_id", "") or ""
    except Exception as exc:
        log.debug("wa_templates: credential lookup failed: %s", type(exc).__name__)
    if not token or not phone_id:
        phone_id = os.getenv("SHARED_PHONE_NUMBER_ID", "").strip()
        token = os.getenv("SHARED_WA_TOKEN", "").strip()
    return phone_id, token


def _error_info(body) -> dict:
    err = (body or {}).get("error") if isinstance(body, dict) else None
    if not isinstance(err, dict):
        return {"code": None, "message": "Meta did not accept the message"}
    return {"code": err.get("code"), "subcode": err.get("error_subcode"),
            "message": str(err.get("message") or "")[:200]}


def _default_send(business: dict | None, payload: dict) -> dict:
    """POST the template. Returns {'ok': True, 'id': wamid} or {'ok': False, 'error': {...}}. One bounded retry
    on Meta's rate-limit codes. Never raises, never logs the token or the message text."""
    phone_id, token = resolve_credentials(business)
    if not phone_id or not token:
        return {"ok": False, "error": {"code": "no_credentials", "message": "No WhatsApp credentials configured"}}
    try:
        import requests
        from core.wa_version import graph_url
        url = graph_url(f"{phone_id}/messages")
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        for attempt, wait in enumerate((0, 2)):
            if wait:
                time.sleep(wait)
            resp = requests.post(url, headers=headers, json=payload, timeout=10)
            try:
                body = resp.json()
            except ValueError:
                body = {}
            if resp.status_code == 200 and isinstance(body, dict) and body.get("messages"):
                return {"ok": True, "id": (body["messages"][0] or {}).get("id")}
            info = _error_info(body)
            if info.get("code") in _RATE_CODES and attempt == 0:
                continue
            return {"ok": False, "error": info}
    except Exception as exc:
        log.warning("wa_templates: send failed: %s", type(exc).__name__)
        return {"ok": False, "error": {"code": "network", "message": "Could not reach WhatsApp"}}
    return {"ok": False, "error": {"code": None, "message": "unknown"}}


def classify_error(err: dict | None) -> str:
    """Stable, safe reason code for dashboards: window | rate_limit | template | opt_out | recipient | meta | internal."""
    code = (err or {}).get("code")
    if code == 131047:
        return "window"
    if code in _RATE_CODES:
        return "rate_limit"
    if isinstance(code, int) and 132000 <= code <= 132999:
        return "template"
    if code in (131026, 131030):
        return "recipient"
    if code == 131050:
        return "opt_out"
    if code in ("no_credentials", "network"):
        return "internal"
    return "meta"


def send_template(business_id: int, phone: str, purpose: str, context: dict, *, ref: str,
                  business: dict | None = None, sender=None, sent_in_run: int = 0, cap: int | None = None) -> dict:
    """Send the approved template for `purpose` to `phone`.

    Returns {'ok': True, 'id': ...} or {'ok': False, 'skipped': 'no_template'|'no_consent'|'opted_out'|'duplicate'|
    'cap', ...} (nothing was sent) or {'ok': False, 'error': {...}, 'reason': <classify_error>} (Meta refused)."""
    if purpose not in PURPOSES:
        return {"ok": False, "skipped": "no_template"}
    tpl = get_template(business_id, purpose)
    if not tpl:
        return {"ok": False, "skipped": "no_template"}
    if tpl.get("category") == "marketing" or PURPOSES[purpose] == "marketing":
        from services import wa_consent
        split = wa_consent.split_marketing(business_id, [phone])
        if not split["allowed"]:
            return {"ok": False, "skipped": "opted_out" if split["opted_out"] else "no_consent"}
    if sent_in_run >= (cap if cap is not None else default_cap()):
        return {"ok": False, "skipped": "cap"}

    from crud import notification_ledger as ledger
    digits = re.sub(r"\D", "", str(phone))
    period = f"{purpose}:{ref}:{digits}"
    if not ledger.claim(business_id, "wa_template", period):
        return {"ok": False, "skipped": "duplicate"}
    try:
        result = (sender or _default_send)(business, build_payload(phone, tpl, context))
    except Exception as exc:
        ledger.release(business_id, "wa_template", period)
        log.warning("wa_templates: sender raised: %s", type(exc).__name__)
        return {"ok": False, "error": {"code": "internal", "message": "send failed"}, "reason": "internal"}
    if not (isinstance(result, dict) and result.get("ok")):
        ledger.release(business_id, "wa_template", period)          # the send did not happen: allow a later retry
        err = (result or {}).get("error") if isinstance(result, dict) else None
        return {"ok": False, "error": err or {"code": None, "message": "send failed"}, "reason": classify_error(err)}
    return {"ok": True, "id": result.get("id"), "template": tpl.get("name")}
