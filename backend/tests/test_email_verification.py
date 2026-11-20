"""Email verification (Phase 4 follow-up): tokens, redemption, gating, grandfathering, degradation."""
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import core.auth as auth
import services.email_verification_service as ev


# -- a small stateful fake of the Supabase client ---------------------------------------------
class _R:
    def __init__(self, data): self.data = data


class _Q:
    def __init__(self, db, name):
        self.db, self.name, self.op, self.payload, self.flt = db, name, "select", None, []
        self._limit = None

    def select(self, *a, **k): return self
    def limit(self, n): self._limit = n; return self
    def order(self, *a, **k): return self
    def eq(self, k, v): self.flt.append(("eq", k, v)); return self
    def ilike(self, k, v): self.flt.append(("ilike", k, v)); return self
    def is_(self, k, v): self.flt.append(("is", k, v)); return self
    def insert(self, row): self.op, self.payload = "insert", row; return self
    def update(self, row): self.op, self.payload = "update", row; return self

    def _match(self, row):
        for kind, k, v in self.flt:
            cur = row.get(k)
            if kind == "eq" and cur != v: return False
            if kind == "ilike" and str(cur or "").lower() != str(v).lower(): return False
            if kind == "is" and v == "null" and cur is not None: return False
        return True

    def execute(self):
        if self.db.fail_tables.get(self.name):
            raise RuntimeError(self.db.fail_tables[self.name])
        rows = self.db.tables.setdefault(self.name, [])
        if self.op == "insert":
            row = dict(self.payload); row.setdefault("id", len(rows) + 1)
            if self.name == "email_verification_tokens" and any(r["token_hash"] == row["token_hash"] for r in rows):
                raise RuntimeError("23505 unique")
            rows.append(row); return _R([row])
        hits = [r for r in rows if self._match(r)]
        if self.op == "update":
            for r in hits: r.update(self.payload)
            return _R([dict(r) for r in hits])
        return _R([dict(r) for r in hits][: self._limit])


class _DB:
    def __init__(self): self.tables, self.fail_tables = {}, {}
    def table(self, name): return _Q(self, name)


@pytest.fixture
def db(monkeypatch):
    import core.db as coredb
    d = _DB()
    d.tables["businesses"] = [{"id": 5, "name": "Shop", "owner_username": "shop", "owner_email": "owner@example.com",
                               "email_verification_required": True, "email_verified_at": None}]
    monkeypatch.setattr(coredb, "supabase", d)
    ev._reset_probe_for_tests()
    return d


def _tokens(db): return db.tables.get("email_verification_tokens", [])


# -- the gating rule ---------------------------------------------------------------------------
@pytest.mark.parametrize("biz,expected", [
    ({"email_verification_required": True, "email_verified_at": None}, True),
    ({"email_verification_required": True}, True),
    ({"email_verification_required": True, "email_verified_at": "2026-11-01T00:00:00+00:00"}, False),
    ({"email_verification_required": False, "email_verified_at": None}, False),      # grandfathered
    ({"name": "old account, columns absent"}, False),                                # migration not applied
    ({"email_verification_required": "true"}, False),                                # only a real True counts
    (None, False), ("x", False),
])
def test_needs_verification_rule(biz, expected):
    assert ev.needs_verification(biz) is expected


def test_is_restricted_looks_up_the_account_and_fails_open(db):
    assert ev.is_restricted(5) is True
    db.tables["businesses"][0]["email_verified_at"] = "2026-11-01T00:00:00+00:00"
    assert ev.is_restricted(5) is False
    db.tables["businesses"][0].update(email_verified_at=None, email_verification_required=False)
    assert ev.is_restricted(5) is False                       # existing account keeps working
    db.fail_tables["businesses"] = "column does not exist"
    assert ev.is_restricted(5) is False                       # migration missing / DB error: never block
    assert ev.is_restricted(None) is False
    assert ev.is_restricted(999) is False                     # unknown account


