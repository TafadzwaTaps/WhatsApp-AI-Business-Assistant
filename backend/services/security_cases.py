"""services/security_cases.py — incidents and the vulnerability register (security audit Stage 4c).

Uses the tables from the Security Operations Center database change (part 1). The database refuses
to delete incidents, notes or findings, and refuses to close them without evidence; the same rules
are checked here first so the admin gets a clear message. Every change is written to the admin
audit trail. Unlike event recording, a failed write here is reported to the caller.
"""
from __future__ import annotations

import datetime as _dt
import logging
from typing import Optional

from crud.admin_audit import log_admin_action

log = logging.getLogger(__name__)

SEVERITIES = ("info", "low", "medium", "high", "critical")
CLASSIFICATIONS = ("suspicious", "suspected_incident", "confirmed_incident", "confirmed_breach")
STATUSES = ("detected", "under_investigation", "contained", "remediation_in_progress",
            "awaiting_verification", "resolved", "false_positive")
CLOSED = ("resolved", "false_positive")
# Resolving is only possible after "awaiting verification". Closed incidents can be reopened.
TRANSITIONS = {
    "detected": ("under_investigation", "contained", "false_positive"),
    "under_investigation": ("contained", "remediation_in_progress", "awaiting_verification", "false_positive"),
    "contained": ("under_investigation", "remediation_in_progress", "awaiting_verification"),
    "remediation_in_progress": ("under_investigation", "contained", "awaiting_verification"),
    "awaiting_verification": ("under_investigation", "remediation_in_progress", "resolved"),
    "resolved": ("under_investigation",),
    "false_positive": ("under_investigation",),
}
VULN_SOURCES = ("dependency_audit", "static_analysis", "secret_scan", "config_check", "staging_scan",
                "manual_review")
VULN_STATUSES = ("open", "acknowledged", "investigating", "mitigated", "resolved", "accepted_risk",
                 "false_positive")
NEEDS_EVIDENCE = ("mitigated", "resolved", "accepted_risk")


