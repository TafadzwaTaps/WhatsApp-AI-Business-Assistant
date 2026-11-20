"""Stage 5a: structured outcome (code + short description + counts) for scheduled campaigns."""
import re

import pytest

from test_phase1c_job_idempotency import _DB, _campaign, _run   # noqa: F401  (shared fake database / runner)
from services.campaign_service import outcome_from_result as oc, FAILURE_DETAILS


def R(**kw):
    base = {"ok": True, "total": 10, "sent": 0, "failed": 0, "skipped": 0, "skipped_window": 0,
            "skipped_no_consent": 0, "skipped_opted_out": 0, "skipped_cap": 0, "first_error_reason": ""}
    base.update(kw)
    return base


def test_everyone_reached_is_delivered_with_no_failure_info():
    o = oc(R(sent=10))
    assert o["outcome"] == "delivered" and o["failure_code"] is None and o["failure_detail"] is None
    assert o["sent_count"] == 10 and o["total_recipients"] == 10


@pytest.mark.parametrize("extra,code", [
    ({"skipped_window": 10}, "window_closed"),
    ({"skipped_no_consent": 10}, "no_consent"),
    ({"skipped_opted_out": 10}, "opted_out"),
    ({"skipped_cap": 10}, "send_cap"),
    ({"failed": 10, "first_error_reason": "rate_limit"}, "rate_limited"),
    ({"failed": 10, "first_error_reason": "meta"}, "meta_error"),
    ({"failed": 10, "first_error_reason": "template"}, "template_error"),
    ({"failed": 10, "first_error_reason": "recipient"}, "recipient_invalid"),
    ({"failed": 10, "first_error_reason": "window"}, "window_closed"),
    ({"failed": 10, "first_error_reason": "internal"}, "internal_error"),
    ({"skipped": 10}, "no_eligible_recipients"),
])
def test_nobody_reached_is_failed_with_the_right_reason(extra, code):
    o = oc(R(**extra))
    assert o["outcome"] == "failed" and o["failure_code"] == code and o["failure_detail"] == FAILURE_DETAILS[code]
    assert o["sent_count"] == 0


def test_empty_audience_is_failed_no_recipients():
    o = oc(R(total=0))
    assert o["outcome"] == "failed" and o["failure_code"] == "no_recipients"


@pytest.mark.parametrize("extra", [{"failed": 2}, {"skipped_window": 3}, {"skipped_opted_out": 1}, {"skipped_no_consent": 4}])
def test_some_reached_and_some_not_is_partial_never_delivered(extra):
    o = oc(R(sent=5, **extra))
    assert o["outcome"] == "partial" and o["failure_code"] == "partial" and "Some recipients" in o["failure_detail"]
    assert o["sent_count"] == 5


def test_partial_names_the_main_reason():
    o = oc(R(sent=2, skipped_window=7, failed=1, first_error_reason="meta"))
    assert "24 hours" in o["failure_detail"]


def test_a_crash_or_a_malformed_result_is_an_internal_failure():
    for o in (oc(None, error=True), oc({"ok": False, "error": "x"}), oc("junk")):
        assert o["outcome"] == "failed" and o["failure_code"] == "internal_error" and o["sent_count"] == 0


def test_garbage_counts_never_crash():
    o = oc({"ok": True, "total": "x", "sent": None, "failed": -3, "skipped_window": "2"})
    assert o["outcome"] == "failed"


def test_details_contain_no_sensitive_material():
    for text in FAILURE_DETAILS.values():
        assert not re.search(r"\d{6,}|token|bearer|secret|password|EAA", text, re.I)
        assert len(text) < 200


# ── the scheduled runner ──────────────────────────────────────────────────────
def test_runner_stores_the_structured_outcome_and_a_zero_reach_run_is_failed(monkeypatch):
    db = _DB(scheduled_campaigns=[_campaign()])
    _run(monkeypatch, db, run_impl=lambda **kw: R(skipped_window=10))
    row = db.tables["scheduled_campaigns"][0]
    assert row["status"] == "failed" and row["outcome"] == "failed" and row["failure_code"] == "window_closed"
    assert row["failure_detail"] and row["skipped_count"] == 10 and row["sent_count"] == 0


def test_runner_tracks_partial_success_as_sent_with_partial_outcome(monkeypatch):
    db = _DB(scheduled_campaigns=[_campaign()])
    _, out = _run(monkeypatch, db, run_impl=lambda **kw: R(sent=6, skipped_no_consent=4))
    row = db.tables["scheduled_campaigns"][0]
    assert row["status"] == "sent" and row["outcome"] == "partial" and row["sent_count"] == 6 and row["skipped_count"] == 4
    assert out["results"][0]["outcome"] == "partial"


def test_runner_full_success(monkeypatch):
    db = _DB(scheduled_campaigns=[_campaign()])
    _run(monkeypatch, db, run_impl=lambda **kw: R(sent=10))
    row = db.tables["scheduled_campaigns"][0]
    assert row["status"] == "sent" and row["outcome"] == "delivered" and row["failure_code"] is None


def test_runner_crash_is_failed_with_an_internal_code_and_no_exception_text(monkeypatch):
    db = _DB(scheduled_campaigns=[_campaign()])

    def boom(**kw):
        raise RuntimeError("secret-token-abc123 database exploded")
    _run(monkeypatch, db, run_impl=boom)
    row = db.tables["scheduled_campaigns"][0]
    assert row["status"] == "failed" and row["failure_code"] == "internal_error"
    assert "abc123" not in str(row)


def test_runner_still_works_when_the_migration_is_not_applied(monkeypatch):
    db = _DB(scheduled_campaigns=[_campaign()])

    def no_new_columns(q):
        if q.op == "update" and q.payload and "outcome" in q.payload:
            raise Exception({"code": "42703", "message": 'column "outcome" of relation "scheduled_campaigns" does not exist'})
    db.hooks[("scheduled_campaigns", "update")] = no_new_columns
    _run(monkeypatch, db, run_impl=lambda **kw: R(sent=3))
    row = db.tables["scheduled_campaigns"][0]
    assert row["status"] == "sent" and row["sent_at"] and "outcome" not in row


def test_the_migration_is_additive_only():
    import os
    sql = open(os.path.join(os.path.dirname(__file__), "..", "migrations", "scheduled_campaign_failure_info.sql"), encoding="utf8").read()
    code = "\n".join(l for l in sql.splitlines() if not l.strip().startswith("--"))
    assert code.count("ADD COLUMN IF NOT EXISTS") == 7
    assert not re.search(r"\bDROP\b|\bDELETE\b|\bTRUNCATE\b|NOT NULL|\bALTER COLUMN\b|\bUPDATE\b", code, re.I)