# -- tokens ------------------------------------------------------------------------------------
def test_only_a_hash_is_stored_and_it_expires_in_a_day(db):
    raw = ev.issue_token(5, "Owner@Example.com", "1.2.3.4")
    assert raw and len(raw) >= 60
    row = _tokens(db)[0]
    assert row["token_hash"] == ev._hash(raw) and raw not in str(db.tables)
    assert row["email"] == "owner@example.com"
    exp = datetime.fromisoformat(row["expires_at"])
    assert timedelta(hours=23, minutes=50) < exp - datetime.now(timezone.utc) < timedelta(hours=24, minutes=1)


def test_a_new_token_retires_the_old_one(db):
    first = ev.issue_token(5, "owner@example.com")
    second = ev.issue_token(5, "owner@example.com")
    assert ev.verify_token(first)["ok"] is False
    assert ev.verify_token(second)["ok"] is True


@pytest.mark.parametrize("email", ["", "no-at-sign", None])
def test_no_token_for_a_missing_or_invalid_address(db, email):
    assert ev.issue_token(5, email) is None and _tokens(db) == []


def test_redeeming_marks_the_account_verified_once(db):
    raw = ev.issue_token(5, "owner@example.com")
    out = ev.verify_token(raw)
    assert out == {"ok": True, "business_id": 5, "first_time": True}
    assert db.tables["businesses"][0]["email_verified_at"]
    assert ev.verify_token(raw)["ok"] is False                 # single use


