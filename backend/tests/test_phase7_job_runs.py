"""Phase 7, stage 2: the background-job run log (observability only, never changes a job)."""
import inspect
import os
import threading
import types
from datetime import datetime, timedelta, timezone

import pytest

from services import job_runs as jr
from test_phase1c_job_idempotency import _DB as _JobDB, _campaign, _run   # noqa: F401  (campaign runner harness)


class _Q:
    def __init__(self, db, t):
        self.db, self.t, self.op, self.payload, self.f, self._limit = db, t, "select", None, [], None

    def select(self, *a, **k): self.op = "select"; return self
    def insert(self, row): self.op, self.payload = "insert", row; return self
    def delete(self): self.op = "delete"; return self
    def lt(self, c, v): self.f.append(lambda r: str(r.get(c)) < str(v)); return self
    def order(self, *a, **k): return self
    def limit(self, n): self._limit = n; return self

    def execute(self):
        if self.t in self.db.missing:
            raise Exception({"code": "42P01", "message": "relation does not exist"})
        if self.db.fail and self.op != "select":
            raise Exception("connection reset")
        rows = self.db.tables.setdefault(self.t, [])
        if self.op == "insert":
            rows.append(dict(self.payload))
            return types.SimpleNamespace(data=[dict(self.payload)])
        if self.op == "delete":
            gone = [r for r in rows if all(f(r) for f in self.f)]
            for r in gone:
                rows.remove(r)
            self.db.deletes += 1
            return types.SimpleNamespace(data=gone)
        out = sorted(rows, key=lambda r: str(r.get("started_at")), reverse=True)
        return types.SimpleNamespace(data=out[: self._limit] if self._limit else out)


class _DB:
    def __init__(self):
        self.tables, self.missing, self.fail, self.deletes = {"job_runs": []}, set(), False, 0

    def table(self, name): return _Q(self, name)

    @property
    def runs(self): return self.tables["job_runs"]


@pytest.fixture
def db(monkeypatch):
    import core.db as coredb
    d = _DB()
    monkeypatch.setattr(coredb, "supabase", d)
    jr._reset_for_tests()
    yield d
    jr._reset_for_tests()


def now(): return datetime.now(timezone.utc)


# ── pure helpers ──────────────────────────────────────────────────────────────
def test_counts_keep_only_integers():
    out = jr.counts_from({"sent": 3, "skipped": 0, "ok": True, "phone": "263771000111", "email": "a@b.c",
                          "results": [1, 2], "ratio": 0.5, "neg": -1, "huge": 10**12})
    assert out == {"sent": 3, "skipped": 0}


def test_counts_from_non_dicts_is_empty():
    assert jr.counts_from(None) == {} and jr.counts_from("x") == {} and jr.counts_from([1]) == {}


@pytest.mark.parametrize("result,err,status", [
    ({"sent": 2}, False, "ok"), ({"sent": 2, "errors": 1}, False, "partial"), ({"failed": 3}, False, "partial"),
    ({"ok": False, "error": "x"}, False, "error"), (None, False, "ok"), ({"sent": 1}, True, "error"),
    ({"errors": 0, "failed": 0}, False, "ok"),
])
def test_status_classification(result, err, status):
    assert jr.status_from(result, error=err) == status


# ── recording ─────────────────────────────────────────────────────────────────
def test_record_stores_a_run_with_duration_and_counts(db):
    t0 = now() - timedelta(seconds=2)
    assert jr.record_result("monthly_report", t0, {"sent": 4, "skipped": 1, "errors": 0}) is True
    r = db.runs[0]
    assert r["job"] == "monthly_report" and r["status"] == "ok" and r["summary"] == {"sent": 4, "skipped": 1, "errors": 0}
    assert r["duration_ms"] >= 1900 and r["error_code"] is None


def test_record_never_raises_when_the_table_is_missing_or_the_db_fails(db):
    db.missing = {"job_runs"}
    assert jr.record("x", status="ok", started_at=now()) is False
    jr._reset_for_tests()
    db.missing = set()
    db.fail = True
    assert jr.record("x", status="ok", started_at=now()) is False


def test_a_missing_table_is_reprobed_so_applying_the_migration_needs_no_restart(db, monkeypatch):
    db.missing = {"job_runs"}
    assert jr.record("x", status="ok", started_at=now()) is False
    db.missing = set()
    assert jr.record("x", status="ok", started_at=now()) is False            # still inside the 60 s back-off
    monkeypatch.setattr(jr, "_ready_at", -10_000.0)
    assert jr.record("x", status="ok", started_at=now()) is True


def test_invalid_status_is_refused(db):
    assert jr.record("x", status="weird", started_at=now()) is False and db.runs == []


def test_error_code_is_a_type_name_only_and_bounded(db):
    jr.record("x", status="error", started_at=now(), error_code="E" * 500)
    assert len(db.runs[0]["error_code"]) == 60


# ── run_tracked ───────────────────────────────────────────────────────────────
def test_run_tracked_returns_the_result_unchanged_and_records(db):
    res = {"sent": 2, "note": "x"}
    assert jr.run_tracked("booking_reminders", lambda a, b=0: res if (a, b) == (1, 2) else None, 1, b=2) is res
    assert db.runs[0]["status"] == "ok" and db.runs[0]["summary"] == {"sent": 2}


def test_run_tracked_records_an_exception_by_type_only_and_reraises(db):
    def boom():
        raise ValueError("secret-token-abc123 for +263771000111")
    with pytest.raises(ValueError):
        jr.run_tracked("monthly_report", boom)
    r = db.runs[0]
    assert r["status"] == "error" and r["error_code"] == "ValueError"
    assert "abc123" not in str(r) and "263771" not in str(r)


