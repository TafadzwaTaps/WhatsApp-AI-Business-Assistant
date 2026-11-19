"""QA follow-ups for test_phase1c_job_idempotency.py: real error shapes, fallback-mode crashes,
sweep scope, trial-email formats and skips, immediate-campaign guard edge cases."""
import asyncio
import types
from datetime import timedelta

import pytest

import crud.notification_ledger as ledger
import routes.growth_routes as growth

from test_phase1c_job_idempotency import (   # noqa: F401  (fixtures + helpers)
    _DB, _campaign, _run, _now, _biz, _ledger_db, _warnings, _send_campaign, _call, _body,
    _ProcessDied, mail, _reset_probe,
)


class _APIError(Exception):
    """Same shape postgrest's APIError has: a dict in args[0]."""
    def __init__(self, code, message):
        super().__init__({"code": code, "message": message})


def test_dict_shaped_duplicate_error_means_already_claimed(monkeypatch):
    db = _ledger_db([_biz(1)])
    monkeypatch.setattr(ledger, "supabase", db)

    def dup(q):
        raise _APIError("23505", "duplicate key value violates unique constraint")
    db.hooks[("notification_log", "insert")] = dup
    assert ledger.claim(1, "trial_warn_7", "p") is False


@pytest.mark.parametrize("code", ["PGRST205", "42P01"])
def test_dict_shaped_missing_table_error_switches_to_the_fallback_marker(monkeypatch, code):
    ledger._reset_probe_for_tests()
    db = _ledger_db([_biz(1)])
    monkeypatch.setattr(ledger, "supabase", db)

    def missing(q):
        raise _APIError(code, "Could not find the table 'public.notification_log' in the schema cache")
    db.hooks[("notification_log", "insert")] = missing
    assert ledger.claim(1, "trial_warn_7", "p") is True
    assert ledger.claim(1, "trial_warn_7", "p") is False
    assert "trial_warn_7:p" in db.tables["businesses"][0]["features_json"]["_notif_sent"]


def test_a_check_constraint_error_on_status_uses_the_sent_at_fallback(monkeypatch):
    db = _DB(scheduled_campaigns=[_campaign()])

    def check_violation(q):
        if q.op == "update" and q.payload.get("status") == "processing":
            raise _APIError("23514", 'new row violates check constraint "scheduled_campaigns_status_check"')
    db.hooks[("scheduled_campaigns", "update")] = check_violation
    sent, out = _run(monkeypatch, db)
    assert sent == [1] and out["processed"] == 1


def test_crash_in_fallback_claim_mode_is_never_resent_and_is_eventually_flagged_failed(monkeypatch):
    db = _DB(scheduled_campaigns=[_campaign()])

    def reject_status(q):
        if q.op == "update" and q.payload.get("status") == "processing":
            raise _APIError("23514", "check constraint")
    db.hooks[("scheduled_campaigns", "update")] = reject_status

    def dies(**kw):
        raise _ProcessDied()
    with pytest.raises(_ProcessDied):
        _run(monkeypatch, db, run_impl=dies)
    row = db.tables["scheduled_campaigns"][0]
    assert row["status"] == "pending" and row["sent_at"]                    # claimed through sent_at
    sent2, _ = _run(monkeypatch, db)
    assert sent2 == [] and row["status"] == "pending"                       # fresh claim: not resent
    row["sent_at"] = (_now() - timedelta(minutes=growth._STALLED_CLAIM_MINUTES + 10)).isoformat()
    sent3, out3 = _run(monkeypatch, db)
    assert sent3 == [] and out3["interrupted_marked_failed"] == 1 and row["status"] == "failed"


def test_sweep_only_touches_stale_claims():
    old = (_now() - timedelta(minutes=growth._STALLED_CLAIM_MINUTES + 10)).isoformat()
    fresh = _now().isoformat()
    db = _DB(scheduled_campaigns=[
        _campaign(1, status="sent", sent_at=old),            # finished long ago: leave
        _campaign(2, status="cancelled", sent_at=old),       # cancelled: leave
        _campaign(3, status="pending", sent_at=None),        # not yet claimed: leave
        _campaign(4, status="processing", sent_at=fresh),    # fresh claim: leave
        _campaign(5, status="processing", sent_at=old),      # stale: fail
        _campaign(6, status="pending", sent_at=old),         # stale fallback claim: fail
    ])
    assert growth._fail_stalled_campaigns(db) == 2
    got = {r["id"]: r["status"] for r in db.tables["scheduled_campaigns"]}
    assert got == {1: "sent", 2: "cancelled", 3: "pending", 4: "processing", 5: "failed", 6: "failed"}


