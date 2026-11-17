"""Phase 3: SSRF guard, CSP/cache headers, WebSocket hardening, frontend escaping."""
import asyncio
import json
import os
import re

import pytest
from fastapi.testclient import TestClient

import routes.business_routes as br
import routes.webhook_routes as wr

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
STATIC = os.path.join(ROOT, "static")


# ── S1: media URL guard ──────────────────────────────────────────────────────
def _public(monkeypatch):
    import socket
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("157.240.1.1", 443))])


@pytest.mark.parametrize("url", [
    "http://lookaside.fbsbx.com/x",                 # not https
    "https://evil.example.com/a.ogg",               # not a Meta host
    "https://lookaside.fbsbx.com.evil.com/a",       # suffix trick
    "https://user:pw@lookaside.fbsbx.com/a",        # credentials in URL
    "https://lookaside.fbsbx.com:8443/a",           # odd port
    "https://169.254.169.254/latest/meta-data",
    "", None,
])
def test_unsafe_media_urls_are_refused(monkeypatch, url):
    _public(monkeypatch)
    assert br._is_safe_media_url(url) is False


def test_meta_media_host_allowed_only_when_it_resolves_public(monkeypatch):
    import socket
    _public(monkeypatch)
    assert br._is_safe_media_url("https://lookaside.fbsbx.com/v/abc") is True
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("10.0.0.5", 443))])
    assert br._is_safe_media_url("https://lookaside.fbsbx.com/v/abc") is False


# ── S2/S7: headers ───────────────────────────────────────────────────────────
@pytest.fixture
def client():
    import main
    return TestClient(main.app)


def test_csp_has_new_directives_and_no_wildcard_wss(client):
    csp = client.get("/healthz").headers["content-security-policy"]
    for d in ("object-src 'none'", "base-uri 'self'", "form-action 'self'"):
        assert d in csp
    assert not re.search(r"connect-src[^;]*\bwss:(\s|;)", csp)


def test_authenticated_json_is_no_store(client):
    r = client.get("/config/public", headers={"Authorization": "Bearer x"})
    assert r.headers.get("cache-control") == "no-store"
    assert "no-store" not in (client.get("/config/public").headers.get("cache-control") or "")


# ── S3: WebSocket ────────────────────────────────────────────────────────────
class _WS:
    def __init__(self, headers=None, query=None, frames=()):
        self.headers, self.query_params = headers or {}, query or {}
        self.frames, self.closed, self.accepted = list(frames), None, False

    async def accept(self): self.accepted = True
    async def close(self, code=1000): self.closed = code
    async def receive_text(self):
        if not self.frames:
            raise RuntimeError("end")
        return self.frames.pop(0)


class _Mgr:
    def __init__(self, n=0): self.n, self.connected = n, False
    def count(self, _b): return self.n
    async def connect(self, ws, b, accepted=False):
        self.connected = True
        raise RuntimeError("stop")
    def disconnect(self, *_): pass


def _go(monkeypatch, ws, payload=None, mgr=None):
    mgr = mgr or _Mgr()
    monkeypatch.setattr(wr, "manager", mgr)
    monkeypatch.setattr(wr, "decode_token", lambda t: payload if t == "good" else (_ for _ in ()).throw(ValueError()))
    try:
        asyncio.run(wr.websocket_chat(ws, 7))
    except RuntimeError:
        pass
    return mgr


GOOD = {"business_id": 7, "role": "business"}


def test_foreign_origin_is_refused(monkeypatch):
    ws = _WS(headers={"origin": "https://evil.example", "host": "wazibothq.com"}, query={"token": "good"})
    mgr = _go(monkeypatch, ws, GOOD)
    assert ws.closed == 4003 and not mgr.connected


def test_same_origin_with_query_token_still_works(monkeypatch):
    ws = _WS(headers={"origin": "https://wazibothq.com", "host": "wazibothq.com"}, query={"token": "good"})
    assert _go(monkeypatch, ws, GOOD).connected


def test_first_message_auth(monkeypatch):
    ws = _WS(frames=[json.dumps({"type": "auth", "token": "good"})])
    assert _go(monkeypatch, ws, GOOD).connected and ws.accepted


def test_first_message_with_bad_token_or_shape_is_closed(monkeypatch):
    for frame in (json.dumps({"type": "auth", "token": "nope"}), json.dumps({"type": "ping"}), "not json"):
        ws = _WS(frames=[frame])
        mgr = _go(monkeypatch, ws, GOOD)
        assert ws.closed == 4001 and not mgr.connected


