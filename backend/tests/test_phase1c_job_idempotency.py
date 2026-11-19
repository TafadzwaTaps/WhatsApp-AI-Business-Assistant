"""Phase 1 (approved exception): scheduled campaigns are sent at most once, and each trial
notification is sent at most once per business and trial period - across retries, overlapping
runs, crashes and failed sends. A stateful in-memory database stands in for Supabase; nothing
is sent."""
import asyncio
import threading
import types
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

import crud.notification_ledger as ledger
import routes.growth_routes as growth
import routes_saas.billing_routes as billing


# ── a small stateful fake of the supabase query builder ───────────────────────
class _Not:
    def __init__(self, q):
        self.q = q

    def is_(self, col, val):
        want_null = (val == "null")
        self.q.f.append(lambda r: (r.get(col) is not None) if want_null else (r.get(col) is None))
        return self.q


class _Q:
    def __init__(self, db, table):
        self.db, self.t, self.f, self.op, self.payload, self._limit = db, table, [], "select", None, None

    @property
    def not_(self):
        return _Not(self)

    def select(self, *_a, **_k):
        self.op = "select"
        return self

    def insert(self, row):
        self.op, self.payload = "insert", row
        return self

    def update(self, row):
        self.op, self.payload = "update", row
        return self

    def delete(self):
        self.op = "delete"
        return self

    def eq(self, c, v):
        self.f.append(lambda r: r.get(c) == v)
        return self

    def lte(self, c, v):
        self.f.append(lambda r: (r.get(c) or "") <= v)
        return self

    def is_(self, c, v):
        self.f.append(lambda r: (r.get(c) is None) if v == "null" else (r.get(c) == v))
        return self

    def in_(self, c, vals):
        self.f.append(lambda r: r.get(c) in vals)
        return self

    def limit(self, n):
        self._limit = n
        return self

    def order(self, *a, **k):
        return self

    def execute(self):
        self.db.calls.append((self.t, self.op, self.payload))
        hook = self.db.hooks.get((self.t, self.op))
        if hook:
            hook(self)
        rows = self.db.tables.setdefault(self.t, [])
        res = types.SimpleNamespace(data=[])
        with self.db.lock:
            if self.op == "insert":
                if self.t in self.db.unique:
                    keys = self.db.unique[self.t]
                    for r in rows:
                        if all(r.get(k) == self.payload.get(k) for k in keys):
                            raise Exception('duplicate key value violates unique constraint "x" (23505)')
                row = dict(self.payload)
                row.setdefault("id", len(rows) + 1)
                rows.append(row)
                res.data = [row]
            else:
                hit = [r for r in rows if all(f(r) for f in self.f)]
                if self.op == "select":
                    res.data = [dict(r) for r in hit][: self._limit] if self._limit else [dict(r) for r in hit]
                elif self.op == "update":
                    for r in hit:
                        r.update(self.payload)
                    res.data = [dict(r) for r in hit]
                elif self.op == "delete":
                    for r in hit:
                        rows.remove(r)
                    res.data = [dict(r) for r in hit]
        return res


class _DB:
    def __init__(self, **tables):
        self.tables, self.calls, self.hooks, self.unique = tables, [], {}, {}
        self.lock = threading.RLock()

    def table(self, name):
        return _Q(self, name)


def _now():
    return datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _reset_probe():
    ledger._reset_probe_for_tests()
    yield
    ledger._reset_probe_for_tests()


# ═════════════════════════ scheduled campaigns ═══════════════════════════════
def _campaign(i=1, biz=1, status="pending", sent_at=None):
    return {"id": i, "business_id": biz, "audience": "all", "message": f"hello {i}",
            "scheduled_at": (_now() - timedelta(minutes=5)).isoformat(), "status": status,
            "sent_at": sent_at}


def _run(monkeypatch, db, run_impl=None, secret="s"):
    import core.db as coredb
    monkeypatch.setattr(coredb, "supabase", db)
    monkeypatch.setenv("CRON_SECRET", secret)
    sent = []

    def default_run(**kw):
        sent.append(kw["business_id"])
        return {"ok": True, "sent": 3, "failed": 0}
    import services.campaign_service as cs
    monkeypatch.setattr(cs.CampaignService, "run", staticmethod(run_impl or default_run))
    req = types.SimpleNamespace(headers={"x-cron-secret": secret})
    return sent, asyncio.run(growth.run_scheduled_campaigns(req))


