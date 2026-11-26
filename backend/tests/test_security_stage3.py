"""Security audit Stage 3: limiter memory bound, per-account write cap and the Low findings."""
from __future__ import annotations

import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

import pytest
from fastapi import FastAPI, Depends
from fastapi.testclient import TestClient

import core.auth as auth
from services import security as sec

BACKEND = Path(__file__).resolve().parents[1]


class _Req:
    def __init__(self, ip="1.2.3.4"):
        self.headers = {"x-forwarded-for": ip}
        self.client = None


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("RATE_LIMIT_BACKEND", raising=False)
    stores = (sec._rate_store, sec._rate_windows, sec._rate_maxima, sec._failed_logins,
              sec._ip_login_fails, sec._account_login_fails, sec._signup_success_store)
    for store in stores:
        store.clear()
    yield
    for store in stores:
        store.clear()


# ── F-03 ─────────────────────────────────────────────────────────────────────

def test_rate_store_is_capped_and_keeps_the_busy_key(monkeypatch):
    monkeypatch.setattr(sec, "MAX_TRACKER_KEYS", 50)
    for _ in range(5):
        sec.rate_limit("login", _Req("9.9.9.9"), 5, 60)
    for i in range(500):
        sec.rate_limit("login", _Req(f"10.0.{i // 250}.{i % 250}"), 5, 60)
    assert len(sec._rate_store) <= 50 and len(sec._rate_windows) <= 50
    with pytest.raises(sec.RateLimitExceeded):          # the limited client was not flushed out
        sec.rate_limit("login", _Req("9.9.9.9"), 5, 60)


def test_expired_keys_are_dropped_first(monkeypatch):
    monkeypatch.setattr(sec, "MAX_TRACKER_KEYS", 10)
    store = defaultdict(list)
    now = time.time()
    for i in range(10):
        store[f"old{i}"] = [now - 1000]
    store["new"] = [now]
    sec._bound_store(store, lambda _k: 60, now)
    assert list(store) == ["new"]


def test_login_trackers_are_capped_and_lockout_survives(monkeypatch):
    monkeypatch.setattr(sec, "MAX_TRACKER_KEYS", 50)
    for _ in range(sec.LOGIN_MAX_ATTEMPTS_PER_ACCOUNT):
        sec.record_failed_login_account("victim")
    for i in range(300):
        sec.record_failed_login_account(f"u{i}")
        sec.record_failed_login(f"ip{i}", f"u{i}")
        sec.record_failed_login_ip(f"ip{i}")
    assert max(len(sec._account_login_fails), len(sec._failed_logins), len(sec._ip_login_fails)) <= 50
    with pytest.raises(sec.RateLimitExceeded):
        sec.check_account_login_lockout("victim")


def test_checking_a_login_does_not_create_entries():
    sec.is_login_locked("ip", "nobody")
    sec.check_ip_login_limit("ip")
    sec.check_account_login_lockout("nobody")
    assert not sec._failed_logins and not sec._ip_login_fails and not sec._account_login_fails


def test_huge_usernames_are_truncated_in_keys():
    sec.record_failed_login_account("x" * 100_000)
    sec.record_failed_login("ip", "x" * 100_000)
    assert max(map(len, [*sec._account_login_fails, *sec._failed_logins])) < 300


def test_saturated_limit_outlives_busier_unsaturated_keys(monkeypatch):
    monkeypatch.setattr(sec, "MAX_TRACKER_KEYS", 20)
    for _ in range(5):
        sec.rate_limit("login", _Req("9.9.9.9"), 5, 60)
    for i in range(100):
        for _ in range(8):                                # more hits, far from their limit
            sec.rate_limit("public_read", _Req(f"10.1.0.{i}"), 120, 60)
    with pytest.raises(sec.RateLimitExceeded):
        sec.rate_limit("login", _Req("9.9.9.9"), 5, 60)


