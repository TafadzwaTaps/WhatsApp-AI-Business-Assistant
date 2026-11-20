"""Cookie sessions, Stage 1: server side, behind COOKIE_AUTH_ENABLED (OFF by default)."""
import asyncio
import time
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from jose import jwt

import core.auth as auth
import core.cookie_auth as ck
import routes.auth_routes as ar
import routes.webhook_routes as wr
import services.email_verification_service as ev


# -- helpers -----------------------------------------------------------------------------------
class _RevDb:
    def __init__(self): self.rows = []
    def table(self, name): return _RevQ(self)


class _RevQ:
    def __init__(self, db): self.db, self._ins, self._flt = db, None, []
    def insert(self, row): self._ins = row; return self
    def select(self, *a, **k): return self
    def eq(self, k, v): self._flt.append((k, v)); return self
    def lt(self, *a): return self
    def delete(self): return self
    def limit(self, *a): return self
    def execute(self):
        class R: pass
        r = R()
        if self._ins is not None:
            self.db.rows.append(self._ins); r.data = [self._ins]
        else:
            r.data = [x for x in self.db.rows if all(x.get(k) == v for k, v in self._flt)]
        return r


@pytest.fixture
def client():
    import main
    return TestClient(main.app)


@pytest.fixture(autouse=True)
def _isolated_rate_limits(monkeypatch):
    """These tests make many calls from one test address: keep them from using up shared in-memory
    limiter buckets that other test modules rely on (the limits themselves are tested elsewhere)."""
    monkeypatch.setattr(ar, "_rate_limit", lambda *a, **k: None)
    monkeypatch.setattr(ar, "_rate_check", lambda *a, **k: None)
    monkeypatch.setattr(ar, "check_ip_login_limit", lambda *a, **k: None)


@pytest.fixture
def revdb(monkeypatch):
    import core.db as coredb
    db = _RevDb()
    monkeypatch.setattr(coredb, "supabase", db)
    auth._revoked_local.clear(); auth._revoked_checked.clear()
    ar._ROTATED.clear()
    return db


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv("COOKIE_AUTH_ENABLED", "1")


@pytest.fixture
def world(monkeypatch):
    """Authentication state: no password change, never suspended; accounts read from a dict."""
    import crud
    monkeypatch.setattr(auth, "_get_current_password_changed_at", lambda b: None)
    monkeypatch.setattr(auth, "_lookup_auth_state", lambda b: None)
    monkeypatch.setattr(ev, "columns_ready", lambda: False)
    monkeypatch.setattr(crud, "get_business_by_id", lambda b: {"id": b, "name": "Shop", "owner_email": "o@example.com"})


BID = 70001
HOST = {"host": "testserver"}


def _login_body(name):
    return {"username": name, "password": "correct horse battery staple"}


@pytest.fixture
def account(monkeypatch):
    uname = f"cookie{time.time_ns()}"
    hashed = auth.hash_password("correct horse battery staple")
    biz = {"id": BID, "name": "Shop", "owner_username": uname, "is_active": True, "owner_password": hashed,
           "password_changed_at": None}
    monkeypatch.setattr(ar.crud, "get_business_by_username", lambda u: biz if u == uname else None)
    return uname, biz


def _cookies(resp):
    out = {}
    for h in resp.headers.get_list("set-cookie"):
        name, _, rest = h.partition("=")
        value, _, attrs = rest.partition(";")
        out[name] = {"value": value, "attrs": [a.strip().lower() for a in attrs.split(";") if a.strip()]}
    return out


def _tokens(sub="shop", bid=BID, role="business", **extra):
    d = {"sub": sub, "role": role, "business_id": bid, **extra}
    return auth.create_access_token(d), auth.create_refresh_token(d)


def _cookie_header(access=None, refresh=None, csrf=None):
    parts = []
    if access: parts.append(f"{ck.ACCESS_COOKIE}={access}")
    if refresh: parts.append(f"{ck.REFRESH_COOKIE}={refresh}")
    if csrf: parts.append(f"{ck.CSRF_COOKIE}={csrf}")
    return {"cookie": "; ".join(parts)}


