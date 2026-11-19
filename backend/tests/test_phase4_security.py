"""Phase 4: session / authentication hardening, upload and debug-route limits, URL safety."""
import asyncio
import json
import os
import re
import subprocess
import time

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import core.auth as auth
import routes.auth_routes as ar
import routes.business_routes as br
import routes.webhook_routes as wr

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
STATIC = os.path.join(ROOT, "static")


@pytest.fixture
def client():
    import main
    return TestClient(main.app)


class _RevDb:
    """Minimal revoked_tokens table so revoke_token / is_token_revoked work."""
    def __init__(self): self.rows = []
    def table(self, name): return _RevQ(self, name)


class _RevQ:
    def __init__(self, db, name): self.db, self.name, self._ins, self._flt = db, name, None, []
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
def revdb(monkeypatch):
    import core.db as coredb
    db = _RevDb()
    monkeypatch.setattr(coredb, "supabase", db)
    auth._revoked_local.clear(); auth._revoked_checked.clear()
    ar._ROTATED.clear()
    return db


def _biz_state(monkeypatch, pwd_changed_at):
    monkeypatch.setattr(auth, "_get_current_password_changed_at", lambda b: pwd_changed_at)
    monkeypatch.setattr(auth, "_lookup_auth_state", lambda b: None)


# -- M1: a token with no pwd_ts is stale once a password change exists --------------------------
def test_business_token_without_pwd_ts_is_rejected_after_a_password_change(monkeypatch, revdb):
    _biz_state(monkeypatch, "2026-11-01T00:00:00+00:00")
    t = auth.create_access_token({"sub": "u", "role": "business", "business_id": 5})
    with pytest.raises(HTTPException) as e:
        auth.get_current_user(t)
    assert e.value.status_code == 401


def test_business_token_without_pwd_ts_is_fine_when_the_password_was_never_changed(monkeypatch, revdb):
    _biz_state(monkeypatch, None)
    t = auth.create_access_token({"sub": "u", "role": "business", "business_id": 5})
    assert auth.get_current_user(t)["business_id"] == 5


def test_matching_pwd_ts_still_works_and_old_one_is_rejected(monkeypatch, revdb):
    _biz_state(monkeypatch, "T2")
    ok = auth.create_access_token({"sub": "u", "role": "business", "business_id": 5, "pwd_ts": "T2"})
    old = auth.create_access_token({"sub": "u", "role": "business", "business_id": 5, "pwd_ts": "T1"})
    assert auth.get_current_user(ok)["business_id"] == 5
    with pytest.raises(HTTPException):
        auth.get_current_user(old)


def test_impersonation_token_is_not_affected(monkeypatch, revdb):
    _biz_state(monkeypatch, "T2")
    t = auth.create_access_token({"sub": "u", "role": "business", "business_id": 5, "impersonated_by": "root"})
    assert auth.get_current_user(t)["impersonated_by"] == "root"


def test_superadmin_token_is_not_affected(monkeypatch, revdb):
    _biz_state(monkeypatch, "T2")
    t = auth.create_access_token({"sub": "root", "role": "superadmin"})
    assert auth.get_current_user(t)["role"] == "superadmin"


def _refresh_setup(monkeypatch, pwd_changed_at, pwd_ts=None, role="business"):
    monkeypatch.setattr(ar.crud, "get_business_by_username",
                        lambda u: {"id": 5, "owner_username": u, "is_active": True, "password_changed_at": pwd_changed_at})
    claims = {"sub": "owner", "role": role, "business_id": 5 if role == "business" else None}
    if pwd_ts:
        claims["pwd_ts"] = pwd_ts
    return auth.create_refresh_token(claims)


def test_refresh_refuses_a_token_without_pwd_ts_after_a_password_change(client, monkeypatch, revdb):
    rt = _refresh_setup(monkeypatch, "2026-11-01T00:00:00+00:00")
    assert client.post("/auth/refresh", json={"refresh_token": rt}).status_code == 401


def test_refresh_works_without_pwd_ts_when_password_never_changed(client, monkeypatch, revdb):
    rt = _refresh_setup(monkeypatch, None)
    assert client.post("/auth/refresh", json={"refresh_token": rt}).status_code == 200