def test_expired_unknown_and_malformed_tokens_all_fail_generically(db):
    raw = ev.issue_token(5, "owner@example.com")
    _tokens(db)[0]["expires_at"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    results = [ev.verify_token(raw), ev.verify_token("x" * 64), ev.verify_token("short"), ev.verify_token(""),
               ev.verify_token(None), ev.verify_token("y" * 500)]
    assert all(r["ok"] is False for r in results)
    assert len({r["reason"] for r in results}) == 1            # identical text: nothing to enumerate
    assert db.tables["businesses"][0]["email_verified_at"] is None


def test_a_link_for_an_old_address_does_not_verify_the_new_one(db):
    raw = ev.issue_token(5, "owner@example.com")
    db.tables["businesses"][0]["owner_email"] = "new@example.com"       # changed before clicking
    assert ev.verify_token(raw)["ok"] is False
    assert db.tables["businesses"][0]["email_verified_at"] is None


def test_two_simultaneous_redemptions_cannot_both_win(db, monkeypatch):
    raw = ev.issue_token(5, "owner@example.com")
    orig = _Q.execute
    state = {"stolen": False}

    def racing(self):
        # between the lookup and the claim, a second request claims the token first
        if self.op == "update" and self.name == "email_verification_tokens" and not state["stolen"]:
            state["stolen"] = True
            for r in self.db.tables["email_verification_tokens"]:
                r["used_at"] = "2026-11-19T00:00:00+00:00"
        return orig(self)
    monkeypatch.setattr(_Q, "execute", racing)
    assert ev.verify_token(raw)["ok"] is False
    assert db.tables["businesses"][0]["email_verified_at"] is None


def test_database_failure_during_redemption_is_not_a_success(db):
    raw = ev.issue_token(5, "owner@example.com")
    db.fail_tables["email_verification_tokens"] = "down"
    assert ev.verify_token(raw)["ok"] is False


def test_issue_token_failure_is_reported_not_raised(db):
    db.fail_tables["email_verification_tokens"] = "table missing"
    assert ev.issue_token(5, "owner@example.com") is None
    assert ev.send_verification(5, "owner@example.com", "Shop") is False


def test_send_verification_emails_the_link_to_that_address_only(db, monkeypatch):
    import services.email_service as es
    sent = []
    monkeypatch.setattr(es, "_send", lambda to, subject, html: sent.append((to, subject, html)) or True)
    assert ev.send_verification(5, "Owner@Example.com", "<b>Shop</b>") is True
    to, subject, html = sent[0]
    raw_in_link = html.split("verify-email.html?token=")[1].split('"')[0]
    assert to == "owner@example.com" and ev._hash(raw_in_link) == _tokens(db)[0]["token_hash"]
    assert "<b>Shop</b>" not in html                                      # business name escaped


# -- the migration switch ----------------------------------------------------------------------
def test_columns_ready_reflects_the_migration_and_recovers_without_restart(db, monkeypatch):
    db.fail_tables["email_verification_tokens"] = "relation does not exist"
    assert ev.columns_ready() is False
    db.fail_tables.clear()
    assert ev.columns_ready() is False                                    # negative answer cached briefly
    ev._ready_checked_at = time.monotonic() - 120
    assert ev.columns_ready() is True


def test_status_summary(db):
    s = ev.status_for(db.tables["businesses"][0])
    assert s == {"active": True, "required": True, "verified": False, "restricted": True, "has_email": True}
    assert ev.status_for({"name": "x"})["active"] is False
    assert ev.status_for(None)["restricted"] is False


# -- routes ------------------------------------------------------------------------------------
@pytest.fixture
def client():
    import main
    return TestClient(main.app)


def _hdr(role="business", bid=5, **extra):
    claims = {"sub": "shop", "role": role, "business_id": bid, **extra}
    return {"Authorization": "Bearer " + auth.create_access_token(claims)}


@pytest.fixture
def authed(monkeypatch, db):
    import crud
    monkeypatch.setattr(auth, "_get_current_password_changed_at", lambda b: None)
    monkeypatch.setattr(auth, "_lookup_auth_state", lambda b: None)
    # crud binds the client at import time, so route code reads accounts through this fake instead
    monkeypatch.setattr(crud, "get_business_by_id",
                        lambda b: next((dict(r) for r in db.tables["businesses"] if r["id"] == b), None))


def test_verify_endpoint_success_and_generic_failure(client, db, authed, monkeypatch):
    import services.email_service as es
    welcomes = []
    monkeypatch.setattr(es, "send_welcome_email", lambda **kw: welcomes.append(kw) or True)
    raw = ev.issue_token(5, "owner@example.com")
    ok = client.post("/auth/verify-email", json={"token": raw})
    assert ok.status_code == 200 and db.tables["businesses"][0]["email_verified_at"]
    assert len(welcomes) == 1 and welcomes[0]["to_email"] == "owner@example.com"     # welcome waits for proof
    again = client.post("/auth/verify-email", json={"token": raw})
    bad = client.post("/auth/verify-email", json={"token": "z" * 64})
    assert again.status_code == bad.status_code == 400 and again.json() == bad.json()
    assert len(welcomes) == 1                                                       # no second welcome email


def test_verify_endpoint_rejects_oversized_tokens(client, db):
    assert client.post("/auth/verify-email", json={"token": "a" * 5000}).status_code == 422


def test_resend_needs_a_business_login(client, db):
    assert client.post("/auth/resend-verification").status_code == 401


def test_resend_is_refused_for_impersonation_and_superadmin(client, db, authed):
    assert client.post("/auth/resend-verification", headers=_hdr(impersonated_by="root")).status_code == 403
    assert client.post("/auth/resend-verification", headers=_hdr(role="superadmin", bid=None)).status_code in (403, 422)


def test_resend_sends_only_to_the_address_on_the_account(client, db, authed, monkeypatch):
    sent = []
    monkeypatch.setattr(ev, "send_verification", lambda bid, email, name, ip="": sent.append((bid, email)) or True)
    r = client.post("/auth/resend-verification", json={"email": "victim@example.com"},
                    headers=_hdr(bid=time.time_ns() % 10**9))
    assert r.status_code == 200 and sent == []                       # unknown account: nothing, same answer
    r = client.post("/auth/resend-verification", json={"email": "victim@example.com"}, headers=_hdr(bid=5))
    assert r.status_code == 200 and sent == [(5, "owner@example.com")]   # the request body is ignored


def test_resend_answer_is_identical_for_verified_and_unverified_accounts(client, db, authed, monkeypatch):
    monkeypatch.setattr(ev, "send_verification", lambda *a, **k: True)
    a = client.post("/auth/resend-verification", headers=_hdr(bid=5)).json()
    db.tables["businesses"][0]["email_verified_at"] = "2026-11-01T00:00:00+00:00"
    b = client.post("/auth/resend-verification", headers=_hdr(bid=5)).json()
    assert a == b


def test_resend_is_rate_limited_per_account(client, db, authed, monkeypatch):
    monkeypatch.setattr(ev, "send_verification", lambda *a, **k: True)
    bid = 700000 + time.time_ns() % 100000
    db.tables["businesses"].append({"id": bid, "owner_email": "x@example.com", "email_verification_required": True})
    codes = [client.post("/auth/resend-verification", headers=_hdr(bid=bid)).status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200] and codes[3] == 429


def test_resend_does_nothing_when_the_migration_is_not_applied(client, db, authed, monkeypatch):
    sent = []
    monkeypatch.setattr(ev, "send_verification", lambda *a, **k: sent.append(1) or True)
    db.fail_tables["email_verification_tokens"] = "missing"
    ev._reset_probe_for_tests()
    r = client.post("/auth/resend-verification", headers=_hdr(bid=5 + time.time_ns() % 1000 + 1000))
    assert r.status_code == 200 and sent == []


def test_status_endpoint(client, db, authed):
    r = client.get("/auth/verification-status", headers=_hdr(bid=5))
    assert r.status_code == 200 and r.json()["restricted"] is True
    assert client.get("/auth/verification-status", headers=_hdr(role="superadmin", bid=None)).json()["restricted"] is False


# -- signup ------------------------------------------------------------------------------------
def _signup_setup(monkeypatch, db, ready):
    import routes.auth_routes as ar
    import crud
    import services.email_service as es
    import services.growth_service as gs
    for name in ("check_signup_abuse", "check_signup_success_limit", "record_signup_success"):
        monkeypatch.setattr(ar, name, lambda *a, **k: None)
    monkeypatch.setattr(ar, "_rate_check", lambda *a, **k: None)
    monkeypatch.setattr(ar, "is_disposable_email", lambda e: False)
    monkeypatch.setattr(crud, "get_business_by_username", lambda u: None)
    monkeypatch.setattr(crud, "create_business", lambda p: {"id": 5, "name": p.name, "owner_username": p.owner_username})
    monkeypatch.setattr(gs, "start_trial", lambda b: None)
    welcomes, verifs, flagged = [], [], []
    monkeypatch.setattr(es, "send_welcome_email", lambda **kw: welcomes.append(kw) or True)
    monkeypatch.setattr(ev, "columns_ready", lambda: ready)
    monkeypatch.setattr(ev, "mark_required", lambda b: flagged.append(b) or True)
    monkeypatch.setattr(ev, "send_verification", lambda *a, **k: verifs.append(a) or True)
    db.tables["businesses"] = []
    return welcomes, verifs, flagged


_BODY = {"business_name": "New Shop", "username": "newshop", "password": "Sup3rSecret!x",
         "email": "New@Example.com", "age_confirmed": True}


def test_signup_with_verification_sends_the_link_not_the_welcome_email(client, db, monkeypatch):
    welcomes, verifs, flagged = _signup_setup(monkeypatch, db, True)
    r = client.post("/auth/signup", json=_BODY)
    assert r.status_code == 200 and r.json()["email_verification_required"] is True
    assert flagged == [5] and verifs[0][:2] == (5, "new@example.com") and welcomes == []
    assert r.json()["access_token"]                                      # still signed in immediately: no lockout


def test_signup_without_the_migration_behaves_exactly_as_before(client, db, monkeypatch):
    welcomes, verifs, flagged = _signup_setup(monkeypatch, db, False)
    r = client.post("/auth/signup", json=_BODY)
    assert r.status_code == 200 and r.json()["email_verification_required"] is False
    assert flagged == [] and verifs == [] and len(welcomes) == 1


def test_signup_survives_a_verification_failure(client, db, monkeypatch):
    welcomes, verifs, flagged = _signup_setup(monkeypatch, db, True)
    monkeypatch.setattr(ev, "send_verification", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("smtp")))
    r = client.post("/auth/signup", json=_BODY)
    assert r.status_code == 200 and r.json()["access_token"]