def _csrf(sub="shop", bid=BID, role="business"):
    return ck.csrf_token_for(sub, bid, role)


def _post_headers(access, csrf=None, **extra):
    h = {**_cookie_header(access=access, csrf=csrf), **extra}
    if csrf:
        h["x-csrf-token"] = csrf
    return h


# ── the flag is OFF and changes nothing ───────────────────────────────────────────────────────
def test_the_flag_is_off_by_default(monkeypatch):
    monkeypatch.delenv("COOKIE_AUTH_ENABLED", raising=False)
    assert ck.enabled() is False
    for v in ("", "0", "false", "no", "off", "maybe"):
        monkeypatch.setenv("COOKIE_AUTH_ENABLED", v)
        assert ck.enabled() is False
    for v in ("1", "true", "YES", "On"):
        monkeypatch.setenv("COOKIE_AUTH_ENABLED", v)
        assert ck.enabled() is True


def test_no_deployment_file_or_default_turns_it_on():
    import os
    root = os.path.join(os.path.dirname(__file__), "..", "..")
    for dirpath, _, files in os.walk(root):
        if any(x in dirpath for x in (".git", "node_modules", "__pycache__", "tests", "docs", ".venv")):
            continue
        for f in files:
            if f.endswith((".yaml", ".yml", ".toml", ".env", ".json", ".sh", ".txt")) and "lock" not in f:
                try:
                    assert "COOKIE_AUTH_ENABLED" not in open(os.path.join(dirpath, f), encoding="utf8", errors="ignore").read(), f
                except OSError:
                    pass


def test_flag_off_login_ignores_the_opt_in_header(client, monkeypatch, account, revdb):
    monkeypatch.delenv("COOKIE_AUTH_ENABLED", raising=False)
    r = client.post("/auth/login", json=_login_body(account[0]), headers={"x-auth-mode": "cookie"})
    assert r.status_code == 200 and r.json()["access_token"] and r.json()["refresh_token"]
    assert r.headers.get_list("set-cookie") == []


def test_flag_off_cookies_are_never_read(client, monkeypatch, world, revdb):
    monkeypatch.delenv("COOKIE_AUTH_ENABLED", raising=False)
    access, refresh = _tokens()
    assert client.get("/auth/verification-status", headers=_cookie_header(access=access)).status_code == 401
    assert client.post("/auth/refresh", json={}, headers=_cookie_header(refresh=refresh)).status_code == 422


def test_session_mode_endpoint_reports_the_flag(client, monkeypatch):
    monkeypatch.delenv("COOKIE_AUTH_ENABLED", raising=False)
    assert client.get("/auth/session-mode").json() == {"cookie_auth": False}
    monkeypatch.setenv("COOKIE_AUTH_ENABLED", "1")
    assert client.get("/auth/session-mode").json() == {"cookie_auth": True}


# ── login, flags and body ─────────────────────────────────────────────────────────────────────
def test_opted_in_login_sets_three_cookies_with_the_approved_flags(client, on, account, revdb):
    r = client.post("/auth/login", json=_login_body(account[0]), headers={"x-auth-mode": "cookie"})
    assert r.status_code == 200
    c = _cookies(r)
    assert set(c) == {ck.ACCESS_COOKIE, ck.REFRESH_COOKIE, ck.CSRF_COOKIE}
    at, rt, cs = c[ck.ACCESS_COOKIE]["attrs"], c[ck.REFRESH_COOKIE]["attrs"], c[ck.CSRF_COOKIE]["attrs"]
    for attrs in (at, rt, cs):
        assert "secure" in attrs and "samesite=strict" in attrs and "samesite=none" not in attrs and "samesite=lax" not in attrs
        assert not any(a.startswith("domain=") for a in attrs)          # __Host- rules: no Domain
    assert "httponly" in at and "path=/" in at and "max-age=3600" in at
    assert "httponly" in rt and "path=/auth" in rt and "max-age=604800" in rt
    assert "httponly" not in cs and "path=/" in cs                        # the page must read the CSRF token
    assert r.headers["cache-control"] == "no-store"


