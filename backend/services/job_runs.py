"""
services/job_runs.py: a small run log for background jobs (Phase 7, stage 2).

One row per run: which job, when, how long, ok / partial / error, and the job's own COUNTS (numbers only).
Purpose: let the owner see that reports, reminders, campaigns and trial emails actually ran, and when one stopped.

Safety rules
  * Observability only. Recording never raises, never blocks a job, and never changes what a job does.
  * Only integer counts and a short error TYPE name are stored: no messages, phone numbers, emails, tokens.
  * Until backend/migrations/job_runs.sql is applied the recorder is a silent no-op (re-probed every 60 s).
  * Old rows are pruned (RETENTION_DAYS), at most once an hour per process.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

log = logging.getLogger("wazibot")

RETENTION_DAYS = 60
_REPROBE_SECONDS = 60
_PRUNE_EVERY = 3600
_COUNT_MAX = 10_000_000

# How long without a successful run before the admin view flags a job as stale (None = no expectation).
EXPECTED_EVERY_HOURS = {
    "booking_reminders": 2,
    "monthly_report": 24 * 33,
    "trial_warnings": 36,
    "scheduled_campaigns": 2,
    "account_purge": 36,
}

_ready = None
_ready_at = 0.0
_last_prune = 0.0


def _now():
    return datetime.now(timezone.utc)


def _available() -> bool:
    global _ready, _ready_at
    t = time.monotonic()
    if _ready is True:
        return True
    if _ready is False and (t - _ready_at) < _REPROBE_SECONDS:
        return False
    try:
        from core.db import supabase
        supabase.table("job_runs").select("id").limit(1).execute()
        _ready = True
    except Exception as exc:
        log.info("job_runs: not active (migration not applied?): %s", type(exc).__name__)
        _ready = False
    _ready_at = t
    return _ready


def _reset_for_tests() -> None:
    global _ready, _ready_at, _last_prune
    _ready, _ready_at, _last_prune = None, 0.0, 0.0


def counts_from(result) -> dict:
    """Only the top-level integer values of a job's result dict (bools and nested data are dropped)."""
    out = {}
    if isinstance(result, dict):
        for k, v in result.items():
            if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= _COUNT_MAX and len(out) < 20:
                out[str(k)[:40]] = v
    return out


def status_from(result, error: bool = False) -> str:
    """ok | partial | error. error: the job raised or reported ok=False; partial: it reported failures/errors."""
    if error or (isinstance(result, dict) and result.get("ok") is False):
        return "error"
    if isinstance(result, dict):
        for k in ("failed", "errors"):
            v = result.get(k)
            if isinstance(v, int) and not isinstance(v, bool) and v > 0:
                return "partial"
    return "ok"


def record(job: str, *, status: str, started_at: datetime, finished_at: datetime | None = None,
           summary: dict | None = None, error_code: str | None = None) -> bool:
    """Store one run. Returns False (and does nothing else) when disabled or on any failure. Never raises."""
    try:
        if status not in ("ok", "partial", "error") or not _available():
            return False
        finished_at = finished_at or _now()
        from core.db import supabase
        supabase.table("job_runs").insert({
            "job": str(job)[:60], "status": status,
            "started_at": started_at.isoformat(), "finished_at": finished_at.isoformat(),
            "duration_ms": max(int((finished_at - started_at).total_seconds() * 1000), 0),
            "summary": summary or {}, "error_code": (error_code or None) and str(error_code)[:60],
        }).execute()
        _maybe_prune()
        return True
    except Exception as exc:
        log.debug("job_runs.record skipped: %s", type(exc).__name__)
        return False


def record_result(job: str, started_at: datetime, result, error_code: str | None = None) -> bool:
    """Record a finished job from its result dict (or an exception type name)."""
    return record(job, status=status_from(result, error=bool(error_code)), started_at=started_at,
                  summary=counts_from(result), error_code=error_code)


def run_tracked(job: str, fn, *args, **kwargs):
    """Run fn(*args, **kwargs), record the run, and return fn's result unchanged. An exception is recorded
    (type name only) and re-raised exactly as before."""
    started = _now()
    try:
        result = fn(*args, **kwargs)
    except BaseException as exc:                        # noqa: BLE001  (re-raised below)
        record(job, status="error", started_at=started, error_code=type(exc).__name__)
        raise
    record_result(job, started, result)
    return result


def _maybe_prune() -> None:
    global _last_prune
    t = time.monotonic()
    if t - _last_prune < _PRUNE_EVERY:
        return
    _last_prune = t
    try:
        from core.db import supabase
        cutoff = (_now() - timedelta(days=RETENTION_DAYS)).isoformat()
        supabase.table("job_runs").delete().lt("started_at", cutoff).execute()
    except Exception as exc:
        log.debug("job_runs prune skipped: %s", type(exc).__name__)


def overview(limit: int = 100) -> dict:
    """{'available': bool, 'runs': [newest first], 'jobs': {job: last_run, last_ok, last_error, stale}}."""
    if not _available():
        return {"available": False, "runs": [], "jobs": {}}
    try:
        from core.db import supabase
        limit = max(1, min(int(limit), 500))
        rows = (supabase.table("job_runs").select("*").order("started_at", desc=True).limit(limit).execute().data) or []
    except Exception as exc:
        log.warning("job_runs.overview failed: %s", type(exc).__name__)
        return {"available": True, "runs": [], "jobs": {}, "error": "could not read job runs"}
    jobs: dict = {}
    for r in rows:                                       # newest first: the first of each kind wins
        j = jobs.setdefault(r.get("job"), {"last_run": None, "last_ok": None, "last_error": None})
        if j["last_run"] is None:
            j["last_run"] = r.get("started_at")
        if j["last_ok"] is None and r.get("status") in ("ok", "partial"):
            j["last_ok"] = r.get("started_at")
        if j["last_error"] is None and r.get("status") == "error":
            j["last_error"] = {"at": r.get("started_at"), "code": r.get("error_code")}
    now = _now()
    for name, j in jobs.items():
        hours = EXPECTED_EVERY_HOURS.get(name)
        last_ok = _parse(j["last_ok"])
        j["stale"] = bool(hours) and (last_ok is None or now - last_ok > timedelta(hours=hours))
    return {"available": True, "runs": rows, "jobs": jobs}


def _parse(v):
    try:
        s = str(v).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None