def test_due_campaign_is_sent_once_and_a_second_run_sends_nothing(monkeypatch):
    db = _DB(scheduled_campaigns=[_campaign()])
    sent, out = _run(monkeypatch, db)
    assert sent == [1] and out["processed"] == 1
    assert db.tables["scheduled_campaigns"][0]["status"] == "sent"
    sent2, out2 = _run(monkeypatch, db)
    assert sent2 == [] and out2["processed"] == 0


def test_a_crashed_run_that_left_the_campaign_processing_is_never_resent(monkeypatch):
    """The real duplicate vector: crash/deploy mid-send leaves the row claimed, and the next
    run used to find it 'pending' and message everyone again."""
    db = _DB(scheduled_campaigns=[_campaign(status="processing", sent_at=_now().isoformat())])
    sent, out = _run(monkeypatch, db)
    assert sent == [] and out["processed"] == 0
    assert db.tables["scheduled_campaigns"][0]["status"] == "processing"     # fresh claim: left alone


class _ProcessDied(BaseException):
    """Simulates the process being killed mid-send (not catchable by `except Exception`)."""


def test_process_dying_mid_send_never_leads_to_a_second_send(monkeypatch):
    """The scenario that duplicated campaigns before: the run starts sending, the process dies
    (deploy/restart/OOM) before the final status write, and the NEXT run used to find the row
    still 'pending' and message every recipient again."""
    db = _DB(scheduled_campaigns=[_campaign()])
    sent = []

    def dies(**kw):
        sent.append("partial")           # some recipients already got it
        raise _ProcessDied()
    with pytest.raises(_ProcessDied):
        _run(monkeypatch, db, run_impl=dies)
    assert db.tables["scheduled_campaigns"][0]["status"] == "processing"    # claimed, never 'pending' again
    sent2, out2 = _run(monkeypatch, db)
    assert sent2 == [] and out2["processed"] == 0                          # NOT re-sent
    # an hour later the interrupted campaign is surfaced as failed (still not re-sent)
    db.tables["scheduled_campaigns"][0]["sent_at"] = (_now() - timedelta(minutes=90)).isoformat()
    sent3, out3 = _run(monkeypatch, db)
    assert sent3 == [] and out3["interrupted_marked_failed"] == 1
    assert db.tables["scheduled_campaigns"][0]["status"] == "failed"


def test_a_campaign_stalled_for_over_an_hour_is_marked_failed_not_resent(monkeypatch):
    old = (_now() - timedelta(minutes=growth._STALLED_CLAIM_MINUTES + 5)).isoformat()
    db = _DB(scheduled_campaigns=[_campaign(status="processing", sent_at=old)])
    sent, out = _run(monkeypatch, db)
    assert sent == [] and out["interrupted_marked_failed"] == 1
    assert db.tables["scheduled_campaigns"][0]["status"] == "failed"


def test_final_status_write_failing_does_not_cause_a_resend(monkeypatch):
    db = _DB(scheduled_campaigns=[_campaign()])

    state = {"n": 0}

    def fail_final_update(q):
        # 1st update = the claim; 2nd update = the final status write -> fails
        if q.op == "update":
            state["n"] += 1
            if state["n"] == 2:
                raise Exception("db down")
    db.hooks[("scheduled_campaigns", "update")] = fail_final_update
    sent, _ = _run(monkeypatch, db)
    assert sent == [1]
    db.hooks.clear()
    sent2, _ = _run(monkeypatch, db)
    assert sent2 == []                                  # still claimed ('processing'), not re-sent


def test_two_overlapping_claims_have_exactly_one_winner(monkeypatch):
    db = _DB(scheduled_campaigns=[_campaign()])
    results = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        results.append(growth._claim_scheduled_campaign(db, 1))
    ts = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert results.count(True) == 1 and results.count(False) == 7