def test_refresh_and_access_tokens_are_not_exposed_to_javascript(client, on, account, revdb):
    r = client.post("/auth/login", json=_login_body(account[0]), headers={"x-auth-mode": "cookie"})
    c = _cookies(r)
    body = r.json()
    assert "access_token" not in body and "refresh_token" not in body and body["auth_mode"] == "cookie"
    assert body["role"] == "business" and body["business_id"] == BID
    refresh, access, csrf = c[ck.REFRESH_COOKIE]["value"], c[ck.ACCESS_COOKIE]["value"], c[ck.CSRF_COOKIE]["value"]
    assert refresh not in r.text and access not in r.text
    assert csrf not in (access, refresh) and csrf not in access and csrf not in refresh      # readable cookie holds no token
    assert auth.decode_token(refresh)["type"] == "refresh" and auth.decode_token(access)["type"] == "access"


def test_clients_without_the_header_are_untouched_even_when_enabled(client, on, account, revdb):
    r = client.post("/auth/login", json=_login_body(account[0]))            # the Flutter app's request shape
    assert r.status_code == 200 and r.headers.get_list("set-cookie") == []
    assert r.json()["access_token"] and r.json()["refresh_token"] and "auth_mode" not in r.json()


def test_bearer_requests_work_exactly_as_before_when_enabled(client, on, world, revdb):
    access, _ = _tokens()
    r = client.get("/auth/verification-status", headers={"Authorization": "Bearer " + access})
    assert r.status_code == 200


def test_csrf_token_is_per_account_and_unforgeable_without_the_key():
    assert _csrf("a", 1) != _csrf("b", 1) and _csrf("a", 1) != _csrf("a", 2)
    assert _csrf("a", 1) == _csrf("a", 1) and len(_csrf()) == 48


# ── cookie-authenticated requests ─────────────────────────────────────────────────────────────
def test_cookie_authenticates_reads_without_csrf(client, on, world, revdb):
    access, _ = _tokens()
    assert client.get("/auth/verification-status", headers=_cookie_header(access=access)).status_code == 200


def test_unauthenticated_requests_still_get_the_original_401(client, on, revdb):
    r = client.get("/auth/verification-status")
    assert r.status_code == 401 and r.json() == {"detail": "Not authenticated"} and "bearer" in r.headers["www-authenticate"].lower()


@pytest.mark.parametrize("name,csrf_cookie,csrf_header,extra,status", [
    ("no csrf at all", None, None, {"origin": "http://testserver"}, 403),
    ("header without cookie", None, "H", {"origin": "http://testserver"}, 403),
    ("cookie without header", "C", None, {"origin": "http://testserver"}, 403),
    ("wrong header", "C", "wrong", {"origin": "http://testserver"}, 403),
    ("matching but forged pair", "forged", "forged", {"origin": "http://testserver"}, 403),
    ("correct, same origin", "C", "C", {"origin": "http://testserver"}, 200),
    ("correct, foreign origin", "C", "C", {"origin": "https://evil.example"}, 403),
    ("correct, null origin", "C", "C", {"origin": "null"}, 403),
    ("correct, no origin and no fetch metadata", "C", "C", {}, 403),
    ("correct, fetch metadata same-origin", "C", "C", {"sec-fetch-site": "same-origin"}, 200),
    ("correct, fetch metadata cross-site", "C", "C", {"sec-fetch-site": "cross-site", "origin": "http://testserver"}, 403),
    ("correct, fetch metadata same-site (sibling subdomain)", "C", "C", {"sec-fetch-site": "same-site", "origin": "http://testserver"}, 403),
])
def test_csrf_matrix_for_cookie_authenticated_writes(client, on, world, revdb, name, csrf_cookie, csrf_header, extra, status):
    bid = 900000 + time.time_ns() % 100000                      # fresh resend rate-limit bucket
    access, _ = _tokens(bid=bid)
    good = _csrf(bid=bid)
    cookie_val = good if csrf_cookie == "C" else csrf_cookie
    header_val = good if csrf_header == "C" else csrf_header
    headers = {**_cookie_header(access=access, csrf=cookie_val), **extra}
    if csrf_header == "H":
        header_val = good
    if header_val:
        headers["x-csrf-token"] = header_val
    r = client.post("/auth/resend-verification", headers=headers)
    assert r.status_code == status, name


