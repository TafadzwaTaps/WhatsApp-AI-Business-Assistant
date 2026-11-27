"""Security audit Stage 4a: the security event recorder, its hooks, repeat counting and retention."""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

import core.auth as auth
import crud.security_events as store
import services.security_events as events

_real_start_worker = events._start_worker      # captured before conftest replaces it per test


class _DB:
    """Records writes. `function` is whether the migration's database functions exist."""

    def __init__(self, function=False, error=None):
        self.function, self.error = function, error
        self.rows, self.calls = [], []
        self._pending = None

    def table(self, name):
        return self

    def insert(self, row):
        self._pending = ("insert", row)
        return self

    def rpc(self, name, params):
        self._pending = (name, params)
        return self

    def execute(self):
        kind, payload = self._pending
        if kind != "insert":
            self.calls.append((kind, payload))
            if not self.function:
                raise RuntimeError("Could not find the function public." + kind)
        if self.error:
            raise self.error
        if kind == "insert":
            self.rows.append(payload)
        return type("R", (), {"data": 3})()


@pytest.fixture
def db(monkeypatch):
    fake = _DB()
    monkeypatch.setattr(store, "supabase", fake)
    return fake


def _queued():
    return [e["fields"] for e in events._pending.values() if e["count"]]


# ── Writing ──────────────────────────────────────────────────────────────────

def test_emit_does_not_touch_the_database_until_flushed(db):
    events.emit("login_failed", ip="1.2.3.4")
    assert not db.rows and not db.calls
    assert events.flush() == 1
    assert db.rows[0]["event_type"] == "login_failed"


def test_without_the_migration_the_original_five_columns_are_written(db):
    events.emit("account_lockout", username="shop", metadata={"failed_attempts": 5})
    events.flush()
    assert db.rows == [{"event_type": "account_lockout", "ip": None, "username": "shop",
                        "business_id": None, "metadata": {"failed_attempts": 5}}]


def test_missing_function_is_not_retried_for_every_event(db):
    for n in range(3):
        events.emit("login_failed", ip=f"10.0.0.{n}")
    events.flush()
    assert len(db.rows) == 3 and len(db.calls) == 1


def test_with_the_migration_severity_and_count_are_sent(monkeypatch):
    db = _DB(function=True)
    monkeypatch.setattr(store, "supabase", db)
    for _ in range(4):
        events.emit("refresh_token_reuse", username="shop", business_id=7)
    events.flush()
    (name, params), = db.calls
    assert name == "record_security_event" and not db.rows
    assert (params["p_severity"], params["p_outcome"], params["p_count"]) == ("high", "blocked", 4)
    assert len(params["p_dedup_key"]) == 64 and "shop" not in params["p_dedup_key"]


def test_repeats_are_batched_and_none_are_lost(db):
    events.emit("rate_limit_exceeded", ip="1.1.1.1")
    events.flush()
    for _ in range(50):
        events.emit("rate_limit_exceeded", ip="1.1.1.1")
    assert events.flush() == 0                      # too soon: still counting
    assert events.flush(force=True) == 1
    assert sum(store.occurrences_of(r) for r in db.rows) == 51


def test_different_sources_are_separate_events(db):
    events.emit("login_failed", ip="1.1.1.1")
    events.emit("login_failed", ip="2.2.2.2")
    events.emit("login_failed", ip="1.1.1.1", rule="superadmin")
    assert events.flush() == 3


def test_writes_per_minute_are_capped_and_urgent_events_go_first(db, monkeypatch):
    monkeypatch.setattr(events, "MAX_WRITES_PER_MINUTE", 2)
    for n in range(5):
        events.emit("login_failed", ip=f"10.0.0.{n}")
    events.emit("cross_tenant_blocked", business_id=1)
    assert events.flush() == 2
    assert db.rows[0]["event_type"] == "cross_tenant_blocked"
    assert len(_queued()) == 4                      # waiting, not dropped


def test_a_full_queue_drops_new_events_without_failing(db, monkeypatch):
    monkeypatch.setattr(events, "MAX_PENDING_EVENTS", 3)
    for n in range(10):
        events.emit("login_failed", ip=f"10.0.0.{n}")
    assert len(events._pending) == 3
    events.flush()
    events.emit("login_failed", ip="10.0.1.1")      # written entries make room again
    assert [f["ip"] for f in _queued()] == ["10.0.1.1"]