def test_sweep_leaves_headroom_after_dropping_expired(monkeypatch):
    monkeypatch.setattr(sec, "MAX_TRACKER_KEYS", 100)
    now = time.time()
    store = {"old": [now - 1000], **{f"k{i}": [now] for i in range(100)}}
    sec._bound_store(store, lambda _k: 60, now)
    assert len(store) == 90


def test_signup_success_check_creates_no_entry():
    sec.check_signup_success_limit(_Req())
    assert not sec._signup_success_store


def test_bad_trial_restart_value_is_tolerated():
    from routes.admin_outreach_routes import _safe_count
    assert [_safe_count(v) for v in ("x", None, {}, "3", -2)] == [0, 0, 0, 3, 0]


# ── Per-account write limit ──────────────────────────────────────────────────

def _app(monkeypatch):
    monkeypatch.setattr(auth, "is_token_revoked", lambda jti: False)
    monkeypatch.setattr(auth, "_lookup_auth_state", lambda bid: (None, True))
    app = FastAPI()

    @app.exception_handler(sec.RateLimitExceeded)
    async def _limited(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": "slow down"}, status_code=429)

    @app.api_route("/x", methods=["GET", "POST"])
    def x(user=Depends(auth.get_current_user)):
        return {"ok": True}

    return TestClient(app)


def _bearer(bid):
    token = auth.create_access_token({"sub": f"u{bid}", "role": "business", "business_id": bid})
    return {"Authorization": f"Bearer {token}"}


def test_writes_are_limited_per_business_and_reads_are_not(monkeypatch):
    monkeypatch.setenv("USER_WRITE_LIMIT_PER_MIN", "3")
    c = _app(monkeypatch)
    assert [c.post("/x", headers=_bearer(1)).status_code for _ in range(4)] == [200, 200, 200, 429]
    assert c.get("/x", headers=_bearer(1)).status_code == 200
    assert c.post("/x", headers=_bearer(2)).status_code == 200            # another account


def test_limit_follows_the_account_not_the_ip(monkeypatch):
    monkeypatch.setenv("USER_WRITE_LIMIT_PER_MIN", "1")
    c = _app(monkeypatch)
    assert c.post("/x", headers={**_bearer(1), "X-Forwarded-For": "1.1.1.1"}).status_code == 200
    assert c.post("/x", headers={**_bearer(1), "X-Forwarded-For": "2.2.2.2"}).status_code == 429


def test_write_limit_can_be_disabled(monkeypatch):
    monkeypatch.setenv("USER_WRITE_LIMIT_PER_MIN", "0")
    c = _app(monkeypatch)
    assert all(c.post("/x", headers=_bearer(1)).status_code == 200 for _ in range(20))


def test_main_app_answers_429_for_the_limit():
    from main import app
    assert sec.RateLimitExceeded in app.exception_handlers


# ── F-05 Stripe takeover ─────────────────────────────────────────────────────

def _stripe_db(monkeypatch, update_rows):
    import crud.stripe_webhook_events as ev

    class Q:
        def __init__(self):
            self.op = None

        def table(self, *_):
            self.op = None
            return self

        def insert(self, *_):
            self.op = "insert"
            return self

        def select(self, *_):
            self.op = "select"
            return self

        def update(self, *_):
            self.op = "update"
            return self

        def eq(self, *_):
            return self

        def limit(self, *_):
            return self

        def execute(self):
            if self.op == "insert":
                raise RuntimeError("duplicate key")
            data = update_rows if self.op == "update" else [
                {"status": "failed", "attempts": 1, "updated_at": "2026-01-01T00:00:00+00:00"}]
            return type("R", (), {"data": data})()

    monkeypatch.setattr(ev, "supabase", Q())
    return ev


@pytest.mark.parametrize("rows,expected", [([], "in_progress"), ([{"event_id": "e"}], "process")])
def test_stripe_takeover_only_proceeds_when_it_won(monkeypatch, rows, expected):
    ev = _stripe_db(monkeypatch, rows)
    assert ev.claim_event("evt_1", "invoice.paid") == expected


# ── F-09 / F-11 / signup / F-08 ──────────────────────────────────────────────

def test_other_tenants_customer_id_looks_unknown(monkeypatch):
    from fastapi import HTTPException
    from routes import chat_routes as cr
    import core.db as coredb

    class Q:
        def __getattr__(self, _):
            return lambda *a, **k: self

        def execute(self):
            return type("R", (), {"data": [{"id": 5, "business_id": 2}]})()

    monkeypatch.setattr(cr.crud, "get_customer_by_id", lambda cid, bid: None)
    monkeypatch.setattr(coredb, "supabase", Q())
    with pytest.raises(HTTPException) as e:
        cr._handoff_get_customer(5, 1, {"username": "a"}, "request")
    assert (e.value.status_code, e.value.detail) == (404, "Customer not found")


def test_redirect_still_works_after_the_event_cap(monkeypatch):
    from routes import marketing_routes as mk
    import services.acquisition_service as acq
    hits = []
    monkeypatch.setattr(mk, "_slug_to_business_id", lambda s: (1, "Shop"))
    monkeypatch.setattr(acq, "record_qr_scan", lambda bid: hits.append(bid))
    app = FastAPI()
    app.include_router(mk.router)
    c = TestClient(app)
    codes = [c.get("/qr/shop", follow_redirects=False).status_code for _ in range(35)]
    assert set(codes) == {302} and len(hits) == 30


def test_like_wildcards_are_escaped():
    from routes.auth_routes import _like_literal
    assert _like_literal("a_b%c*d\\e@x.com") == "a\\_b\\%c_d\\\\e@x.com"


def test_patch_me_cannot_change_server_managed_feature_keys(monkeypatch):
    from routes import business_routes as br
    saved = {}
    current = {"features_json": {"_notif_sent": {"k": 1}, "marketing_emails_opt_out": True}}
    monkeypatch.setattr(br.crud, "get_business_by_id", lambda bid: current)
    monkeypatch.setattr(br.crud, "update_business",
                        lambda bid, data: saved.update(data.dict()) or {"id": bid, **data.dict()})
    br.update_me(br.BusinessUpdate(features_json={"_notif_sent": {}, "_new": 1,
                                                  "marketing_emails_opt_out": False, "theme": "x"}),
                 user={"business_id": 1})
    assert saved["features_json"] == {"_notif_sent": {"k": 1}, "marketing_emails_opt_out": True,
                                      "theme": "x"}


def test_patch_product_ignores_server_managed_columns(monkeypatch):
    from routes import business_routes as br
    seen = {}
    monkeypatch.setattr(br.crud, "update_product", lambda pid, bid, data: seen.update(data) or {"id": pid})
    br.update_product(3, {"id": 9, "business_id": 9, "created_at": "x", "price": 2}, user={"business_id": 1})
    assert seen == {"price": 2}


# ── F-12 CORS ────────────────────────────────────────────────────────────────

def _preflight(origin, headers="authorization,content-type,x-csrf-token"):
    from main import app
    return TestClient(app).options("/me", headers={
        "Origin": origin, "Access-Control-Request-Method": "PATCH",
        "Access-Control-Request-Headers": headers})


def test_cors_allows_the_headers_the_clients_send():
    r = _preflight("https://wazibothq.com", "authorization,content-type,x-csrf-token,x-auth-mode")
    assert r.status_code == 200


def test_cors_refuses_unknown_headers_and_origins():
    assert _preflight("https://wazibothq.com", "x-evil").status_code == 400
    assert _preflight("https://evil.example").status_code == 400


def test_localhost_origins_are_dropped_on_render():
    code = ("import os,sys;sys.path[:0]=['.','tests'];import conftest,main;"
            "print('ORIGINS', [o for o in main._cors_origins if 'localhost' in o or '127.0.0.1' in o])")
    import os
    env = {**os.environ, "RENDER": "true", "ALLOWED_ORIGINS": "", "PYTHONIOENCODING": "utf-8"}
    out = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, env=env,
                         capture_output=True, text=True, encoding="utf-8", timeout=90)
    assert "ORIGINS []" in out.stdout, out.stderr[-800:]