def test_refresh_with_current_pwd_ts_works(client, monkeypatch, revdb):
    rt = _refresh_setup(monkeypatch, "T9", pwd_ts="T9")
    assert client.post("/auth/refresh", json={"refresh_token": rt}).status_code == 200


# -- M2: refresh rotation with a grace window and reuse detection ------------------------------
def test_second_use_inside_the_grace_window_is_allowed(client, monkeypatch, revdb):
    rt = _refresh_setup(monkeypatch, None)
    assert client.post("/auth/refresh", json={"refresh_token": rt}).status_code == 200
    assert client.post("/auth/refresh", json={"refresh_token": rt}).status_code == 200      # second tab


def test_reuse_after_the_grace_window_is_refused_and_revoked(client, monkeypatch, revdb):
    rt = _refresh_setup(monkeypatch, None)
    assert client.post("/auth/refresh", json={"refresh_token": rt}).status_code == 200
    jti = auth.decode_token(rt)["jti"]
    ar._ROTATED[jti] = time.time() - ar._ROTATE_GRACE_SECONDS - 5
    assert client.post("/auth/refresh", json={"refresh_token": rt}).status_code == 401
    assert any(r["jti"] == jti for r in revdb.rows)                       # now dead for good
    ar._ROTATED.pop(jti, None)
    assert client.post("/auth/refresh", json={"refresh_token": rt}).status_code == 401      # revoked, even after a restart


def test_each_new_refresh_token_has_its_own_window(client, monkeypatch, revdb):
    a = _refresh_setup(monkeypatch, None)
    b = _refresh_setup(monkeypatch, None)
    client.post("/auth/refresh", json={"refresh_token": a})
    ar._ROTATED[auth.decode_token(a)["jti"]] = time.time() - 1000
    assert client.post("/auth/refresh", json={"refresh_token": b}).status_code == 200


def test_the_rotation_record_is_pruned():
    ar._ROTATED.clear()
    ar._ROTATED["old"] = time.time() - 9 * 86400
    ar._rotate_refresh_token({"jti": "new", "sub": "x"}, "tok")
    assert "old" not in ar._ROTATED and "new" in ar._ROTATED


# -- M3: lockout keys are normalised; unknown users cost the same bcrypt ------------------------
def test_lockout_checks_use_the_normalised_username(client, monkeypatch):
    seen = []
    monkeypatch.setattr(ar, "is_login_locked", lambda ip, u: seen.append(("ip_acct", u)) or False)
    monkeypatch.setattr(ar, "check_account_login_lockout", lambda u: seen.append(("acct", u)))
    monkeypatch.setattr(ar.crud, "get_business_by_username", lambda u: None)
    r = client.post("/auth/login", json={"username": "  VicTim ", "password": "x"})
    assert r.status_code == 401
    assert ("ip_acct", "victim") in seen and ("acct", "victim") in seen


def test_valid_login_with_mixed_case_and_padding_still_works_and_carries_pwd_ts(client, monkeypatch):
    biz = {"id": 5, "name": "Shop", "owner_username": "victim", "is_active": True,
           "owner_password": ar.hash_password("Passw0rd!x"), "password_changed_at": "T7"}
    monkeypatch.setattr(ar.crud, "get_business_by_username", lambda u: biz if u == "victim" else None)
    r = client.post("/auth/login", json={"username": "  VICTIM ", "password": "Passw0rd!x"})
    assert r.status_code == 200
    assert auth.decode_token(r.json()["access_token"])["pwd_ts"] == "T7"


def test_signup_style_token_without_pwd_ts_keeps_working_until_a_password_change(client, monkeypatch, revdb):
    rt = _refresh_setup(monkeypatch, None)                               # no pwd_ts in the token
    assert client.post("/auth/refresh", json={"refresh_token": rt}).status_code == 200
    rt2 = _refresh_setup(monkeypatch, "T3")                              # owner changed password since
    assert client.post("/auth/refresh", json={"refresh_token": rt2}).status_code == 401


def test_unknown_user_still_runs_a_password_verification(client, monkeypatch):
    calls = []
    monkeypatch.setattr(ar, "_dummy_verify", lambda pw: calls.append(pw))
    monkeypatch.setattr(ar.crud, "get_business_by_username", lambda u: None)
    assert client.post("/auth/login", json={"username": "nobody", "password": "pw"}).status_code == 401
    assert calls == ["pw"]