def test_claim_falls_back_to_sent_at_when_the_database_rejects_the_processing_status(monkeypatch):
    db = _DB(scheduled_campaigns=[_campaign()])

    def reject_status(q):
        if q.op == "update" and q.payload.get("status") == "processing":
            raise Exception('new row violates check constraint "scheduled_campaigns_status_check"')
    db.hooks[("scheduled_campaigns", "update")] = reject_status
    assert growth._claim_scheduled_campaign(db, 1) is True
    assert growth._claim_scheduled_campaign(db, 1) is False          # second claim loses
    row = db.tables["scheduled_campaigns"][0]
    assert row["status"] == "pending" and row["sent_at"]


def test_claim_failure_means_no_send(monkeypatch):
    db = _DB(scheduled_campaigns=[_campaign()])

    def always_fail(q):
        if q.op == "update":
            raise Exception("db down")
    db.hooks[("scheduled_campaigns", "update")] = always_fail
    sent, out = _run(monkeypatch, db)
    assert sent == [] and out["skipped_already_claimed"] == 1


def test_cancelled_campaign_is_not_sent_and_tenants_are_independent(monkeypatch):
    db = _DB(scheduled_campaigns=[_campaign(1, 1, "cancelled"), _campaign(2, 2), _campaign(3, 1)])
    sent, out = _run(monkeypatch, db)
    assert sorted(sent) == [1, 2] and out["processed"] == 2          # business ids, one per campaign


def test_a_failed_send_run_is_marked_failed_and_not_retried_automatically(monkeypatch):
    db = _DB(scheduled_campaigns=[_campaign()])

    def boom(**kw):
        raise RuntimeError("meta down")
    sent, out = _run(monkeypatch, db, run_impl=boom)
    assert db.tables["scheduled_campaigns"][0]["status"] == "failed"
    sent2, _ = _run(monkeypatch, db)
    assert sent2 == []                                  # the owner reschedules on purpose


def test_cron_secret_still_required(monkeypatch):
    import core.db as coredb
    monkeypatch.setattr(coredb, "supabase", _DB(scheduled_campaigns=[_campaign()]))
    monkeypatch.setenv("CRON_SECRET", "s")
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        asyncio.run(growth.run_scheduled_campaigns(types.SimpleNamespace(headers={"x-cron-secret": "bad"})))
    assert e.value.status_code == 403


# ── immediate campaign: double-click guard ────────────────────────────────────
def _send_campaign(monkeypatch, body, results):
    import routes.business_routes as br
    monkeypatch.setattr(br, "_rate_check", lambda *a, **k: None)
    import core.platform_controls as pc
    monkeypatch.setattr(pc, "is_paused", lambda *_a, **_k: False)
    import services.campaign_service as cs
    calls = []

    def run(**kw):
        calls.append(kw)
        return results.pop(0)
    monkeypatch.setattr(cs.CampaignService, "run", staticmethod(run))
    br._RECENT_CAMPAIGNS.clear()
    return br, calls


def _body(br, message="Big sale today", audience="all", dry_run=False, phones=None):
    return br.CampaignRequest(audience=audience, message=message, dry_run=dry_run, phone_list=phones)


def _call(br, body):
    return asyncio.run(br.campaign_send(body, types.SimpleNamespace(headers={}), user={"business_id": 1},
                                        _plan=None, _restrict=None))


def test_identical_campaign_within_a_minute_is_refused_but_a_different_one_is_allowed(monkeypatch):
    br, calls = _send_campaign(monkeypatch, None, [{"ok": True, "sent": 5}, {"ok": True, "sent": 5}])
    assert _call(br, _body(br))["sent"] == 5
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        _call(br, _body(br))
    assert e.value.status_code == 409 and len(calls) == 1
    assert _call(br, _body(br, message="A different message"))["sent"] == 5
    assert len(calls) == 2


def test_a_campaign_that_sent_nothing_can_be_retried_immediately(monkeypatch):
    br, calls = _send_campaign(monkeypatch, None, [{"ok": False, "error": "no credentials"}, {"ok": True, "sent": 2}])
    _call(br, _body(br))
    assert _call(br, _body(br))["sent"] == 2 and len(calls) == 2