def test_secrets_are_redacted_and_sizes_bounded(db):
    events.emit("admin_action", username="u" * 500, user_agent="a" * 900,
                metadata={"api_key": "sk-live", "note": "ok"})
    events.emit("login_failed", metadata={"blob": "x" * 10_000})
    events.flush()
    by_type = {r["event_type"]: r for r in db.rows}
    assert by_type["admin_action"]["metadata"] == {"api_key": "[redacted]", "note": "ok"}
    assert len(by_type["admin_action"]["username"]) == 120
    assert by_type["login_failed"]["metadata"] == {"truncated": True}


def test_neither_a_bad_call_nor_a_dead_database_raises(monkeypatch):
    events.emit("login_failed", request=object())   # not a request at all
    monkeypatch.setattr(store, "supabase", _DB(error=RuntimeError("down")))
    events.emit("login_failed", ip="1.1.1.1")
    assert events.flush() == 1


def test_background_thread_writes_a_new_event(monkeypatch):
    wrote = threading.Event()

    def _flush_once(**_):
        if events._pending:
            wrote.set()
            raise SystemExit                        # end the thread so it cannot outlive the test

    monkeypatch.setattr(events, "flush", _flush_once)
    monkeypatch.setattr(events, "_thread", None)
    monkeypatch.setattr(events, "_start_worker", _real_start_worker)
    events.emit("login_failed", ip="3.3.3.3")
    assert wrote.wait(5)
    events._thread.join(5)


# ── Retention ────────────────────────────────────────────────────────────────