class CaseError(Exception):
    """A refusal with the HTTP status and a message that is safe to show."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _table(name: str):
    from core.db import supabase
    return supabase.table(name)


def _run(query, doing: str) -> list:
    try:
        return query.execute().data or []
    except Exception as exc:
        code = str(getattr(exc, "code", ""))
        log.warning("security_cases: %s failed (%s %s)", doing, type(exc).__name__, code)
        if code in ("42P01", "42703", "PGRST204", "PGRST205"):
            raise CaseError(503, "Not available: the security database change has not been applied.")
        if code == "23505":
            raise CaseError(409, "An entry with this reference already exists.")
        if code == "23514":
            raise CaseError(422, "The database refused these values.")
        raise CaseError(503, f"Could not {doing}. Nothing may have changed; check before retrying.")


def _one(table: str, row_id: int, what: str) -> dict:
    rows = _run(_table(table).select("*").eq("id", row_id).limit(1), f"read the {what}")
    if not rows:
        raise CaseError(404, f"No such {what}.")
    return rows[0]


def _page(table: str, filters: dict, order: str, page: int, page_size: int, what: str) -> tuple:
    query = _table(table).select("*")
    for column, value in filters.items():
        if value:
            query = query.eq(column, value)
    start = (page - 1) * page_size
    rows = _run(query.order(order, desc=True).order("id", desc=True).range(start, start + page_size),
                f"read the {what}")
    return rows[:page_size], len(rows) > page_size


def _audit(admin: str, action: str, kind: str, row_id: int, *, reason: Optional[str] = None,
           details: Optional[dict] = None, ip: Optional[str] = None) -> None:
    log_admin_action(actor_username=admin, action=action, target_type=kind, target_id=row_id,
                     reason=reason, metadata=details or {}, ip_address=ip)


# ── Incidents ────────────────────────────────────────────────────────────────

def list_incidents(status: Optional[str], severity: Optional[str], page: int, page_size: int) -> tuple:
    return _page("security_incidents", {"status": status, "severity": severity}, "detected_at",
                 page, page_size, "incidents")


def _add_note(incident_id: int, kind: str, body: str, author: str) -> None:
    _run(_table("security_incident_notes").insert(
        {"incident_id": incident_id, "kind": kind, "body": body, "author": author}), "save the note")


def _try_note(incident_id: int, kind: str, body: str, author: str) -> bool:
    """For the timeline entry that follows a change already saved: report failure, do not undo."""
    try:
        _add_note(incident_id, kind, body, author)
        return True
    except CaseError:
        return False


def get_incident(incident_id: int) -> dict:
    from services.security_center import describe
    incident = _one("security_incidents", incident_id, "incident")
    notes = _run(_table("security_incident_notes").select("*").eq("incident_id", incident_id)
                 .order("created_at", desc=False).limit(500), "read the notes")
    events = _run(_table("security_events").select("*").eq("incident_id", incident_id)
                  .order("created_at", desc=True).limit(201), "read the linked events")
    return {"incident": incident, "notes": notes, "events": [describe(e) for e in events[:200]],
            "more_events": len(events) > 200,
            "allowed_statuses": list(TRANSITIONS.get(incident.get("status"), ()))}


def create_incident(admin: str, fields: dict, event_ids: list, ip: Optional[str] = None) -> dict:
    now = _now()
    row = {**fields, "status": "detected", "created_by": admin, "detected_at": now,
           "created_at": now, "updated_at": now}
    saved = _run(_table("security_incidents").insert(row), "create the incident")
    if not saved:
        raise CaseError(503, "Could not create the incident.")
    incident = saved[0]
    _audit(admin, "incident.create", "security_incident", incident["id"], ip=ip,
           details={"severity": fields.get("severity"), "classification": fields.get("classification")})
    warnings = []
    if not _try_note(incident["id"], "status_change", "Incident created.", admin):
        warnings.append("The first timeline entry was not saved.")
    linked = 0
    if event_ids:
        try:
            linked = link_events(admin, incident["id"], event_ids, ip=ip)["linked"]
            if linked < len(set(event_ids)):
                warnings.append("Some events were not linked: they are gone or belong to another incident.")
        except CaseError as exc:
            warnings.append(f"Events were not linked. {exc.message}")
    return {"incident": incident, "linked": linked, "warnings": warnings}


def update_incident(admin: str, incident_id: int, changes: dict, reason: str, ip: Optional[str] = None) -> dict:
    current = _one("security_incidents", incident_id, "incident")
    changed = {k: v for k, v in changes.items() if current.get(k) != v}
    if not changed:
        raise CaseError(422, "Nothing to change.")
    saved = _run(_table("security_incidents").update({**changed, "updated_at": _now()})
                 .eq("id", incident_id).eq("updated_at", current.get("updated_at")), "update the incident")
    if not saved:
        raise CaseError(409, "The incident changed while you were editing. Reload it and try again.")
    before = {k: current.get(k) for k in changed}
    _audit(admin, "incident.update", "security_incident", incident_id, reason=reason, ip=ip,
           details={"before": before, "after": changed})
    summary = "; ".join(f"{k.replace('_', ' ')} changed" if k in ("summary", "notification_assessment")
                        else f"{k.replace('_', ' ')}: {before[k] or 'none'} → {changed[k] or 'none'}"
                        for k in changed)
    noted = _try_note(incident_id, "status_change", f"Updated ({summary}). Reason: {reason}", admin)
    return {"incident": saved[0], "warnings": [] if noted else ["The timeline entry was not saved."]}


def change_status(admin: str, incident_id: int, status: str, note: str,
                  evidence: Optional[str] = None, ip: Optional[str] = None) -> dict:
    current = _one("security_incidents", incident_id, "incident")
    old = current.get("status")
    if status not in TRANSITIONS.get(old, ()):
        allowed = ", ".join(s.replace("_", " ") for s in TRANSITIONS.get(old, ())) or "none"
        raise CaseError(409, f"Cannot move from {str(old).replace('_', ' ')} to {status.replace('_', ' ')}. "
                             f"Allowed: {allowed}.")
    if status == "resolved" and not evidence:
        raise CaseError(422, "Resolving needs verification evidence: how was the fix confirmed?")
    update = {"status": status, "updated_at": _now(),
              "resolved_at": _now() if status == "resolved" else None}
    if status == "resolved":
        update["verification_evidence"] = evidence
    # Only if nobody else changed the status meanwhile.
    saved = _run(_table("security_incidents").update(update).eq("id", incident_id).eq("status", old),
                 "change the status")
    if not saved:
        raise CaseError(409, "The status changed while you were editing. Reload the incident.")
    _audit(admin, "incident.status", "security_incident", incident_id, reason=note, ip=ip,
           details={"from": old, "to": status,
                    **({"previous_evidence": current.get("verification_evidence")}
                       if current.get("verification_evidence") else {}),
                    **({"evidence": evidence} if status == "resolved" else {})})
    body = f"Status: {old.replace('_', ' ')} → {status.replace('_', ' ')}. {note}"
    if status == "resolved":
        body += f"\nVerification evidence: {evidence}"
    noted = _try_note(incident_id, "status_change", body, admin)
    return {"incident": saved[0], "warnings": [] if noted else ["The timeline entry was not saved."]}


def add_note(admin: str, incident_id: int, kind: str, body: str, ip: Optional[str] = None) -> dict:
    _one("security_incidents", incident_id, "incident")
    _add_note(incident_id, kind, body, admin)
    _audit(admin, "incident.note", "security_incident", incident_id, ip=ip, details={"kind": kind})
    return {"ok": True}


def link_events(admin: str, incident_id: int, event_ids: list, ip: Optional[str] = None) -> dict:
    """Link events that belong to no incident yet. Linked events are kept by retention."""
    _one("security_incidents", incident_id, "incident")
    ids = sorted(set(event_ids))
    rows = _run(_table("security_events").update({"incident_id": incident_id})
                .in_("id", ids).is_("incident_id", "null"), "link the events")
    done = [r.get("id") for r in rows]
    if done:
        _audit(admin, "incident.link_events", "security_incident", incident_id, ip=ip,
               details={"event_ids": done})
        _try_note(incident_id, "status_change", f"Linked {len(done)} event(s): {', '.join(map(str, done))}.", admin)
    return {"linked": len(done), "requested": len(ids)}


def unlink_event(admin: str, incident_id: int, event_id: int, reason: str, ip: Optional[str] = None) -> dict:
    rows = _run(_table("security_events").update({"incident_id": None})
                .eq("id", event_id).eq("incident_id", incident_id), "unlink the event")
    if not rows:
        raise CaseError(404, "That event is not linked to this incident.")
    _audit(admin, "incident.unlink_events", "security_incident", incident_id, reason=reason, ip=ip,
           details={"event_ids": [event_id]})
    noted = _try_note(incident_id, "status_change", f"Unlinked event {event_id}. Reason: {reason}", admin)
    return {"ok": True, "warnings": [] if noted else ["The timeline entry was not saved."]}


# ── Vulnerabilities ──────────────────────────────────────────────────────────

def list_vulnerabilities(status: Optional[str], severity: Optional[str], page: int, page_size: int) -> tuple:
    return _page("security_vulnerabilities", {"status": status, "severity": severity}, "detected_at",
                 page, page_size, "vulnerabilities")


def create_vulnerability(admin: str, fields: dict, ip: Optional[str] = None) -> dict:
    now = _now()
    row = {**fields, "status": "open", "detected_at": now, "created_at": now, "updated_at": now}
    saved = _run(_table("security_vulnerabilities").insert(row), "save the finding")
    if not saved:
        raise CaseError(503, "Could not save the finding.")
    _audit(admin, "vulnerability.create", "security_vulnerability", saved[0]["id"], ip=ip,
           details={"title": fields.get("title"), "severity": fields.get("severity"),
                    "source": fields.get("source")})
    return {"vulnerability": saved[0]}


def update_vulnerability(admin: str, vulnerability_id: int, changes: dict, reason: str,
                         ip: Optional[str] = None) -> dict:
    current = _one("security_vulnerabilities", vulnerability_id, "finding")
    status = changes.get("status")
    changed = {k: v for k, v in changes.items() if current.get(k) != v}
    if not changed:
        raise CaseError(422, "Nothing to change.")
    if "status" in changed and status in NEEDS_EVIDENCE:
        # Evidence must come with this change: an old note must not close a reopened finding.
        if not changes.get("evidence"):
            raise CaseError(422, f"Marking this {status.replace('_', ' ')} needs evidence.")
        changed["evidence"] = changes["evidence"]
        if status != "accepted_risk":
            changed["last_verified_at"] = _now()
    final_status = changed.get("status", current.get("status"))
    if final_status in NEEDS_EVIDENCE and "evidence" in changed and not changed["evidence"]:
        raise CaseError(422, "Evidence cannot be removed while the finding is closed.")
    saved = _run(_table("security_vulnerabilities").update({**changed, "updated_at": _now()})
                 .eq("id", vulnerability_id).eq("updated_at", current.get("updated_at")), "update the finding")
    if not saved:
        raise CaseError(409, "The finding changed while you were editing. Reload it and try again.")
    _audit(admin, "vulnerability.update", "security_vulnerability", vulnerability_id, reason=reason, ip=ip,
           details={"before": {k: current.get(k) for k in changed}, "after": changed})
    return {"vulnerability": saved[0]}