def test_dry_run_is_never_blocked(monkeypatch):
    br, calls = _send_campaign(monkeypatch, None, [{"ok": True, "sent": 0, "dry_run": True}] * 3)
    for _ in range(3):
        _call(br, _body(br, dry_run=True))
    assert len(calls) == 3


# ═════════════════════════ trial notifications ═══════════════════════════════
def _biz(i=1, days=7, hours=1, email="o@example.com", fj=None):
    return {"id": i, "name": f"Biz{i}", "owner_email": email, "billing_status": "trialing",
            "trial_ends_at": (_now() + timedelta(days=days, hours=hours)).isoformat(),
            "features_json": fj if fj is not None else {}}


@pytest.fixture
def mail(monkeypatch):
    import services.email_service as es
    box = {"warn": [], "expired": [], "ok": True, "raise": False}

    def warn(email, name, days, business_id=None):
        if box["raise"]:
            raise RuntimeError("smtp")
        box["warn"].append((email, days))
        return box["ok"]

    def expired(email, name):
        if box["raise"]:
            raise RuntimeError("smtp")
        box["expired"].append(email)
        return box["ok"]
    monkeypatch.setattr(es, "send_trial_expiry_warning", warn)
    monkeypatch.setattr(es, "send_trial_expired", expired)
    return box


def _warnings(monkeypatch, db):
    import core.db as coredb
    monkeypatch.setattr(coredb, "supabase", db)
    monkeypatch.setattr(ledger, "supabase", db)
    monkeypatch.setenv("CRON_SECRET", "s")
    return billing.send_trial_warnings(types.SimpleNamespace(headers={"x-cron-secret": "s"}))


def _ledger_db(businesses, with_table=True):
    db = _DB(businesses=businesses, notification_log=[])
    db.unique["notification_log"] = ("business_id", "kind", "period_key")
    if not with_table:
        def missing(q):
            if q.t == "notification_log":
                raise Exception('relation "public.notification_log" does not exist (42P01)')
        for op in ("insert", "delete", "select"):
            db.hooks[("notification_log", op)] = missing
    return db


@pytest.mark.parametrize("with_table", [True, False])
def test_warning_is_sent_once_even_if_the_cron_runs_repeatedly(monkeypatch, mail, with_table):
    db = _ledger_db([_biz(1, days=7)], with_table)
    assert _warnings(monkeypatch, db)["sent"] == 1
    for _ in range(3):
        out = _warnings(monkeypatch, db)
        assert out["sent"] == 0 and out["already_sent"] == 1
    assert mail["warn"] == [("o@example.com", 7)]


@pytest.mark.parametrize("with_table", [True, False])
def test_expired_trial_email_is_sent_once_not_every_day(monkeypatch, mail, with_table):
    db = _ledger_db([_biz(1, days=-3)], with_table)
    for _ in range(4):
        _warnings(monkeypatch, db)
    assert mail["expired"] == ["o@example.com"]


def test_each_threshold_is_its_own_notification(monkeypatch, mail):
    db = _ledger_db([_biz(1, days=7)])
    _warnings(monkeypatch, db)
    db.tables["businesses"][0]["trial_ends_at"] = (_now() + timedelta(days=3, hours=1)).isoformat()
    # a different trial_ends_at is a different period key too, so it sends; same period tested below
    _warnings(monkeypatch, db)
    assert [d for _e, d in mail["warn"]] == [7, 3]


def test_same_period_different_threshold_and_same_threshold_are_separate_kinds():
    ledger._reset_probe_for_tests()
    assert ledger._marker("trial_warn_7", "p") != ledger._marker("trial_warn_3", "p")


def test_a_new_trial_period_gets_its_notifications_again(monkeypatch, mail):
    db = _ledger_db([_biz(1, days=7)])
    _warnings(monkeypatch, db)
    db.tables["businesses"][0]["trial_ends_at"] = (_now() + timedelta(days=37, hours=1)).isoformat()   # new trial
    assert _warnings(monkeypatch, db)["sent"] == 0                                                     # not due yet
    db.tables["businesses"][0]["trial_ends_at"] = (_now() + timedelta(days=7, hours=2)).isoformat()    # new period, due
    assert _warnings(monkeypatch, db)["sent"] == 1
    assert len(mail["warn"]) == 2