def test_bearer_writes_are_exempt_from_csrf(client, on, world, revdb):
    bid = 910000 + time.time_ns() % 100000
    access, _ = _tokens(bid=bid)
    assert client.post("/auth/resend-verification", headers={"Authorization": "Bearer " + access}).status_code == 200


def test_bearer_wins_when_both_are_present_and_needs_no_csrf(client, on, world, revdb):
    bid = 920000 + time.time_ns() % 100000
    mine, _ = _tokens(bid=bid)
    theirs, _ = _tokens(sub="other", bid=bid + 1)
    h = {"Authorization": "Bearer " + mine, **_cookie_header(access=theirs)}
    r = client.post("/auth/resend-verification", headers=h)
    assert r.status_code == 200


def test_impersonation_tokens_are_never_accepted_from_a_cookie(client, on, world, revdb):
    access, _ = _tokens(impersonated_by="root")
    assert client.get("/auth/verification-status", headers=_cookie_header(access=access)).status_code == 401


def test_a_refresh_token_in_the_access_cookie_is_rejected(client, on, world, revdb):
    _, refresh = _tokens()
    assert client.get("/auth/verification-status", headers=_cookie_header(access=refresh)).status_code == 401


def test_expired_cookie_tokens_are_rejected(client, on, world, revdb):
    expired = jwt.encode({"sub": "shop", "role": "business", "business_id": BID, "type": "access", "jti": "x",
                          "exp": datetime.utcnow() - timedelta(minutes=1)}, auth.SECRET_KEY, algorithm=auth.ALGORITHM)
    assert client.get("/auth/verification-status", headers=_cookie_header(access=expired)).status_code == 401


def test_revoked_cookie_tokens_are_rejected(client, on, world, revdb):
    access, _ = _tokens()
    assert client.get("/auth/verification-status", headers=_cookie_header(access=access)).status_code == 200
    assert auth.revoke_token(access) is True
    assert client.get("/auth/verification-status", headers=_cookie_header(access=access)).status_code == 401


def test_a_password_change_invalidates_cookie_sessions(client, on, world, revdb, monkeypatch):
    access, _ = _tokens(pwd_ts="T1")
    assert client.get("/auth/verification-status", headers=_cookie_header(access=access)).status_code == 200
    monkeypatch.setattr(auth, "_get_current_password_changed_at", lambda b: "T2")
    assert client.get("/auth/verification-status", headers=_cookie_header(access=access)).status_code == 401


# ── refresh ───────────────────────────────────────────────────────────────────────────────────
def _refresh_world(monkeypatch, pwd_changed=None):
    monkeypatch.setattr(ar.crud, "get_business_by_username",
                        lambda u: {"id": BID, "owner_username": u, "is_active": True, "password_changed_at": pwd_changed})


def test_refresh_by_cookie_needs_csrf(client, on, revdb, monkeypatch):
    _refresh_world(monkeypatch)
    _, refresh = _tokens()
    r = client.post("/auth/refresh", json={}, headers={**_cookie_header(refresh=refresh), "origin": "http://testserver"})
    assert r.status_code == 403
    r = client.post("/auth/refresh", json={}, headers={**_cookie_header(refresh=refresh, csrf="x"), "x-csrf-token": "x",
                                                        "origin": "http://testserver"})
    assert r.status_code == 403
    r = client.post("/auth/refresh", json={}, headers={**_cookie_header(refresh=refresh, csrf=_csrf()), "x-csrf-token": _csrf(),
                                                        "origin": "https://evil.example"})
    assert r.status_code == 403