def test_connection_cap(monkeypatch):
    ws = _WS(query={"token": "good"})
    mgr = _go(monkeypatch, ws, GOOD, _Mgr(n=wr._WS_MAX_PER_BUSINESS))
    assert ws.closed == 4008 and not mgr.connected


# ── F1-F6: frontend escaping ─────────────────────────────────────────────────
def _read(name):
    return open(os.path.join(STATIC, name), encoding="utf8").read()


@pytest.mark.parametrize("name", ["dashboard.js", "inbox.js"])
def test_no_raw_error_message_inside_innerhtml(name):
    bad = [l.strip() for l in _read(name).splitlines()
           if "innerHTML" in l and re.search(r"\$\{(e|err|error|result)\.message\}", l)]
    assert not bad, bad


@pytest.mark.parametrize("name", ["dashboard.js", "inbox.js", "multidash.html"])
def test_escHtml_escapes_single_quote(name):
    src = _read(name)
    body = src[src.index("function escHtml"):][:400]
    assert "&#39;" in body


def test_first_order_banner_escapes_data():
    src = _read("dashboard.js")
    assert "${escHtml(item.name" in src and "${escHtml(first.customer_phone" in src


def test_broadcast_view_button_uses_double_quoted_handler():
    assert "onclick='filterCrmByPhoneSet" not in _read("dashboard.js")


def test_signup_success_escapes_business_name_and_username():
    src = _read("signup.html")
    assert "data.business_name +" not in src and "payload.username +" not in src


def test_marketplace_slug_encoded_and_token_not_logged():
    assert "/store/${encodeURIComponent(slug)}" in _read("marketplace.html")
    assert "tokenPreview" not in _read("dashboard.js")


# ── Narrowed Meta media allow-list (shared by dashboard + webhook downloads) ──
import services.whatsapp_service as wa


@pytest.mark.parametrize("url,ok", [
    ("https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=1", True),
    ("https://scontent.xx.fbcdn.net/v/t.ogg", True),
    ("https://mmg.whatsapp.net/d/f/abc.enc", True),
    ("https://www.facebook.com/x", False),          # broad suffixes were removed
    ("https://web.whatsapp.com/x", False),
    ("https://evil.fbsbx.com/x", False),            # only the exact lookaside host
    ("https://fbcdn.net.evil.com/x", False),
])
def test_narrowed_host_allow_list(monkeypatch, url, ok):
    _public(monkeypatch)
    assert wa.is_safe_media_url(url) is ok


def test_extra_host_via_env(monkeypatch):
    _public(monkeypatch)
    monkeypatch.setenv("META_MEDIA_EXTRA_HOSTS", "media.example-meta.net")
    assert wa.is_safe_media_url("https://media.example-meta.net/a") is True


def test_ipv4_mapped_ipv6_private_is_rejected(monkeypatch):
    import socket
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(10, 1, 6, "", ("::ffff:10.0.0.5", 443, 0, 0))])
    assert wa.is_safe_media_url("https://lookaside.fbsbx.com/a") is False


def test_webhook_download_refuses_untrusted_media_url(monkeypatch):
    """The token must not be sent to a non-Meta URL even if Graph (or a spoof) returns one."""
    sent = []

    class Resp:
        def __init__(self, data): self._d = data; self.content = b"x"
        def raise_for_status(self): pass
        def json(self): return self._d

    class Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, **k):
            sent.append(url)
            return Resp({"url": "https://attacker.example/steal", "mime_type": "image/png"})

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", Client)
    assert asyncio.run(wa.download_whatsapp_media("m1", "tok")) == (None, None)
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    assert asyncio.run(wa.transcribe_whatsapp_voice_note("m1", "tok")) is None
    assert all("attacker.example" not in u for u in sent)


def test_access_token_lifetime_is_sixty_minutes():
    import core.auth as auth
    assert auth.ACCESS_TOKEN_EXPIRE_MINUTES == 60
    assert auth.IMPERSONATION_TOKEN_EXPIRE_MINUTES <= 60


# ── innerHTML audit follow-ups ───────────────────────────────────────────────
import services.site_generator as sg
import routes.booking_routes as bk


def test_site_generator_rejects_non_hex_theme_colour():
    src = open(sg.__file__, encoding="utf8").read()
    assert 'if not (isinstance(theme, str) and re.fullmatch' in src
    # the guard sits before the colour reaches _hex_darken / _build_css
    assert src.index("isinstance(theme, str) and re.fullmatch") < src.index("theme_dark  = _hex_darken(theme, 25)")


def test_booking_accent_requires_strict_hex():
    src = open(bk.__file__, encoding="utf8").read()
    assert 're.fullmatch(r"#[0-9a-fA-F]{6}", accent)' in src


