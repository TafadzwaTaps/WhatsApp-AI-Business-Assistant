"""
services/wa_consent.py: per-business WhatsApp MARKETING consent (opt-in / opt-out), approved Phase 6 decision.

Rules implemented:
  * Consent is recorded per (business, phone) as an append-only history (table whatsapp_consent_events) with a
    reliable current-state view (whatsapp_marketing_consent_current). Each event keeps the source, timestamp,
    business id and status. Opting out of one business never touches another.
  * UNKNOWN consent means NOT opted in. Nothing is inferred from the fact that someone once messaged the
    business, and nothing is inferred for existing customers.
  * STOP, UNSUBSCRIBE and clearly equivalent messages opt the customer out immediately; an explicit
    SUBSCRIBE / OPT IN (or START after an opt-out) opts them back in. Nothing else changes consent.
  * Only MARKETING is gated. Transactional messages (order, booking and payment updates) are unaffected.
  * While the migration is not applied (see migrations/whatsapp_consent_and_templates.sql) the feature is OFF:
    no gating and no keyword handling, exactly as before. WA_MARKETING_CONSENT_REQUIRED=0 is an emergency
    switch that turns gating off without touching the database (recording continues).
"""
from __future__ import annotations

import logging
import os
import re
import time

log = logging.getLogger("wazibot")

SOURCES = {"customer_message", "dashboard", "website_form", "in_store", "phone_call", "qr", "import"}
_PHONE_OK = re.compile(r"^[0-9]{6,16}$")
_CHUNK = 100
_REPROBE_SECONDS = 60

_ready = None
_ready_at = 0.0


def norm_phone(phone) -> str:
    """WhatsApp ids are digits without '+': strip everything else."""
    return re.sub(r"\D", "", str(phone or ""))


# ── availability / enforcement ────────────────────────────────────────────────────────────────────
def available() -> bool:
    """True when the consent table and the current-state view exist. A positive answer is cached; a
    negative one is re-probed every minute, so applying the migration needs no restart."""
    global _ready, _ready_at
    now = time.monotonic()
    if _ready is True:
        return True
    if _ready is False and (now - _ready_at) < _REPROBE_SECONDS:
        return False
    try:
        from core.db import supabase
        supabase.table("whatsapp_consent_events").select("id").limit(1).execute()
        supabase.table("whatsapp_marketing_consent_current").select("phone").limit(1).execute()
        _ready = True
    except Exception as exc:
        log.info("wa_consent: not active (migration not applied?): %s", type(exc).__name__)
        _ready = False
    _ready_at = now
    return _ready


def _reset_for_tests() -> None:
    global _ready, _ready_at
    _ready, _ready_at = None, 0.0


def enforced() -> bool:
    """Marketing consent is required only when the data exists AND the emergency switch is not off."""
    if os.getenv("WA_MARKETING_CONSENT_REQUIRED", "").strip().lower() in {"0", "false", "no", "off"}:
        return False
    return available()


# ── reads / writes ────────────────────────────────────────────────────────────────────────────────
def record(business_id: int, phone, status: str, source: str, evidence: str = "", recorded_by: str = "") -> bool:
    """Append one consent event. Returns False (and records nothing) for bad input or a failed write."""
    p = norm_phone(phone)
    if status not in ("opted_in", "opted_out") or source not in SOURCES or not _PHONE_OK.match(p):
        return False
    try:
        from core.db import supabase
        supabase.table("whatsapp_consent_events").insert({
            "business_id": business_id, "phone": p, "category": "marketing", "status": status,
            "source": source, "evidence": (evidence or "")[:200] or None,
            "recorded_by": (recorded_by or "")[:100] or None,
        }).execute()
        return True
    except Exception as exc:
        log.warning("wa_consent: could not record event biz=%s: %s", business_id, type(exc).__name__)
        return False


def statuses(business_id: int, phones) -> dict:
    """{normalised phone: 'opted_in' | 'opted_out'} for the phones that have a recorded status; any phone
    with no record is simply absent (= unknown = not opted in). A failure returns what was read so far."""
    wanted = [p for p in dict.fromkeys(norm_phone(x) for x in (phones or [])) if _PHONE_OK.match(p)]
    out: dict = {}
    try:
        from core.db import supabase
        for i in range(0, len(wanted), _CHUNK):
            rows = (supabase.table("whatsapp_marketing_consent_current").select("phone,status")
                    .eq("business_id", business_id).in_("phone", wanted[i:i + _CHUNK]).execute().data) or []
            for r in rows:
                out[str(r.get("phone"))] = r.get("status")
    except Exception as exc:
        log.warning("wa_consent: lookup failed biz=%s: %s", business_id, type(exc).__name__)
    return out