def test_signup_without_an_email_is_still_flagged_so_it_cannot_skip_the_gate(client, db, monkeypatch):
    welcomes, verifs, flagged = _signup_setup(monkeypatch, db, True)
    r = client.post("/auth/signup", json={**_BODY, "email": ""})
    assert r.status_code == 200 and flagged == [5] and verifs == [] and welcomes == []
    assert r.json()["email_verification_required"] is True


# -- gating ------------------------------------------------------------------------------------
def test_require_email_verified_dependency(monkeypatch):
    from core.plan_guard import require_email_verified
    dep = require_email_verified()
    monkeypatch.setattr(ev, "is_restricted", lambda b: b == 5)
    with pytest.raises(HTTPException) as e:
        dep({"business_id": 5})
    assert e.value.status_code == 403 and e.value.detail["error"] == "email_unverified"
    assert dep({"business_id": 6}) == {"business_id": 6}                 # verified / grandfathered account
    with pytest.raises(HTTPException):
        dep({})


@pytest.mark.parametrize("path,method", [("/broadcast", "POST"), ("/campaigns/send", "POST"),
                                         ("/campaigns/scheduled", "POST")])
def test_campaign_routes_carry_the_verification_guard(path, method):
    import main
    from _routes_util import find_route
    route = find_route(main.app, path, method)
    names = [d.call.__qualname__ for d in route.dependant.dependencies]
    assert any(n.startswith("require_email_verified") for n in names), names


