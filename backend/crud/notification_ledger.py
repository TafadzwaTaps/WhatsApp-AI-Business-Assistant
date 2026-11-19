"""
crud/notification_ledger.py — "send this notification at most once" bookkeeping.

claim(business_id, kind, period_key) -> True only for the FIRST caller for that
(business, kind, period); everyone else (a retried cron call, an overlapping run, a run
after a restart) gets False and must not send. release() undoes a claim when the send
did not actually happen, so a failed send is retried on the next run instead of being lost.

Storage, in order of preference:
  1. table `notification_log` with UNIQUE (business_id, kind, period_key)
     (see backend/migrations/notification_ledger.sql - proposed, applied by the owner).
     Atomic across processes.
  2. Until that table exists: a marker inside businesses.features_json["_notif_sent"],
     claimed under a process-wide lock. Correct for the single-worker deployment WaziBot
     runs today; NOT safe across several workers (see docs/ai-development/SCALING_NOTES.md).
     A dashboard "save settings" that rewrites features_json from a stale copy could erase a
     marker, which would at worst repeat one email.

Any other database failure raises: the caller must skip the send (fail closed) rather than
risk a duplicate.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from core.db import supabase

log = logging.getLogger(__name__)

_LOCK = threading.Lock()
_table_ok: bool | None = None          # None = unknown, True/False after the first probe
_MAX_MARKERS = 40                      # keep the fallback marker small


def _is_missing_table(exc: Exception) -> bool:
    s = str(exc).lower()
    code = getattr(exc, "code", None)
    if code is None and getattr(exc, "args", None) and isinstance(exc.args[0], dict):
        code = exc.args[0].get("code")
    if code is not None and str(code) != "":
        return str(code) in ("42P01", "PGRST205")      # a structured code decides on its own
    return ("notification_log" in s
            and ("does not exist" in s or "could not find the table" in s or "schema cache" in s))


def _is_duplicate(exc: Exception) -> bool:
    s = str(exc).lower()
    code = getattr(exc, "code", None)
    if code is None and getattr(exc, "args", None) and isinstance(exc.args[0], dict):
        code = exc.args[0].get("code")
    return str(code) == "23505" or "23505" in s or "duplicate key" in s


def _marker(kind: str, period_key: str) -> str:
    return f"{kind}:{period_key}"


# ── claim ─────────────────────────────────────────────────────────────────────

def claim(business_id: int, kind: str, period_key: str) -> bool:
    """True if THIS call won the right to send; False if it was already claimed/sent."""
    global _table_ok
    period_key = str(period_key)
    if _table_ok is not False:
        try:
            supabase.table("notification_log").insert({
                "business_id": business_id, "kind": kind, "period_key": period_key,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }).execute()
            _table_ok = True
            return True
        except Exception as exc:
            if _is_duplicate(exc):
                _table_ok = True
                return False
            if _is_missing_table(exc):
                log.warning("notification_log table not found - using the features_json fallback "
                            "(apply backend/migrations/notification_ledger.sql for cross-process safety)")
                _table_ok = False
            else:
                raise
    return _claim_fallback(business_id, kind, period_key)


def _claim_fallback(business_id: int, kind: str, period_key: str) -> bool:
    key = _marker(kind, period_key)
    with _LOCK:
        res = (supabase.table("businesses").select("features_json")
               .eq("id", business_id).limit(1).execute())
        if not res.data:
            raise RuntimeError("business not found")
        fj = res.data[0].get("features_json")
        fj = dict(fj) if isinstance(fj, dict) else {}
        sent = dict(fj.get("_notif_sent") or {})
        if key in sent:
            return False
        sent[key] = datetime.now(timezone.utc).isoformat()
        if len(sent) > _MAX_MARKERS:                      # drop the oldest markers
            for old in sorted(sent, key=lambda k: sent[k])[: len(sent) - _MAX_MARKERS]:
                sent.pop(old, None)
        fj["_notif_sent"] = sent
        supabase.table("businesses").update({"features_json": fj}).eq("id", business_id).execute()
        return True


# ── release ───────────────────────────────────────────────────────────────────

def release(business_id: int, kind: str, period_key: str) -> None:
    """Undo a claim because the notification was NOT delivered, so the next run retries it.
    Never raises."""
    period_key = str(period_key)
    try:
        if _table_ok is not False:
            supabase.table("notification_log").delete() \
                .eq("business_id", business_id).eq("kind", kind).eq("period_key", period_key).execute()
            return
    except Exception as exc:
        if not _is_missing_table(exc):
            log.warning("notification_ledger.release failed (table): %s", type(exc).__name__)
            return
    key = _marker(kind, period_key)
    try:
        with _LOCK:
            res = (supabase.table("businesses").select("features_json")
                   .eq("id", business_id).limit(1).execute())
            fj = (res.data[0].get("features_json") if res.data else None)
            if isinstance(fj, dict) and key in (fj.get("_notif_sent") or {}):
                fj = dict(fj)
                sent = dict(fj["_notif_sent"])
                sent.pop(key, None)
                fj["_notif_sent"] = sent
                supabase.table("businesses").update({"features_json": fj}).eq("id", business_id).execute()
    except Exception as exc:
        log.warning("notification_ledger.release failed (fallback): %s", type(exc).__name__)


def _reset_probe_for_tests() -> None:
    global _table_ok
    _table_ok = None