def test_retention_waits_then_runs_daily(monkeypatch):
    runs = []
    clock = [1000.0]
    monkeypatch.setattr(events.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(store, "prune_security_events", lambda days: runs.append(days))
    monkeypatch.delenv("SECURITY_EVENT_RETENTION_DAYS", raising=False)
    for step in (0, 1, 600, 1, 86400):
        clock[0] += step
        events._prune_if_due()
    assert runs == [180, 180]


def test_retention_can_be_disabled(monkeypatch):
    monkeypatch.setenv("SECURITY_EVENT_RETENTION_DAYS", "0")
    monkeypatch.setattr(store, "prune_security_events", lambda days: pytest.fail("must not prune"))
    monkeypatch.setattr(events, "_next_prune", 1.0)
    events._prune_if_due()


def test_retention_is_a_no_op_before_the_migration(db):
    assert store.prune_security_events(180) is None and not db.rows
    db.function = True
    assert store.prune_security_events(180) == 3


# ── Hooks ────────────────────────────────────────────────────────────────────

@pytest.fixture
def client(monkeypatch):
    from main import app
    monkeypatch.setattr(auth, "is_token_revoked", lambda jti: False)
    monkeypatch.setattr(auth, "_lookup_auth_state", lambda bid: (None, True))
    import services.security as sec
    for tracker in (sec._rate_store, sec._ip_login_fails, sec._account_login_fails, sec._failed_logins):
        tracker.clear()
    yield TestClient(app)
    for tracker in (sec._rate_store, sec._ip_login_fails, sec._account_login_fails, sec._failed_logins):
        tracker.clear()


def _of_type(event_type):
    return [f for f in _queued() if f["event_type"] == event_type]


def test_login_failure_for_an_unknown_name_does_not_store_what_was_typed(client, monkeypatch):
    import crud
    monkeypatch.setattr(crud, "get_business_by_username", lambda _u: None)
    r = client.post("/auth/login", json={"username": "MyPassword123", "password": "x"})
    assert r.status_code == 401
    event, = _of_type("login_failed")
    assert (event["username"], event["rule"], event["endpoint"]) == (None, "unknown_account", "/auth/login")
    assert "mypassword123" not in str(event).lower()


def test_login_failure_for_a_real_account_names_it(client, monkeypatch):
    import crud
    biz = {"id": 4, "owner_username": "shop", "owner_password": auth.hash_password("right-password")}
    monkeypatch.setattr(crud, "get_business_by_username", lambda _u: biz)
    assert client.post("/auth/login", json={"username": "shop", "password": "wrong"}).status_code == 401
    event, = _of_type("login_failed")
    assert (event["username"], event["business_id"], event["rule"]) == ("shop", 4, "wrong_password")


def test_rate_limit_refusal_is_recorded_with_the_route(client, monkeypatch):
    import crud
    monkeypatch.setattr(crud, "get_business_by_username", lambda _u: None)
    codes = [client.post("/auth/login", json={"username": "a", "password": "x"}).status_code for _ in range(8)]
    assert 429 in codes
    assert {e["endpoint"] for e in _of_type("rate_limit_exceeded")} == {"/auth/login"}


def test_revoked_token_is_recorded(client, monkeypatch):
    monkeypatch.setattr(auth, "is_token_revoked", lambda jti: True)
    token = auth.create_access_token({"sub": "shop", "role": "business", "business_id": 2})
    r = client.get("/chat/customers", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401
    assert _of_type("revoked_token_used")[0]["business_id"] == 2


def test_support_session_write_is_recorded_once(client):
    token = auth.create_access_token({"sub": "shop", "role": "business", "business_id": 2,
                                      "impersonated_by": "root"})
    r = client.post("/chat/read/1", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403
    assert [e["username"] for e in _of_type("support_write_blocked")] == ["root"]
    assert not _of_type("admin_action")             # the audit row for it is not a second event


def test_socket_for_another_business_is_recorded(client):
    token = auth.create_access_token({"sub": "shop", "role": "business", "business_id": 2})
    with pytest.raises(Exception):
        with client.websocket_connect("/ws/chat/9") as ws:
            ws.send_json({"type": "auth", "token": token})
            ws.receive_text()
    event, = _of_type("cross_tenant_blocked")
    assert (event["business_id"], event["rule"], event["metadata"]) == (2, "websocket",
                                                                        {"requested_business_id": 9})


def test_admin_actions_and_exports_are_recorded():
    from crud.admin_audit import log_admin_action
    log_admin_action(actor_username="root", action="business.suspend", business_id=3)
    log_admin_action(actor_username="root", action="data_export")
    log_admin_action(actor_username="root", action="support.access", business_id=3)
    assert sorted((e["event_type"], e["rule"]) for e in _queued()) == [
        ("admin_action", "business.suspend"), ("data_export", "data_export")]


def test_ai_quota_refusal_is_recorded(monkeypatch):
    import core.plan_guard as plan_guard
    from services.ai_usage_tracker import business_llm_quota_ok
    monkeypatch.setattr(plan_guard, "check_ai_usage_limit", lambda _b: None)
    assert business_llm_quota_ok(5) is True and not _queued()
    monkeypatch.setattr(plan_guard, "check_ai_usage_limit", lambda _b: {"error": "ai_quota"})
    assert business_llm_quota_ok(5) is False
    assert _of_type("ai_quota_reached")[0]["business_id"] == 5


# ── History ──────────────────────────────────────────────────────────────────

def test_history_adds_up_repeats_from_both_write_paths(client, fake_supabase):
    when = "2026-09-10T00:00:00+00:00"
    fake_supabase.results["security_events"] = [
        {"event_type": "login_failed", "created_at": when, "occurrence_count": 7, "metadata": {}},
        {"event_type": "login_failed", "created_at": when, "metadata": {"occurrences": 3}},
        {"event_type": "login_failed", "created_at": when},
    ]
    client.app.dependency_overrides[auth.get_current_user] = lambda: {
        "username": "root", "role": "superadmin", "business_id": None}
    try:
        body = client.get("/admin/saas/security/history").json()
    finally:
        client.app.dependency_overrides.clear()
    assert body["totals_by_type"]["login_failed"] == 11
    assert body["daily_counts"][0]["login_failed"] == 11


@pytest.mark.parametrize("total,expected,truncated", [(5, 5, False), (4, 4, False), (50, 6, True)])
def test_history_reads_every_page_and_says_when_it_cannot(monkeypatch, total, expected, truncated):
    class Paged:
        def __getattr__(self, _name):
            return lambda *a, **k: self

        def range(self, start, end):
            self.span = (start, end)
            return self

        def execute(self):
            start, end = self.span
            return type("R", (), {"data": [{"id": n} for n in range(total)][start:end + 1]})()

    monkeypatch.setattr(store, "supabase", Paged())
    rows, cut = store.list_all_security_events_since("2026-01-01", page=2, max_rows=6)
    assert ([r["id"] for r in rows], cut) == (list(range(expected)), truncated)


def test_every_reported_event_type_is_listed():
    import pathlib
    import re
    root = pathlib.Path(__file__).resolve().parents[1]
    call = re.compile(r"(?:emit|emit_security_event|_persist_security_event)\(\s*\"([a-z_]+)\""
                      r"|log_security_event\(\s*event_type=\"([a-z_]+)\"")
    reported = set()
    for path in root.rglob("*.py"):
        if "tests" not in path.parts:
            reported.update(a or b for a, b in call.findall(path.read_text(encoding="utf-8", errors="ignore")))
    assert reported and reported <= set(store.EVENT_TYPES), reported - set(store.EVENT_TYPES)