def test_dummy_verify_never_raises(monkeypatch):
    monkeypatch.setattr(ar, "hash_password", lambda p: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr(ar, "_DUMMY_HASH", None)
    ar._dummy_verify("pw")


def test_superadmin_refresh_stops_when_superadmin_login_is_disabled(client, monkeypatch, revdb):
    rt = auth.create_refresh_token({"sub": "root", "role": "superadmin"})
    monkeypatch.setattr(ar, "SUPER_ADMIN_LOGIN_DISABLED", True)
    assert client.post("/auth/refresh", json={"refresh_token": rt}).status_code == 401
    monkeypatch.setattr(ar, "SUPER_ADMIN_LOGIN_DISABLED", False)
    assert client.post("/auth/refresh", json={"refresh_token": rt}).status_code == 200


# -- WebSocket: password change / suspension -----------------------------------------------------
class _WS:
    def __init__(self, query=None):
        self.headers, self.query_params, self.closed = {}, query or {}, None
    async def accept(self): pass
    async def close(self, code=1000): self.closed = code
    async def receive_text(self): raise RuntimeError("end")


class _Mgr:
    def __init__(self): self.connected = False
    def count(self, _b): return 0
    async def connect(self, ws, b, accepted=False):
        self.connected = True
        raise RuntimeError("stop")
    def disconnect(self, *_): pass


def _ws(monkeypatch, payload, pwd_changed_at=None, active=None):
    mgr = _Mgr()
    monkeypatch.setattr(wr, "manager", mgr)
    monkeypatch.setattr(wr, "decode_token", lambda t: payload)
    monkeypatch.setattr(auth, "_get_current_password_changed_at", lambda b: pwd_changed_at)
    monkeypatch.setattr(auth, "_lookup_auth_state", lambda b: (None, active) if active is not None else None)
    ws = _WS({"token": "t"})
    try:
        asyncio.run(wr.websocket_chat(ws, 7))
    except RuntimeError:
        pass
    return ws, mgr


def test_websocket_refuses_a_token_from_before_a_password_change(monkeypatch):
    ws, mgr = _ws(monkeypatch, {"business_id": 7, "role": "business", "pwd_ts": "T1"}, "T2")
    assert ws.closed == 4001 and not mgr.connected


def test_websocket_refuses_a_token_without_pwd_ts_after_a_password_change(monkeypatch):
    ws, mgr = _ws(monkeypatch, {"business_id": 7, "role": "business"}, "T2")
    assert ws.closed == 4001 and not mgr.connected


def test_websocket_refuses_a_suspended_business(monkeypatch):
    ws, mgr = _ws(monkeypatch, {"business_id": 7, "role": "business"}, None, active=False)
    assert ws.closed == 4001 and not mgr.connected


def test_websocket_still_accepts_a_current_token(monkeypatch):
    ws, mgr = _ws(monkeypatch, {"business_id": 7, "role": "business", "pwd_ts": "T2"}, "T2", active=True)
    assert mgr.connected


def test_websocket_support_impersonation_is_exempt(monkeypatch):
    ws, mgr = _ws(monkeypatch, {"business_id": 7, "role": "business", "impersonated_by": "root"}, "T2", active=False)
    assert mgr.connected


# -- API surface ---------------------------------------------------------------------------------
def test_schema_debug_route_is_superadmin_only(client, monkeypatch, revdb):
    _biz_state(monkeypatch, None)
    biz = auth.create_access_token({"sub": "u", "role": "business", "business_id": 5})
    root = auth.create_access_token({"sub": "root", "role": "superadmin"})
    assert client.get("/debug/schema", headers={"Authorization": "Bearer " + biz}).status_code == 403
    assert client.get("/debug/schema", headers={"Authorization": "Bearer " + root}).status_code == 200


def test_webhook_debug_route_is_rate_limited_per_business(client, monkeypatch, revdb):
    import routes.admin_routes as adm
    _biz_state(monkeypatch, None)
    monkeypatch.setattr(adm.crud, "get_business_by_id", lambda b: None)
    h = {"Authorization": "Bearer " + auth.create_access_token(
        {"sub": "u", "role": "business", "business_id": time.time_ns()})}
    codes = [client.get("/debug/webhook", headers=h).status_code for _ in range(8)]
    assert codes[0] == 200 and codes[-1] == 429


def _request(ip="9.9.9.9"):
    from starlette.requests import Request
    return Request({"type": "http", "headers": [], "client": (ip, 1), "method": "POST", "path": "/"})


def test_upload_limit_is_per_business():
    from services.security import RateLimitExceeded
    a, b = time.time_ns(), time.time_ns() + 1               # fresh keys: repeatable in one process
    for _ in range(30):
        br._upload_rate_limit(_request(), a)
    with pytest.raises(RateLimitExceeded):
        br._upload_rate_limit(_request(), a)
    br._upload_rate_limit(_request(), b)                    # another business is unaffected


@pytest.mark.parametrize("fn", [br.upload_avatar, br.upload_site_logo, br.upload_product_image,
                                br.voice_transcribe, br.import_products_csv])
def test_every_upload_and_transcription_route_is_limited(fn):
    import inspect
    assert "_upload_rate_limit(" in inspect.getsource(fn)


def test_openapi_schema_is_not_served(client):
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/docs").status_code == 404


def test_csp_no_longer_allows_cdnjs(client):
    csp = client.get("/healthz").headers["content-security-policy"]
    assert "cdnjs.cloudflare.com" not in csp and "object-src 'none'" in csp


# -- frontend: URL safety (runs the real function in Node) ---------------------------------------
def _safe_url_js(expr):
    src = open(os.path.join(STATIC, "dashboard.js"), encoding="utf8").read()
    m = re.search(r"function safeUrl\(u, fallback\)\{.*?\n\}\n", src, re.S)
    assert m, "safeUrl helper missing"
    out = subprocess.run(["node", "-e", m.group(0) + "console.log(JSON.stringify(" + expr + "))"],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.mark.parametrize("url,ok", [
    ("/pricing", True), ("/inbox?phone=1", True), ("https://checkout.stripe.com/c/pay/x", True),
    ("javascript:alert(1)", False), ("JaVaScRiPt:alert(1)", False), ("data:text/html,<script>", False),
    ("//evil.example/x", False), ("/\\evil.example", False), ("http://insecure.example/x", False),
    ("", False), (None, False), ("vbscript:x", False),
])
def test_safe_url_only_allows_site_paths_and_https(url, ok):
    got = _safe_url_js(f"safeUrl({json.dumps(url)})")
    assert (got == (url or "")) if ok else (got == "")


@pytest.mark.parametrize("raw", ["/\t/evil.example", "/\n/evil.example", "/\r/evil.example", "\t//evil.example"])
def test_safe_url_cannot_be_bypassed_with_control_characters(raw):
    got = _safe_url_js(f"safeUrl({json.dumps(raw)})")
    assert got == "" or not got.replace("\t", "").replace("\n", "").replace("\r", "").startswith("//")
    assert not re.search(r"[\t\r\n]", got)


def test_product_image_bulk_uploads_have_their_own_higher_bucket():
    from services.security import RateLimitExceeded
    k = time.time_ns()
    for _ in range(31):
        br._upload_rate_limit(_request(), k, "upload_product", 120)
    with pytest.raises(RateLimitExceeded):
        for _ in range(100):
            br._upload_rate_limit(_request(), k, "upload_product", 120)


def test_safe_url_fallback():
    assert _safe_url_js("safeUrl('javascript:x', '/pricing')") == "/pricing"
    assert _safe_url_js("safeUrl(undefined, '/pricing')") == "/pricing"


def test_server_supplied_urls_go_through_safe_url():
    src = open(os.path.join(STATIC, "dashboard.js"), encoding="utf8").read()
    for bad in ("window.location.href = result.url", "window.open(result.url", "window.location.href = res.url;",
                "window.open(_mktData.whatsapp_link", "jsArg(w.endpoint)", "detail.upgrade_url ||"):
        assert bad not in src, bad
    assert "safeUrl(detail.upgrade_url" in src and "safeUrl(w.endpoint" in src
    assert "/^https:\\/\\//i.test(String(data.url))" in open(os.path.join(STATIC, "pricing.html"), encoding="utf8").read()


def test_refresh_uses_the_newest_token_from_shared_storage():
    src = open(os.path.join(STATIC, "dashboard.js"), encoding="utf8").read()
    assert "localStorage.getItem('wazi_refresh') || refreshTok" in src