def test_a_failing_sweep_does_not_stop_pending_campaigns_from_being_sent(monkeypatch):
    db = _DB(scheduled_campaigns=[_campaign()])
    calls = {"n": 0}

    def first_update_fails(q):
        calls["n"] += 1
        if calls["n"] == 1:               # the sweep runs first
            raise Exception("db hiccup")
    db.hooks[("scheduled_campaigns", "update")] = first_update_fails
    sent, _ = _run(monkeypatch, db)
    assert sent == [1]


# ── trial emails: formats, skips, response shape ─────────────────────────────
def test_trial_end_in_z_and_naive_formats(monkeypatch, mail):
    z = (_now() + timedelta(days=7, hours=1)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    b1 = {**_biz(1), "trial_ends_at": z}
    b2 = {**_biz(2, email="n@x.com"),
          "trial_ends_at": (_now() + timedelta(days=7, hours=1)).replace(tzinfo=None).isoformat()}
    db = _ledger_db([b1, b2])
    out = _warnings(monkeypatch, db)
    assert out["sent"] == 1 and out["errors"] == 1          # the naive timestamp is an error, as before
    assert _warnings(monkeypatch, db)["sent"] == 0


def test_business_without_an_email_is_skipped_and_never_claimed(monkeypatch, mail):
    db = _ledger_db([_biz(1, email="")])
    out = _warnings(monkeypatch, db)
    assert out["sent"] == 0 and mail["warn"] == [] and db.tables["notification_log"] == []


def test_trial_response_keeps_the_legacy_keys(monkeypatch, mail):
    out = _warnings(monkeypatch, _ledger_db([_biz(1)]))
    assert {"ok", "sent", "errors", "already_sent"} <= set(out)


def test_days_outside_the_thresholds_are_neither_sent_nor_claimed(monkeypatch, mail):
    db = _ledger_db([_biz(1, days=5)])
    assert _warnings(monkeypatch, db)["sent"] == 0
    assert db.tables["notification_log"] == []


def test_kinds_are_separate_even_for_the_same_period(monkeypatch):
    db = _ledger_db([_biz(1)])
    monkeypatch.setattr(ledger, "supabase", db)
    assert ledger.claim(1, "trial_warn_7", "same") is True
    assert ledger.claim(1, "trial_warn_3", "same") is True
    assert ledger.claim(1, "trial_expired", "same") is True
    assert ledger.claim(1, "trial_warn_7", "same") is False


# ── immediate campaign guard ─────────────────────────────────────────────────
def test_phone_list_order_does_not_defeat_the_guard_and_tenants_are_independent(monkeypatch):
    br, calls = _send_campaign(monkeypatch, None, [{"ok": True, "sent": 2}] * 4)
    from fastapi import HTTPException
    _call(br, _body(br, audience="custom", phones=["111", "222"]))
    with pytest.raises(HTTPException):
        _call(br, _body(br, audience="custom", phones=["222", "111"]))
    other = asyncio.run(br.campaign_send(_body(br, audience="custom", phones=["111", "222"]),
                                         types.SimpleNamespace(headers={}), user={"business_id": 2},
                                         _plan=None, _restrict=None))
    assert other["sent"] == 2 and len(calls) == 2


def test_exception_during_send_releases_the_guard(monkeypatch):
    br, calls = _send_campaign(monkeypatch, None, [])
    import services.campaign_service as cs
    state = {"n": 0}

    def run(**kw):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("meta exploded")
        return {"ok": True, "sent": 1}
    monkeypatch.setattr(cs.CampaignService, "run", staticmethod(run))
    with pytest.raises(RuntimeError):
        _call(br, _body(br))
    assert _call(br, _body(br))["sent"] == 1


def test_guard_expires_after_the_window(monkeypatch):
    br, calls = _send_campaign(monkeypatch, None, [{"ok": True, "sent": 1}] * 2)
    _call(br, _body(br))
    for k in list(br._RECENT_CAMPAIGNS):
        br._RECENT_CAMPAIGNS[k] -= br._CAMPAIGN_DUP_WINDOW + 1       # pretend a minute passed
    assert _call(br, _body(br))["sent"] == 1 and len(calls) == 2


# ── security review follow-ups ───────────────────────────────────────────────
def _cancel(monkeypatch, db, campaign_id=1, bid=1):
    import core.db as coredb
    monkeypatch.setattr(coredb, "supabase", db)
    return growth.cancel_scheduled_campaign(campaign_id, user={"business_id": bid})


def test_cancel_works_for_an_unclaimed_pending_campaign(monkeypatch):
    db = _DB(scheduled_campaigns=[_campaign()])
    assert _cancel(monkeypatch, db)["status"] == "cancelled"
    sent, out = _run(monkeypatch, db)
    assert sent == [] and out["processed"] == 0                   # a cancelled campaign is never sent


def test_cancel_loses_the_race_when_the_scheduler_claims_between_check_and_write(monkeypatch):
    """The cancel's check sees 'pending'; the scheduler claims; the cancel's write must then
    match nothing and the owner must be told the truth instead of 'cancelled'."""
    from fastapi import HTTPException
    db = _DB(scheduled_campaigns=[_campaign()])
    state = {"claimed": False}

    def claim_before_cancel_write(q):
        if q.op == "update" and q.payload.get("status") == "cancelled" and not state["claimed"]:
            state["claimed"] = True
            db.tables["scheduled_campaigns"][0].update({"status": "processing", "sent_at": _now().isoformat()})
    db.hooks[("scheduled_campaigns", "update")] = claim_before_cancel_write
    with pytest.raises(HTTPException) as e:
        _cancel(monkeypatch, db)
    assert e.value.status_code == 409
    assert db.tables["scheduled_campaigns"][0]["status"] == "processing"       # not overwritten


def test_cancel_is_refused_for_a_campaign_claimed_through_the_sent_at_fallback(monkeypatch):
    from fastapi import HTTPException
    db = _DB(scheduled_campaigns=[_campaign(sent_at=_now().isoformat())])      # still 'pending' but claimed
    with pytest.raises(HTTPException) as e:
        _cancel(monkeypatch, db)
    assert e.value.status_code == 409


def test_cancel_cannot_touch_another_tenants_campaign(monkeypatch):
    from fastapi import HTTPException
    db = _DB(scheduled_campaigns=[_campaign(1, biz=2)])
    with pytest.raises(HTTPException) as e:
        _cancel(monkeypatch, db, 1, bid=1)
    assert e.value.status_code == 404
    assert db.tables["scheduled_campaigns"][0]["status"] == "pending"


def test_final_status_write_never_overwrites_a_cancelled_campaign(monkeypatch):
    """If a campaign ends up 'cancelled' while a run is finishing, the run must not flip it
    back to sent/failed."""
    db = _DB(scheduled_campaigns=[_campaign()])

    def run(**kw):
        db.tables["scheduled_campaigns"][0]["status"] = "cancelled"        # state changed during the send
        return {"ok": True, "sent": 2, "failed": 0}
    _run(monkeypatch, db, run_impl=run)
    assert db.tables["scheduled_campaigns"][0]["status"] == "cancelled"


def test_marketing_opt_out_is_a_skip_not_a_failure_and_keeps_no_claim(monkeypatch, mail):
    import services.email_service as es
    monkeypatch.setattr(es, "_is_opted_out", lambda bid: True)
    db = _ledger_db([_biz(1, days=7)])
    out = _warnings(monkeypatch, db)
    assert out["sent"] == 0 and out["errors"] == 0 and out["opted_out"] == 1
    assert db.tables["notification_log"] == [] and mail["warn"] == []


def test_opt_out_does_not_suppress_the_trial_expired_notice(monkeypatch, mail):
    import services.email_service as es
    monkeypatch.setattr(es, "_is_opted_out", lambda bid: True)
    db = _ledger_db([_biz(1, days=-2)])
    assert _warnings(monkeypatch, db)["sent"] == 1                # the expiry email is transactional, unchanged


def test_missing_column_error_is_not_mistaken_for_a_missing_table():
    assert not ledger._is_missing_table(_APIError("PGRST204", "Could not find the 'x' column of 'notification_log'"))
    assert ledger._is_missing_table(_APIError("PGRST205", "Could not find the table"))
    assert ledger._is_missing_table(_APIError("42P01", "relation does not exist"))


def test_sql_files_revoke_public_and_pin_search_path():
    import pathlib
    mig = pathlib.Path(__file__).resolve().parent.parent / "migrations"
    stock = (mig / "proposed_phase2_stock_restoration.sql").read_text(encoding="utf8")
    assert "SET search_path = public" in stock
    assert "FROM PUBLIC, anon, authenticated" in stock and "TO service_role" in stock
    ledger_sql = (mig / "notification_ledger.sql").read_text(encoding="utf8")
    assert "REVOKE ALL ON SEQUENCE public.notification_log_id_seq" in ledger_sql