def split_marketing(business_id: int, phones) -> dict:
    """Who may receive MARKETING now. {'allowed': [...], 'no_consent': [...], 'opted_out': [...]} (order kept).
    When consent is not enforced everyone is allowed (behaviour before the migration)."""
    phones = list(phones or [])
    if not enforced():
        return {"allowed": phones, "no_consent": [], "opted_out": []}
    st = statuses(business_id, phones)
    res = {"allowed": [], "no_consent": [], "opted_out": []}
    for p in phones:
        s = st.get(norm_phone(p))
        res["allowed" if s == "opted_in" else "opted_out" if s == "opted_out" else "no_consent"].append(p)
    return res


# ── keyword recognition ───────────────────────────────────────────────────────────────────────────
_OPT_OUT_EXACT = {
    "stop", "stopall", "stop all", "unsubscribe", "unsub", "opt out", "optout", "opt-out", "cancel subscription",
    "stop messages", "stop messaging", "stop texting", "stop promotions", "stop promos", "stop marketing",
    "stop sending", "do not contact", "do not contact me", "dont contact me", "do not message me",
    "dont message me", "remove me", "remove my number", "no more messages", "no more promotions", "no more texts",
    "parar", "detener", "darme de baja", "baja", "arreter", "arrêter", "desabonner", "désabonner", "se desabonner",
    "pare", "cancelar inscricao", "abbestellen", "stopp", "przestan", "przestań", "wypisz", "wypisz mnie",
}
_STOP_TRAILERS = {"all", "messages", "message", "msgs", "promotions", "promos", "promotion", "texts", "texting",
                  "messaging", "sending", "please", "pls", "now", "these", "them", "it", "me", "marketing",
                  "notifications", "whatsapp", "contacting", "contact", "emails", "ads", "offers"}
_OPT_IN_EXACT = {"subscribe", "opt in", "optin", "opt-in", "subscribe me", "yes subscribe", "start promotions",
                 "send me offers", "send me promotions", "send me promos", "i want promotions", "i want offers"}
_RESTART_WORDS = {"start", "start messages", "restart", "resume", "yes start", "start again"}


def _norm_text(text: str) -> str:
    t = re.sub(r"[^\w\s'\-àâäçéèêëîïôöùûüÿñáíóúãõąćęłńśźżß]", " ", str(text or "").lower())
    return re.sub(r"\s+", " ", t).strip()


def is_opt_out_text(text: str) -> bool:
    t = _norm_text(text)
    if not t or len(t) > 60:
        return False
    if t in _OPT_OUT_EXACT:
        return True
    words = t.split()
    if len(words) <= 6 and "unsubscribe" in words:
        return True
    if words and words[0] == "stop" and len(words) <= 5 and all(w in _STOP_TRAILERS for w in words[1:]):
        return True
    return False


def is_opt_in_text(text: str) -> bool:
    return _norm_text(text) in _OPT_IN_EXACT


def is_restart_text(text: str) -> bool:
    return _norm_text(text) in _RESTART_WORDS


def handle_inbound(business_id: int, phone, text: str, business_name: str = "") -> str | None:
    """Called for a customer's inbound text BEFORE the AI. Returns the confirmation to send when the message
    was a consent keyword that was successfully recorded, else None (the AI handles the message normally).
    Does nothing while the migration is not applied."""
    if not text or not available():
        return None
    name = (business_name or "this business").strip()[:60]
    if is_opt_out_text(text):
        if record(business_id, phone, "opted_out", "customer_message", _norm_text(text)[:60], "customer"):
            return (f"You have been unsubscribed from promotional messages from {name}. "
                    f"You will still receive order, booking and payment updates. Reply START to opt back in.")
        return None
    if is_opt_in_text(text) or (is_restart_text(text) and statuses(business_id, [phone]).get(norm_phone(phone)) == "opted_out"):
        if record(business_id, phone, "opted_in", "customer_message", _norm_text(text)[:60], "customer"):
            return f"You are subscribed to promotions from {name}. Reply STOP at any time to unsubscribe."
    return None