@pytest.mark.parametrize("with_table", [True, False])
def test_failed_send_is_released_and_retried_then_sent_once(monkeypatch, mail, with_table):
    db = _ledger_db([_biz(1, days=7)], with_table)
    mail["ok"] = False
    out = _warnings(monkeypatch, db)
    assert out["sent"] == 0 and out["errors"] == 1
    mail["ok"] = True
    assert _warnings(monkeypatch, db)["sent"] == 1                       # retried
    assert _warnings(monkeypatch, db)["sent"] == 0                       # and now only once
    assert len(mail["warn"]) == 2                                        # 1 failed attempt + 1 delivered


def test_send_raising_releases_the_claim(monkeypatch, mail):
    db = _ledger_db([_biz(1, days=7)])
    mail["raise"] = True
    out = _warnings(monkeypatch, db)
    assert out["errors"] == 1 and out["sent"] == 0
    mail["raise"] = False
    assert _warnings(monkeypatch, db)["sent"] == 1


def test_database_error_while_claiming_means_no_email(monkeypatch, mail):
    db = _ledger_db([_biz(1, days=7)])

    def boom(q):
        raise Exception("connection refused")
    db.hooks[("notification_log", "insert")] = boom
    out = _warnings(monkeypatch, db)
    assert out["sent"] == 0 and out["errors"] == 1 and mail["warn"] == []


def test_tenants_are_independent(monkeypatch, mail):
    db = _ledger_db([_biz(1, days=7, email="a@x.com"), _biz(2, days=7, email="b@x.com")])
    assert _warnings(monkeypatch, db)["sent"] == 2
    assert _warnings(monkeypatch, db)["sent"] == 0


def test_fallback_marker_preserves_other_settings_and_is_pruned(monkeypatch, mail):
    db = _ledger_db([_biz(1, days=7, fj={"currency": "USD", "stripe": {"x": 1}})], with_table=False)
    _warnings(monkeypatch, db)
    fj = db.tables["businesses"][0]["features_json"]
    assert fj["currency"] == "USD" and fj["stripe"] == {"x": 1} and len(fj["_notif_sent"]) == 1
    for i in range(60):
        ledger._claim_fallback(1, "k", str(i))
    assert len(db.tables["businesses"][0]["features_json"]["_notif_sent"]) <= ledger._MAX_MARKERS


@pytest.mark.parametrize("with_table", [True, False])
def test_concurrent_claims_have_exactly_one_winner(monkeypatch, with_table):
    db = _ledger_db([_biz(1)], with_table)
    monkeypatch.setattr(ledger, "supabase", db)
    results = []
    barrier = threading.Barrier(10)

    def worker():
        barrier.wait()
        results.append(ledger.claim(1, "trial_warn_7", "2026-12-01"))
    ts = [threading.Thread(target=worker) for _ in range(10)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert results.count(True) == 1


def test_release_is_safe_when_nothing_was_claimed(monkeypatch):
    db = _ledger_db([_biz(1)])
    monkeypatch.setattr(ledger, "supabase", db)
    ledger.release(1, "trial_warn_7", "p")          # must not raise
    ledger.release(99, "trial_warn_7", "p")


def test_migration_file_is_additive_and_rls_locked():
    import pathlib
    sql = (pathlib.Path(__file__).resolve().parent.parent / "migrations" / "notification_ledger.sql").read_text(encoding="utf8")
    assert "CREATE TABLE IF NOT EXISTS public.notification_log" in sql
    assert "UNIQUE (business_id, kind, period_key)" in sql
    assert "ENABLE ROW LEVEL SECURITY" in sql and "REVOKE ALL" in sql and "ROLLBACK" in sql
    assert "DROP TABLE IF EXISTS public.notification_log" in sql
    assert "DELETE" not in sql.upper().replace("-- ROLLBACK", "")