def test_onboarding_only_stores_hex_colour():
    src = open(os.path.join(ROOT, "backend", "routes", "onboarding_routes.py"), encoding="utf8").read()
    assert '_re.fullmatch(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})", theme_colour.strip())' in src


@pytest.mark.parametrize("needle", [
    "${esc(c)} (${counts[c]})",                      # marketplace category label
])
def test_marketplace_category_escaped(needle):
    assert needle in _read("marketplace.html")


def test_dashboard_remaining_sinks_escaped():
    src = _read("dashboard.js")
    assert "failed_numbers.slice(0,5).map(escHtml)" in src
    assert "escHtml(data.answer||'')" in src and "escHtml(s)}</li>" in src
    assert "escHtml(result?.error" in src
    assert "/^#[0-9a-f]{3,8}$/i.test(String(hex))" in src


def test_multidash_and_onboarding_pages_escape():
    assert "${escHtml(k)}" in _read("multidash.html") and "⚠ ${escHtml(msg)}" in _read("multidash.html")
    assert "replace(/\"/g,'&quot;')" in _read("onboarding.html") and "${esc(v)||'—'}" in _read("onboarding.html")


# ── Logout / server-side revocation ──────────────────────────────────────────
import types as _types
import core.auth as auth


class _RevDb:
    """Minimal revoked_tokens table."""
    def __init__(self, fail=False): self.rows, self.fail = [], fail
    def table(self, name):
        db = self
        class Q:
            def __init__(s): s.f, s.ins = None, None
            def select(s, *_): return s
            def eq(s, c, v): s.f = (c, v); return s
            def limit(s, *_): return s
            def insert(s, row): s.ins = row; return s
            def delete(s): return s
            def lt(s, *_): return s
            def execute(s):
                if db.fail: raise RuntimeError("table missing")
                if s.ins is not None: db.rows.append(s.ins)
                data = [r for r in db.rows if s.f is None or r.get(s.f[0]) == s.f[1]] if s.ins is None else [s.ins]
                return _types.SimpleNamespace(data=data)
        return Q()


@pytest.fixture
def revdb(monkeypatch):
    import core.db as coredb
    db = _RevDb()
    monkeypatch.setattr(coredb, "supabase", db)
    auth._revoked_local.clear(); auth._revoked_checked.clear()
    return db


def test_tokens_carry_unique_jti():
    a, b = auth.create_access_token({"sub": "u"}), auth.create_access_token({"sub": "u"})
    pa, pb = auth.decode_token(a), auth.decode_token(b)
    assert pa["jti"] and pa["jti"] != pb["jti"]
    assert auth.decode_token(auth.create_refresh_token({"sub": "u"}))["jti"]


def test_logged_out_access_token_is_rejected_but_others_still_work(revdb):
    from fastapi import HTTPException
    t1 = auth.create_access_token({"sub": "u", "role": "superadmin"})
    t2 = auth.create_access_token({"sub": "u", "role": "superadmin"})
    assert auth.get_current_user(t1)["username"] == "u"
    assert auth.revoke_token(t1) is True
    with pytest.raises(HTTPException) as e:
        auth.get_current_user(t1)
    assert e.value.status_code == 401
    assert auth.get_current_user(t2)["username"] == "u"          # other session untouched
    assert len(revdb.rows) == 1 and revdb.rows[0]["jti"] == auth.decode_token(t1)["jti"]


def test_revocation_survives_a_process_cache_miss(revdb):
    """Another instance (or a restart) must still see it via the table."""
    t = auth.create_access_token({"sub": "u", "role": "superadmin"})
    auth.revoke_token(t)
    auth._revoked_local.clear(); auth._revoked_checked.clear()
    assert auth.is_token_revoked(auth.decode_token(t)["jti"]) is True


def test_lookup_failure_fails_open_and_old_tokens_without_jti_still_work(monkeypatch):
    import core.db as coredb
    monkeypatch.setattr(coredb, "supabase", _RevDb(fail=True))
    auth._revoked_local.clear(); auth._revoked_checked.clear()
    t = auth.create_access_token({"sub": "u", "role": "superadmin"})
    assert auth.get_current_user(t)["username"] == "u"
    assert auth.is_token_revoked(None) is False


def test_logout_endpoint_revokes_access_and_refresh_and_blocks_refresh(client, revdb):
    pair = auth.create_access_token({"sub": "u", "role": "superadmin"}), auth.create_refresh_token({"sub": "u", "role": "superadmin"})
    r = client.post("/auth/logout", json={"refresh_token": pair[1]}, headers={"Authorization": "Bearer " + pair[0]})
    assert r.status_code == 200 and r.json()["revoked"] == 2
    assert client.get("/me", headers={"Authorization": "Bearer " + pair[0]}).status_code == 401
    assert client.post("/auth/refresh", json={"refresh_token": pair[1]}).status_code == 401