def test_a_broken_recorder_never_changes_the_job_outcome(db):
    db.fail = True
    assert jr.run_tracked("x", lambda: {"sent": 1}) == {"sent": 1}
    with pytest.raises(KeyError):
        jr.run_tracked("x", lambda: (_ for _ in ()).throw(KeyError("k")))


def test_old_runs_are_pruned_at_most_once_an_hour(db):
    db.runs.append({"job": "old", "status": "ok", "started_at": (now() - timedelta(days=90)).isoformat()})
    jr.record("x", status="ok", started_at=now())
    jr.record("x", status="ok", started_at=now())
    assert db.deletes == 1 and all(r["job"] != "old" for r in db.runs)


# ── overview / staleness ──────────────────────────────────────────────────────
def test_overview_summarises_each_job_and_flags_stale_ones(db):
    old = now() - timedelta(days=40)
    db.runs.extend([
        {"job": "monthly_report", "status": "ok", "started_at": old.isoformat(), "error_code": None},
        {"job": "booking_reminders", "status": "ok", "started_at": (now() - timedelta(minutes=20)).isoformat(), "error_code": None},
        {"job": "booking_reminders", "status": "error", "started_at": (now() - timedelta(hours=3)).isoformat(), "error_code": "OSError"},
        {"job": "custom_job", "status": "error", "started_at": old.isoformat(), "error_code": "X"},
    ])
    o = jr.overview()
    assert o["available"] and o["jobs"]["monthly_report"]["stale"] is True
    b = o["jobs"]["booking_reminders"]
    assert b["stale"] is False and b["last_error"]["code"] == "OSError" and b["last_ok"]
    assert o["jobs"]["custom_job"]["stale"] is False                          # no expectation set for unknown jobs
    assert o["jobs"]["custom_job"]["last_ok"] is None


def test_a_job_that_never_succeeded_is_stale_when_it_is_expected(db):
    db.runs.append({"job": "trial_warnings", "status": "error", "started_at": now().isoformat(), "error_code": "E"})
    assert jr.overview()["jobs"]["trial_warnings"]["stale"] is True


def test_overview_without_the_table_says_unavailable(db):
    db.missing = {"job_runs"}
    assert jr.overview() == {"available": False, "runs": [], "jobs": {}}


def test_overview_limit_is_clamped(db):
    for i in range(5):
        db.runs.append({"job": "a", "status": "ok", "started_at": (now() - timedelta(minutes=i)).isoformat()})
    assert len(jr.overview(2)["runs"]) == 2 and len(jr.overview(-5)["runs"]) == 1 and len(jr.overview(10**9)["runs"]) == 5


# ── admin endpoint is superadmin-only ─────────────────────────────────────────
def test_job_runs_endpoint_requires_superadmin():
    from _routes_util import find_route
    import main
    route = find_route(main.app, "/admin/saas/job-runs", "GET")
    deps = [getattr(d.call, "__name__", "") for d in route.dependant.dependencies]
    assert "require_superadmin" in deps


def test_job_runs_endpoint_returns_the_overview(db):
    from routes.saas_admin_routes import saas_job_runs
    db.runs.append({"job": "monthly_report", "status": "ok", "started_at": now().isoformat(), "error_code": None})
    assert saas_job_runs(limit=10, user={"role": "superadmin"})["jobs"]["monthly_report"]["last_ok"]


# ── wiring ────────────────────────────────────────────────────────────────────
def test_scheduled_campaign_runs_are_recorded(monkeypatch, db):
    jdb = _JobDB(scheduled_campaigns=[_campaign()], job_runs=[])
    jr._reset_for_tests()
    _run(monkeypatch, jdb, run_impl=lambda **kw: {"ok": True, "sent": 3, "failed": 0, "total": 3})
    rows = jdb.tables["job_runs"]
    assert len(rows) == 1 and rows[0]["job"] == "scheduled_campaigns" and rows[0]["summary"]["processed"] == 1


def test_jobs_are_wired_to_the_recorder():
    base = os.path.join(os.path.dirname(__file__), "..")
    def src(rel): return open(os.path.join(base, rel), encoding="utf8").read()
    assert 'run_tracked("monthly_report", send_weekly_reports)' in src("services/weekly_report_service.py")
    assert 'run_tracked("booking_reminders", run_all_due_reminders)' in src("services/booking_service.py")
    b = src("routes_saas/billing_routes.py")
    assert 'record_result("trial_warnings"' in b and 'run_tracked("account_purge", purge_due_accounts)' in b


def test_the_migration_is_additive_and_locked_down():
    sql = open(os.path.join(os.path.dirname(__file__), "..", "migrations", "job_runs.sql"), encoding="utf8").read()
    code = "\n".join(l for l in sql.splitlines() if not l.strip().startswith("--"))
    assert "CREATE TABLE IF NOT EXISTS public.job_runs" in code and "ENABLE ROW LEVEL SECURITY" in code
    assert "REVOKE ALL ON public.job_runs FROM anon, authenticated" in code
    import re
    assert not re.search(r"\bDROP\b|\bALTER TABLE public\.(?!job_runs)|\bDELETE\b|\bUPDATE\b", code, re.I)


def test_recording_is_thread_safe_enough(db):
    ts = [threading.Thread(target=lambda: jr.record("x", status="ok", started_at=now())) for _ in range(10)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert len(db.runs) == 10