def test_win_back_route_carries_the_guard():
    import main
    from _routes_util import iter_routes
    route = next(r for r in iter_routes(main.app.routes) if "win" in getattr(r, "path", "") and "POST" in (getattr(r, "methods", None) or ()))
    assert any(d.call.__qualname__.startswith("require_email_verified") for d in route.dependant.dependencies)


class _Chain:
    def __init__(self, data): self._data = data
    def __getattr__(self, name): return self
    def __call__(self, *a, **k): return self
    def execute(self): return _R(self._data)


class _Fixed:
    def __init__(self, data): self._data = data
    def table(self, name): return _Chain(self._data)


def _trial_biz(days=3):
    return {"id": 5, "name": "Shop", "owner_email": "owner@example.com",
            "trial_ends_at": (datetime.now(timezone.utc) + timedelta(days=days, hours=1)).isoformat()}


@pytest.mark.parametrize("restricted,expected_sent", [(True, 0), (False, 1)])
def test_trial_warnings_skip_unconfirmed_accounts(client, monkeypatch, restricted, expected_sent):
    import core.db as coredb
    import services.email_service as es
    import crud.notification_ledger as nl
    monkeypatch.setenv("CRON_SECRET", "s3cret")
    monkeypatch.setattr(coredb, "supabase", _Fixed([_trial_biz()]))
    mails = []
    monkeypatch.setattr(es, "send_trial_expiry_warning", lambda *a, **k: mails.append(a) or True)
    monkeypatch.setattr(es, "_is_opted_out", lambda b: False)
    monkeypatch.setattr(nl, "claim", lambda *a, **k: True)
    monkeypatch.setattr(ev, "is_restricted", lambda b: restricted)
    r = client.post("/billing/trial-warnings", headers={"x-cron-secret": "s3cret"})
    assert r.status_code == 200
    assert r.json()["sent"] == expected_sent and r.json()["unverified_skipped"] == (1 if restricted else 0)
    assert len(mails) == expected_sent