def test_refresh_by_cookie_rotates_cookies_and_returns_no_tokens(client, on, revdb, monkeypatch):
    _refresh_world(monkeypatch)
    _, refresh = _tokens()
    h = {**_cookie_header(refresh=refresh, csrf=_csrf()), "x-csrf-token": _csrf(), "origin": "http://testserver"}
    r = client.post("/auth/refresh", json={}, headers=h)
    assert r.status_code == 200
    body = r.json()
    assert "access_token" not in body and "refresh_token" not in body and body["auth_mode"] == "cookie"
    c = _cookies(r)
    assert set(c) == {ck.ACCESS_COOKIE, ck.REFRESH_COOKIE, ck.CSRF_COOKIE}
    assert c[ck.REFRESH_COOKIE]["value"] != refresh and "httponly" in c[ck.REFRESH_COOKIE]["attrs"]


def test_the_reuse_tripwire_still_covers_cookie_refresh(client, on, revdb, monkeypatch):
    _refresh_world(monkeypatch)
    _, refresh = _tokens()
    h = {**_cookie_header(refresh=refresh, csrf=_csrf()), "x-csrf-token": _csrf(), "origin": "http://testserver"}
    assert client.post("/auth/refresh", json={}, headers=h).status_code == 200
    jti = auth.decode_token(refresh)["jti"]
    ar._ROTATED[jti] = time.time() - ar._ROTATE_GRACE_SECONDS - 5
    assert client.post("/auth/refresh", json={}, headers=h).status_code == 401
    assert any(r["jti"] == jti for r in revdb.rows)


def test_refresh_cookie_cannot_be_used_for_a_password_changed_account(client, on, revdb, monkeypatch):
    _refresh_world(monkeypatch, pwd_changed="T2")
    _, refresh = _tokens(pwd_ts="T1")
    h = {**_cookie_header(refresh=refresh, csrf=_csrf()), "x-csrf-token": _csrf(), "origin": "http://testserver"}
    assert client.post("/auth/refresh", json={}, headers=h).status_code == 401


def test_refresh_with_a_body_token_is_unchanged_for_mobile_clients(client, on, revdb, monkeypatch):
    _refresh_world(monkeypatch)
    _, refresh = _tokens()
    r = client.post("/auth/refresh", json={"refresh_token": refresh})
    assert r.status_code == 200 and r.json()["access_token"] and r.json()["refresh_token"]
    assert r.headers.get_list("set-cookie") == []


def test_refresh_without_any_token_is_rejected(client, on, revdb):
    assert client.post("/auth/refresh", json={}).status_code == 422


# ── logout ────────────────────────────────────────────────────────────────────────────────────
def test_logout_revokes_the_cookie_tokens_and_clears_the_cookies(client, on, world, revdb):
    access, refresh = _tokens()
    r = client.post("/auth/logout", json={}, headers={**_cookie_header(access=access, refresh=refresh, csrf=_csrf()),
                                                      "origin": "http://testserver"})
    assert r.status_code == 200 and r.json()["revoked"] == 2
    cleared = _cookies(r)
    assert set(cleared) == {ck.ACCESS_COOKIE, ck.REFRESH_COOKIE, ck.CSRF_COOKIE}
    assert all(any(a == "max-age=0" for a in v["attrs"]) for v in cleared.values())
    assert {row["jti"] for row in revdb.rows} == {auth.decode_token(access)["jti"], auth.decode_token(refresh)["jti"]}
    assert client.get("/auth/verification-status", headers=_cookie_header(access=access)).status_code == 401


def test_logout_without_cookies_or_flag_behaves_as_before(client, monkeypatch, world, revdb):
    monkeypatch.delenv("COOKIE_AUTH_ENABLED", raising=False)
    access, refresh = _tokens()
    r = client.post("/auth/logout", json={"refresh_token": refresh}, headers={"Authorization": "Bearer " + access})
    assert r.status_code == 200 and r.json()["revoked"] == 2 and r.headers.get_list("set-cookie") == []


