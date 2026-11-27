"""services/security_events.py — the one place the application reports security events (Stage 4a).

emit() only touches memory, so it never slows down or fails the request that reports something.
A background thread does the writing: a new event within about a second, repeats of the same
event (same type, source, account, business, route and rule inside a 5 minute window) as a count,
at most once per FLUSH_SECONDS. Writes are capped per minute; what exceeds the cap waits as a
count. Anything not yet written is lost if the process is killed.

The same thread removes old events once a day (SECURITY_EVENT_RETENTION_DAYS, default 180, 0
disables). Removal happens in the database function from migration part 1, so nothing is deleted
until that migration has been applied.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from collections import deque
from typing import Optional

from core.safelog import record_dropped, redact

log = logging.getLogger(__name__)

DEDUP_WINDOW_SECONDS = 300
FLUSH_SECONDS = 15
MAX_PENDING_EVENTS = 2000
MAX_WRITES_PER_MINUTE = 300
_MAX_METADATA_CHARS = 2000
_PRUNE_FIRST_AFTER = 600
_PRUNE_EVERY = 86400
_URGENT = ("high", "critical")

_lock = threading.Lock()
_pending: dict = {}                 # dedup key -> {"fields", "count", "due", "bucket"}
_write_times: deque = deque()
_wake = threading.Event()
_thread: Optional[threading.Thread] = None
_next_prune = 0.0


def _clip(value, length: int):
    return None if value is None else str(value).replace("\x00", "")[:length]


def _bounded_metadata(metadata) -> dict:
    clean = redact(metadata or {})
    if len(json.dumps(clean, default=str)) > _MAX_METADATA_CHARS:
        return {"truncated": True}
    return clean


def _route_of(request) -> Optional[str]:
    """The route template ("/chat/conversations/{phone}"), so ids and phone numbers are not stored."""
    route = getattr(request, "scope", {}).get("route")
    return getattr(route, "path", None) or request.url.path


def emit(event_type: str, *, request=None, ip: Optional[str] = None, username: Optional[str] = None,
         business_id: Optional[int] = None, endpoint: Optional[str] = None, rule: Optional[str] = None,
         severity: Optional[str] = None, outcome: Optional[str] = None,
         user_agent: Optional[str] = None, metadata: Optional[dict] = None) -> None:
    """Queue one security event. Never raises and never waits on the database."""
    try:
        if request is not None:
            from services.security import get_client_ip
            ip = ip or get_client_ip(request)
            endpoint = endpoint or _route_of(request)
            user_agent = user_agent or request.headers.get("user-agent")
        fields = {
            "event_type": _clip(event_type, 80), "ip": _clip(ip, 64), "username": _clip(username, 120),
            "business_id": business_id if isinstance(business_id, int) else None,
            "endpoint": _clip(endpoint, 200), "rule": _clip(rule, 120), "severity": severity,
            "outcome": outcome, "user_agent": _clip(user_agent, 300),
            "metadata": _bounded_metadata(metadata),
        }
        bucket = int(time.time() // DEDUP_WINDOW_SECONDS)
        identity = [fields[k] for k in ("event_type", "ip", "username", "business_id", "endpoint",
                                        "rule", "severity", "outcome")] + [bucket]
        key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        with _lock:
            entry = _pending.get(key)
            is_new = entry is None
            if is_new:
                if len(_pending) >= MAX_PENDING_EVENTS:
                    # Entries already written only exist to batch repeats: dropping them loses nothing.
                    for old in [k for k, e in _pending.items() if not e["count"]]:
                        del _pending[old]
                if len(_pending) < MAX_PENDING_EVENTS:
                    entry = _pending[key] = {"count": 0, "due": 0.0, "bucket": bucket}
            if entry is not None:
                entry["count"] += 1
                entry["fields"] = fields
        if entry is None:
            record_dropped("security_events.queue", fields["event_type"], OverflowError("queue full"))
            return
        _start_worker()
        if is_new:
            _wake.set()
    except Exception as exc:
        record_dropped("security_events.emit", str(event_type), exc)


def flush(*, force: bool = False, limit: Optional[int] = None) -> int:
    """Write the events that are due (all pending ones when forced), urgent ones first.
    Returns the number of writes attempted."""
    from crud.security_events import EVENT_RULES, log_security_event

    def routine(item) -> bool:
        fields = item[1]["fields"]
        severity = fields["severity"] or EVENT_RULES.get(fields["event_type"], ("info", None))[0]
        return severity not in _URGENT

    now = time.monotonic()
    bucket = int(time.time() // DEDUP_WINDOW_SECONDS)
    batch = []
    with _lock:
        while _write_times and now - _write_times[0] >= 60:
            _write_times.popleft()
        for key, entry in sorted(_pending.items(), key=routine):
            if entry["count"] and (force or entry["due"] <= now):
                if (limit is not None and len(batch) >= limit) or \
                        (not force and len(_write_times) >= MAX_WRITES_PER_MINUTE):
                    continue
                batch.append((key, entry["fields"], entry["count"]))
                entry["count"], entry["due"] = 0, now + FLUSH_SECONDS
                _write_times.append(now)
            elif not entry["count"] and entry["bucket"] < bucket:
                del _pending[key]
    for key, fields, count in batch:
        log_security_event(dedup_key=key, occurrences=count, **fields)
    return len(batch)


def retention_days() -> int:
    try:
        return int(os.getenv("SECURITY_EVENT_RETENTION_DAYS", "180"))
    except ValueError:
        return 180


def _prune_if_due() -> None:
    global _next_prune
    now = time.monotonic()
    if not _next_prune:
        _next_prune = now + _PRUNE_FIRST_AFTER
        return
    if now < _next_prune:
        return
    _next_prune = now + _PRUNE_EVERY
    days = retention_days()
    if days > 0:
        from crud.security_events import prune_security_events
        removed = prune_security_events(days)
        if removed:
            log.info("security_events: removed %s events older than %s days", removed, max(days, 30))


def _run() -> None:
    while True:
        _wake.wait(1.0)
        _wake.clear()
        try:
            flush()
            _prune_if_due()
        except Exception as exc:                # the writer must survive anything
            record_dropped("security_events.worker", "flush", exc)


def _start_worker() -> None:
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    with _lock:
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_run, name="security-events", daemon=True)
            _thread.start()


def attach_security_event_worker(app) -> None:
    """Start the writer with the app (so retention runs on a quiet day) and write what is left on exit."""
    @app.on_event("startup")
    def _startup() -> None:
        _start_worker()

    @app.on_event("shutdown")
    def _shutdown() -> None:
        try:
            flush(force=True, limit=100)
        except Exception as exc:
            record_dropped("security_events.shutdown", "flush", exc)


def _reset_for_tests() -> None:
    global _next_prune
    with _lock:
        _pending.clear()
        _write_times.clear()
    _next_prune = 0.0