def test_monthly_reports_skip_unconfirmed_accounts(monkeypatch):
    import core.db as coredb
    import services.email_service as es
    import services.weekly_report_service as wr
    biz = {"id": 5, "name": "Shop", "owner_email": "owner@example.com", "owner_username": "shop", "features_json": {}}
    monkeypatch.setattr(coredb, "supabase", _Fixed([biz]))
    mails = []
    monkeypatch.setattr(es, "_send", lambda **kw: mails.append(kw) or True)
    monkeypatch.setattr(wr, "_get_weekly_stats", lambda *a, **k: {})
    monkeypatch.setattr(wr, "_build_report_html", lambda *a, **k: "<p>r</p>")
    monkeypatch.setattr(ev, "is_restricted", lambda b: True)
    assert wr.send_weekly_reports() == {"sent": 0, "skipped": 1, "errors": 0}
    monkeypatch.setattr(ev, "is_restricted", lambda b: False)
    out = wr.send_weekly_reports()
    assert out["sent"] == 1 and len(mails) == 1


def test_scheduled_campaign_runner_leaves_unconfirmed_accounts_pending(client, monkeypatch):
    import core.db as coredb
    import routes.growth_routes as gr
    monkeypatch.setenv("CRON_SECRET", "s3cret")
    camp = {"id": 1, "business_id": 5, "audience": "all", "message": "hello there"}
    monkeypatch.setattr(coredb, "supabase", _Fixed([camp]))
    monkeypatch.setattr(gr, "_fail_stalled_campaigns", lambda s: 0)
    claims = []
    monkeypatch.setattr(gr, "_claim_scheduled_campaign", lambda s, i: claims.append(i) or True)
    monkeypatch.setattr(ev, "is_restricted", lambda b: True)
    r = client.post("/campaigns/scheduled/run", headers={"x-cron-secret": "s3cret"})
    assert r.status_code == 200 and r.json()["skipped_unverified_email"] == 1 and claims == []


# -- email change ------------------------------------------------------------------------------
def _email_change_setup(client, db, authed, monkeypatch, **biz_fields):
    import crud
    db.tables["businesses"][0].update(biz_fields)
    monkeypatch.setattr(crud, "get_business_by_id", lambda b: dict(db.tables["businesses"][0]))
    monkeypatch.setattr(crud, "update_business", lambda b, d: {**db.tables["businesses"][0], **d.dict()})
    calls = {"flagged": [], "sent": [], "order": []}
    monkeypatch.setattr(ev, "columns_ready", lambda: True)
    monkeypatch.setattr(ev, "mark_required", lambda b: calls["order"].append("flag") or calls["flagged"].append(b) or True)
    monkeypatch.setattr(ev, "send_verification", lambda *a, **k: calls["sent"].append(a) or True)
    return calls


def _fresh_bid_hdr(db, **fields):
    bid = 800000 + time.time_ns() % 100000
    db.tables["businesses"][0]["id"] = bid
    return bid, _hdr(bid=bid)


def test_a_verified_account_changing_email_must_confirm_the_new_address(client, db, authed, monkeypatch):
    calls = _email_change_setup(client, db, authed, monkeypatch, email_verified_at="2026-11-01T00:00:00+00:00")
    bid, h = _fresh_bid_hdr(db)
    r = client.patch("/me", json={"owner_email": "Other@Example.com"}, headers=h)
    assert r.status_code == 200 and calls["flagged"] == [bid] and calls["sent"][0][:2] == (bid, "other@example.com")


def test_the_account_is_flagged_before_the_new_address_is_saved(client, db, authed, monkeypatch):
    import crud
    calls = _email_change_setup(client, db, authed, monkeypatch, email_verified_at="2026-11-01T00:00:00+00:00")
    monkeypatch.setattr(crud, "update_business", lambda b, d: calls["order"].append("save") or {**db.tables["businesses"][0], **d.dict()})
    bid, h = _fresh_bid_hdr(db)
    client.patch("/me", json={"owner_email": "other@example.com"}, headers=h)
    assert calls["order"] == ["flag", "save"]