# ── CORS and credentials ──────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("origin", ["https://wazibothq.com", "https://evil.example"])
def test_cors_never_allows_credentials(client, on, origin):
    r = client.options("/auth/login", headers={"origin": origin, "access-control-request-method": "POST"})
    assert "access-control-allow-credentials" not in {k.lower() for k in r.headers}
    r2 = client.get("/healthz", headers={"origin": origin})
    assert "access-control-allow-credentials" not in {k.lower() for k in r2.headers}


# ── WebSocket ─────────────────────────────────────────────────────────────────────────────────
class _WS:
    def __init__(self, headers=None, cookies=None, query=None):
        self.headers, self.cookies, self.query_params = headers or {}, cookies or {}, query or {}
        self.closed, self.accepted = None, False
    async def accept(self): self.accepted = True
    async def close(self, code=1000): self.closed = code
    async def receive_text(self): raise RuntimeError("no frame")


class _Mgr:
    def __init__(self): self.connected = False
    def count(self, _b): return 0
    async def connect(self, ws, b, accepted=False):
        self.connected = True
        raise RuntimeError("stop")
    def disconnect(self, *_): pass


def _run_ws(monkeypatch, ws, bid=BID):
    mgr = _Mgr()
    monkeypatch.setattr(wr, "manager", mgr)
    monkeypatch.setattr(auth, "_get_current_password_changed_at", lambda b: None)
    monkeypatch.setattr(auth, "_lookup_auth_state", lambda b: None)
    try:
        asyncio.run(wr.websocket_chat(ws, bid))
    except RuntimeError:
        pass
    return mgr


def test_websocket_accepts_the_access_cookie_from_an_allowed_origin(on, revdb, monkeypatch):
    access, _ = _tokens()
    ws = _WS(headers={"origin": "https://wazibothq.com", "host": "wazibothq.com"}, cookies={ck.ACCESS_COOKIE: access})
    assert _run_ws(monkeypatch, ws).connected is True


def test_websocket_ignores_the_cookie_without_an_origin(on, revdb, monkeypatch):
    access, _ = _tokens()
    ws = _WS(headers={"host": "wazibothq.com"}, cookies={ck.ACCESS_COOKIE: access})
    mgr = _run_ws(monkeypatch, ws)
    assert mgr.connected is False and ws.closed == 4001


def test_websocket_refuses_a_foreign_origin_even_with_a_cookie(on, revdb, monkeypatch):
    access, _ = _tokens()
    ws = _WS(headers={"origin": "https://evil.example", "host": "wazibothq.com"}, cookies={ck.ACCESS_COOKIE: access})
    mgr = _run_ws(monkeypatch, ws)
    assert mgr.connected is False and ws.closed == 4003


def test_websocket_ignores_cookies_when_the_flag_is_off(revdb, monkeypatch):
    monkeypatch.delenv("COOKIE_AUTH_ENABLED", raising=False)
    access, _ = _tokens()
    ws = _WS(headers={"origin": "https://wazibothq.com", "host": "wazibothq.com"}, cookies={ck.ACCESS_COOKIE: access})
    mgr = _run_ws(monkeypatch, ws)
    assert mgr.connected is False and ws.closed == 4001


def test_websocket_rejects_a_cookie_for_another_business(on, revdb, monkeypatch):
    access, _ = _tokens(bid=BID + 5)
    ws = _WS(headers={"origin": "https://wazibothq.com", "host": "wazibothq.com"}, cookies={ck.ACCESS_COOKIE: access})
    assert _run_ws(monkeypatch, ws, bid=BID).connected is False


# ── review fixes ──────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("sfs,status", [("same-site", 403), ("cross-site", 403), ("same-origin", 200), ("none", 200), (None, 200)])
def test_same_site_and_cross_site_fetches_are_refused_for_cookie_reads_too(client, on, world, revdb, sfs, status):
    access, _ = _tokens()
    h = _cookie_header(access=access)
    if sfs:
        h["sec-fetch-site"] = sfs
    assert client.get("/auth/verification-status", headers=h).status_code == status