def test_logout_is_idempotent_and_ignores_garbage(client, revdb):
    r = client.post("/auth/logout", json={"refresh_token": "garbage"}, headers={"Authorization": "Bearer nope"})
    assert r.status_code == 200 and r.json()["revoked"] == 0


def test_dashboard_logout_calls_the_server_and_migration_exists():
    src = _read("dashboard.js")
    assert "/auth/logout" in src and "keepalive: true" in src
    mig = open(os.path.join(ROOT, "backend", "migrations", "phase3_revoked_tokens.sql"), encoding="utf8").read()
    assert "ENABLE ROW LEVEL SECURITY" in mig and "DROP TABLE IF EXISTS public.revoked_tokens" in mig


# ── Directory opt-in ─────────────────────────────────────────────────────────
import routes.marketplace_routes as mk


def test_listing_requires_explicit_true():
    assert mk._is_publicly_listed({"features_json": {"public_listing": True}}) is True
    for fj in (None, {}, {"public_listing": False}, {"public_listing": "true"}, {"public_listing": 1}, "x"):
        assert mk._is_publicly_listed({"features_json": fj}) is False
    assert mk._is_publicly_listed({}) is False


def test_public_payload_has_no_internal_id_or_private_fields():
    out = mk._safe_biz({"id": 9, "name": "Zed Cafe", "category": "Food", "features_json": {"public_listing": True},
                        "owner_password": "x", "whatsapp_token": "t", "owner_email": "a@b.c"})
    assert "id" not in out and "features_json" not in out
    assert not {"owner_password", "whatsapp_token", "owner_email"} & set(out)
    assert out["slug"] == "zed-cafe"


def test_directory_only_returns_opted_in_businesses(client, monkeypatch):
    import core.db as coredb
    rows = [{"id": 1, "name": "Listed Shop", "category": "x", "features_json": {"public_listing": True}},
            {"id": 2, "name": "Hidden Shop", "category": "x", "features_json": {}},
            {"id": 3, "name": "Legacy Shop", "category": "x"}]

    class Q:
        def select(s, *_): return s
        def eq(s, *_): return s
        def order(s, *a, **k): return s
        def execute(s): return _types.SimpleNamespace(data=list(rows))
    monkeypatch.setattr(coredb, "supabase", _types.SimpleNamespace(table=lambda _n: Q()))
    monkeypatch.setattr(mk, "_get_biz_columns", lambda: {"id", "name", "features_json"})
    monkeypatch.setattr(mk, "_get_biz_public_fields", lambda: "id,name,category")
    body = client.get("/api/directory").json()
    assert [b["name"] for b in body["businesses"]] == ["Listed Shop"]
    assert all("id" not in b for b in body["businesses"])


def test_platform_active_list_requires_superadmin(client):
    assert client.get("/platform/businesses/active").status_code in (401, 403)


def test_dashboard_has_public_listing_toggle_default_off():
    assert 'id="set-public-listing"' in open(os.path.join(STATIC, "dashboard.html"), encoding="utf8").read()
    assert "public_listing === true" in _read("dashboard.js")


# ── Chart.js self-hosted with SRI ────────────────────────────────────────────
def test_chartjs_is_self_hosted_with_matching_sri():
    import base64, hashlib
    html = _read("multidash.html")
    assert "cdnjs.cloudflare.com" not in html
    m = re.search(r'src="(/static/vendor/chart\.umd\.4\.4\.1\.js)" integrity="sha384-([^"]+)"', html)
    assert m
    data = open(os.path.join(STATIC, "vendor", "chart.umd.4.4.1.js"), "rb").read()
    assert base64.b64encode(hashlib.sha384(data).digest()).decode() == m.group(2)
    assert b"Chart.js v4.4.1" in data[:200]


def test_logout_reports_failure_when_revocation_cannot_be_persisted(client, monkeypatch):
    import core.db as coredb
    monkeypatch.setattr(coredb, "supabase", _RevDb(fail=True))
    auth._revoked_local.clear(); auth._revoked_checked.clear()
    t = auth.create_access_token({"sub": "u", "role": "superadmin"})
    r = client.post("/auth/logout", json={}, headers={"Authorization": "Bearer " + t})
    assert r.status_code == 503


def test_revoked_local_is_pruned_of_expired_entries(revdb):
    auth._revoked_local["old"] = 1.0
    auth.revoke_token(auth.create_access_token({"sub": "u"}))
    assert "old" not in auth._revoked_local