def test_if_flagging_fails_the_email_is_not_changed(client, db, authed, monkeypatch):
    import crud
    calls = _email_change_setup(client, db, authed, monkeypatch, email_verified_at="2026-11-01T00:00:00+00:00")
    monkeypatch.setattr(ev, "mark_required", lambda b: False)
    saved = []
    monkeypatch.setattr(crud, "update_business", lambda b, d: saved.append(d.dict()) or {})
    bid, h = _fresh_bid_hdr(db)
    r = client.patch("/me", json={"owner_email": "other@example.com"}, headers=h)
    assert r.status_code == 503 and saved == [] and calls["sent"] == []


def test_a_grandfathered_account_is_only_sent_a_link_and_not_restricted(client, db, authed, monkeypatch):
    calls = _email_change_setup(client, db, authed, monkeypatch, email_verified_at=None, email_verification_required=False)
    bid, h = _fresh_bid_hdr(db)
    r = client.patch("/me", json={"owner_email": "other@example.com"}, headers=h)
    assert r.status_code == 200 and calls["flagged"] == [] and len(calls["sent"]) == 1


def test_resaving_the_same_email_does_not_reset_verification(client, db, authed, monkeypatch):
    calls = _email_change_setup(client, db, authed, monkeypatch, email_verified_at="2026-11-01T00:00:00+00:00")
    bid, h = _fresh_bid_hdr(db)
    r = client.patch("/me", json={"owner_email": " OWNER@example.com "}, headers=h)
    assert r.status_code == 200 and calls["flagged"] == [] and calls["sent"] == []


def test_email_change_without_the_migration_is_unchanged(client, db, authed, monkeypatch):
    calls = _email_change_setup(client, db, authed, monkeypatch)
    monkeypatch.setattr(ev, "columns_ready", lambda: False)
    bid, h = _fresh_bid_hdr(db)
    r = client.patch("/me", json={"owner_email": "other@example.com"}, headers=h)
    assert r.status_code == 200 and calls["flagged"] == [] and calls["sent"] == []


def test_email_changes_are_rate_limited(client, db, authed, monkeypatch):
    calls = _email_change_setup(client, db, authed, monkeypatch, email_verified_at="2026-11-01T00:00:00+00:00")
    bid, h = _fresh_bid_hdr(db)
    codes = [client.patch("/me", json={"owner_email": f"n{i}@example.com"}, headers=h).status_code for i in range(5)]
    assert codes[:3] == [200, 200, 200] and codes[3] == 429


def test_without_the_migration_email_changes_are_not_rate_limited(client, db, authed, monkeypatch):
    calls = _email_change_setup(client, db, authed, monkeypatch)
    monkeypatch.setattr(ev, "columns_ready", lambda: False)
    bid, h = _fresh_bid_hdr(db)
    codes = [client.patch("/me", json={"owner_email": f"m{i}@example.com"}, headers=h).status_code for i in range(6)]
    assert codes == [200] * 6


def test_a_race_with_an_email_change_cannot_verify_the_new_address(db, monkeypatch):
    raw = ev.issue_token(5, "owner@example.com")
    orig = _Q.execute

    def racing(self):
        if self.op == "update" and self.name == "businesses" and self.payload.get("email_verified_at"):
            self.db.tables["businesses"][0]["owner_email"] = "new@example.com"     # changed just before the write
        return orig(self)
    monkeypatch.setattr(_Q, "execute", racing)
    assert ev.verify_token(raw)["ok"] is False
    assert db.tables["businesses"][0]["email_verified_at"] is None


# -- frontend ----------------------------------------------------------------------------------
def test_verify_page_uses_text_content_and_requires_a_click():
    import os
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "static"))
    js = open(os.path.join(root, "verify-email.js"), encoding="utf8").read()
    html = open(os.path.join(root, "verify-email.html"), encoding="utf8").read()
    assert "innerHTML" not in js and "textContent" in js
    assert js.index("addEventListener('click'") < js.index("/auth/verify-email")      # token sent only on click
    assert "no-referrer" in html and "<script src=" in html
    board = open(os.path.join(root, "dashboard.js"), encoding="utf8").read()
    assert "/auth/verification-status" in board and "/auth/resend-verification" in board
    assert "email-verify-banner" in open(os.path.join(root, "dashboard.html"), encoding="utf8").read()