def test_the_site_check_does_not_apply_to_bearer_requests(client, on, world, revdb):
    access, _ = _tokens()
    r = client.get("/auth/verification-status", headers={"Authorization": "Bearer " + access, "sec-fetch-site": "cross-site"})
    assert r.status_code == 200


def test_logout_only_clears_cookies_when_one_was_sent_from_the_same_origin(client, on, world, revdb):
    access, refresh = _tokens()
    plain = client.post("/auth/logout", json={}, headers={"origin": "http://testserver"})        # no cookie presented
    assert plain.status_code == 200 and plain.headers.get_list("set-cookie") == []
    cross = client.post("/auth/logout", json={}, headers={**_cookie_header(access=access, refresh=refresh),
                                                          "origin": "https://evil.example"})      # forged cross-site form
    assert cross.headers.get_list("set-cookie") == []
    ok = client.post("/auth/logout", json={}, headers={**_cookie_header(access=access, refresh=refresh), "sec-fetch-site": "same-origin"})
    assert len(ok.headers.get_list("set-cookie")) == 3


def test_superadmin_never_gets_or_uses_a_cookie_session(client, on, world, revdb, monkeypatch):
    access, refresh = _tokens(sub="root", bid=None, role="superadmin")
    assert client.get("/auth/verification-status", headers=_cookie_header(access=access)).status_code == 401
    h = {**_cookie_header(refresh=refresh, csrf=_csrf("root", None, "superadmin")),
         "x-csrf-token": _csrf("root", None, "superadmin"), "origin": "http://testserver"}
    assert client.post("/auth/refresh", json={}, headers=h).status_code == 403
    monkeypatch.setattr(ar, "SUPER_ADMIN_LOGIN_DISABLED", False)
    monkeypatch.setattr(ar, "SUPER_ADMIN_PASSWORD", auth.hash_password("correct horse battery staple"))
    r = client.post("/auth/login", json={"username": ar.SUPER_ADMIN_USERNAME, "password": "correct horse battery staple"},
                    headers={"x-auth-mode": "cookie"})
    assert r.status_code == 200 and r.headers.get_list("set-cookie") == [] and r.json()["access_token"]


def test_websocket_rejects_a_support_or_admin_cookie(on, revdb, monkeypatch):
    for kw in ({"impersonated_by": "root"}, {"role": "superadmin"}):
        access, _ = _tokens(**kw)
        ws = _WS(headers={"origin": "https://wazibothq.com", "host": "wazibothq.com"}, cookies={ck.ACCESS_COOKIE: access})
        assert _run_ws(monkeypatch, ws).connected is False


def test_the_csrf_value_uses_a_derived_key_not_the_raw_signing_key():
    import hashlib, hmac
    raw = hmac.new(auth.SECRET_KEY.encode(), b"csrf|shop|70001|business", hashlib.sha256).hexdigest()[:48]
    assert _csrf() != raw


def test_flag_off_refresh_without_a_token_keeps_the_original_validation_shape(client, monkeypatch, revdb):
    monkeypatch.delenv("COOKIE_AUTH_ENABLED", raising=False)
    r = client.post("/auth/refresh", json={})
    d = r.json()["detail"]
    assert r.status_code == 422 and isinstance(d, list) and d[0]["loc"] == ["body", "refresh_token"] and d[0]["type"] == "missing"


def test_behind_a_host_rewriting_proxy_writes_fail_closed_until_the_origin_is_allowed(client, on, world, revdb, monkeypatch):
    bid = 930000 + time.time_ns() % 100000
    access, _ = _tokens(bid=bid)
    h = {**_cookie_header(access=access, csrf=_csrf(bid=bid)), "x-csrf-token": _csrf(bid=bid), "origin": "https://wazibothq.com"}
    assert client.post("/auth/resend-verification", headers=h).status_code == 403          # Host is "testserver"
    monkeypatch.setenv("COOKIE_AUTH_ALLOWED_ORIGINS", "https://wazibothq.com")
    assert client.post("/auth/resend-verification", headers=h).status_code == 200
