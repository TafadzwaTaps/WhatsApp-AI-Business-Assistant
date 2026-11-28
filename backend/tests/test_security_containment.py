"""Security audit Stage 4d: temporary restrictions, session revocation and their enforcement."""
from __future__ import annotations

import datetime as dt
import pathlib
import re
import shutil
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import core.auth as auth
import crud.admin_audit as admin_audit
import crud.businesses as businesses
import crud.security_events as store
import services.restrictions as restrictions
import services.security_events as events
from test_security_cases import _DB

STATIC = pathlib.Path(__file__).resolve().parents[2] / "static"
HERE = pathlib.Path(__file__).resolve().parent
BASE = "/admin/saas/security"
BAD, GOOD, ADMIN = "93.184.216.34", "93.184.216.99", "151.101.1.69"


class _Store(_DB):
    def table(self, name):
        query = super().table(name)
        query.gt = lambda column, value: (query.tests.append(lambda r: str(r.get(column)) > str(value)), query)[1]
        return query


@pytest.fixture
def db(monkeypatch):
    fake = _Store()
    for module in (store, admin_audit, businesses, sys.modules["core.db"]):
        monkeypatch.setattr(module, "supabase", fake)
    fake.rows("businesses").extend([
        {"id": 7, "name": "Shop", "owner_username": "shop", "is_active": True, "password_changed_at": None},
        {"id": 8, "name": "Other", "owner_username": "other", "is_active": True, "password_changed_at": None}])
    auth._pwd_ts_cache.clear()
    auth._active_cache.clear()
    businesses._biz_cache_clear()
    yield fake
    auth._pwd_ts_cache.clear()
    auth._active_cache.clear()
    businesses._biz_cache_clear()


@pytest.fixture
def app_client(monkeypatch):
    from main import app
    import services.security as sec
    monkeypatch.setattr(auth, "is_token_revoked", lambda jti: False)
    monkeypatch.delenv("SECURITY_IP_BLOCKING", raising=False)
    monkeypatch.delenv("EDGE_SHARED_SECRET", raising=False)
    monkeypatch.delenv("TRUSTED_PROXY_DEPTH", raising=False)
    sec._rate_store.clear()
    yield TestClient(app)
    sec._rate_store.clear()


def _bearer(role="superadmin", **extra):
    return {"Authorization": "Bearer " + auth.create_access_token({"sub": "root", "role": role, **extra})}


def _admin(**headers):
    return {**_bearer(), "X-Forwarded-For": ADMIN, **headers}


def _from(ip, **headers):
    return {"X-Forwarded-For": ip, **headers}


def _create(client, **body):
    payload = {"kind": "ip", "target": BAD, "minutes": 60, "reason": "attack", "confirm": True, **body}
    return client.post(f"{BASE}/restrictions", json=payload, headers=_admin())


def _restrict(client, feature, business=7, **extra):
    r = _create(client, kind="feature", target=str(business), feature=feature, **extra)
    assert r.status_code == 201, r.text
    return r.json()


def _queued(event_type):
    return [e["fields"] for e in events._pending.values() if e["fields"]["event_type"] == event_type]


def _past():
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)).isoformat()


# ── Access and validation ────────────────────────────────────────────────────

def _routes():
    from routes.security_containment_routes import router
    return [(m, re.sub(r"\{[^}]+\}", "7", r.path)) for r in router.routes for m in r.methods]


@pytest.mark.parametrize("method,path", _routes())
def test_only_the_super_admin_may_use_containment(app_client, db, method, path):
    for headers in ({}, _bearer("business", business_id=7),
                    _bearer("business", business_id=7, impersonated_by="root")):
        assert app_client.request(method, path, headers=headers, json={}).status_code in (401, 403)
    assert not db.rows("security_restrictions") and db.rows("businesses")[0]["password_changed_at"] is None


@pytest.mark.parametrize("change", [
    {"confirm": False}, {"confirm": None}, {"reason": "  "}, {"minutes": 4}, {"minutes": 43201},
    {"kind": "account"}, {"feature": "storefront", "kind": "feature"}, {"created_by": "x"}])
def test_a_reason_confirmation_and_sensible_duration_are_required(app_client, db, change):
    assert _create(app_client, **change).status_code == 422
    assert not db.rows("security_restrictions")


@pytest.mark.parametrize("target", [
    "10.0.0.5", "192.168.1.1", "127.0.0.1", "169.254.1.1", "::1", "fe80::1", "224.0.0.1", "0.0.0.0",
    "104.16.1.1", "172.68.0.1", "2606:4700::1", "::ffff:104.16.1.1", "::ffff:10.0.0.1",
    ADMIN, "::ffff:" + ADMIN, "93.184.216.0/24", "example.com", "1.2.3.4,5.6.7.8"])
def test_dangerous_or_invalid_addresses_cannot_be_blocked(app_client, db, target):
    r = _create(app_client, target=target)
    assert r.status_code == 422, (target, r.text)
    assert not db.rows("security_restrictions")


def test_unknown_business_incident_and_duplicates_are_refused(app_client, db):
    assert _create(app_client, kind="feature", target="99", feature="uploads").status_code == 404
    assert _create(app_client, kind="feature", target="shop", feature="uploads").status_code == 422
    assert _create(app_client, incident_id=5).status_code == 404
    assert _create(app_client).status_code == 201
    assert _create(app_client).status_code == 409
    db.rows("security_restrictions")[0]["expires_at"] = _past()      # an expired one does not block a new one
    assert _create(app_client).status_code == 201


def test_every_action_is_audited_and_added_to_the_incident(app_client, db):
    db.rows("security_incidents").append({"id": 1, "status": "detected"})
    made = _create(app_client, incident_id=1).json()
    assert made["warnings"] == [] and made["restriction"]["created_by"] == "root"
    r = app_client.post(f"{BASE}/restrictions/1/revoke", json={"reason": "false alarm"}, headers=_admin())
    assert r.status_code == 200
    again = app_client.post(f"{BASE}/restrictions/1/revoke", json={"reason": "x"}, headers=_admin())
    assert again.status_code == 404
    actions = [(a["action"], a["reason"]) for a in db.rows("admin_audit_logs")]
    assert actions == [("restriction.create", "attack"), ("restriction.revoke", "false alarm")]
    assert [n["kind"] for n in db.rows("security_incident_notes")] == ["containment", "containment"]
    row = db.rows("security_restrictions")[0]
    assert row["revoked_by"] == "root" and row["revoke_reason"] == "false alarm"


# ── Address blocking ─────────────────────────────────────────────────────────

def test_a_blocked_address_is_refused_and_released(app_client, db):
    assert app_client.get("/config/public", headers=_from(BAD)).status_code == 200
    _create(app_client)
    refused = app_client.get("/config/public", headers=_from(BAD))
    assert refused.status_code == 403 and "temporarily restricted" in refused.json()["detail"]
    assert refused.headers["x-content-type-options"] == "nosniff"
    assert app_client.get("/config/public", headers=_from("::ffff:" + BAD)).status_code == 403
    assert app_client.get("/config/public", headers=_from(GOOD)).status_code == 200
    # a forged first hop does not help or harm: the proxy-added last hop is what counts
    assert app_client.get("/config/public", headers=_from(f"{BAD}, {GOOD}")).status_code == 200
    assert _queued("ip_blocked_request")[0]["ip"] == BAD
    app_client.post(f"{BASE}/restrictions/1/revoke", json={"reason": "done"}, headers=_admin())
    assert app_client.get("/config/public", headers=_from(BAD)).status_code == 200


@pytest.mark.parametrize("method,path", [
    ("GET", "/healthz"), ("GET", "/webhook"), ("POST", "/webhook"), ("POST", "/payment/webhook"),
    ("POST", "/payments/paypal/webhook"), ("POST", "/billing/webhook")])
def test_payment_and_whatsapp_webhooks_and_health_are_never_blocked(app_client, db, method, path):
    _create(app_client)
    r = app_client.request(method, path, headers=_from(BAD), content=b"{}")
    assert "temporarily restricted" not in r.text
    assert not _queued("ip_blocked_request")


def test_the_exempt_paths_are_real_routes():
    from main import app
    paths = {getattr(r, "path", "") for r in app.routes}
    assert restrictions.EXEMPT_PATHS <= paths, restrictions.EXEMPT_PATHS - paths


def test_super_admin_token_and_off_switch_pass_a_block(app_client, db, monkeypatch):
    _create(app_client)
    assert app_client.get(f"{BASE}/restrictions", headers={**_bearer(), **_from(BAD)}).status_code == 200
    assert app_client.get("/config/public", headers={**_bearer("business", business_id=7), **_from(BAD)}).status_code == 403
    assert app_client.get("/config/public", headers={"Authorization": "Bearer junk", **_from(BAD)}).status_code == 403
    monkeypatch.setenv("SECURITY_IP_BLOCKING", "0")
    assert app_client.get("/config/public", headers=_from(BAD)).status_code == 200
    body = app_client.get(f"{BASE}/restrictions", headers=_admin()).json()
    assert body["ip_blocking_enabled"] is False and body["your_address"] == ADMIN
    other = _create(app_client, target=GOOD).json()
    assert "switched off" in other["warnings"][0]


def test_expiry_applies_without_waiting_for_a_reload(app_client, db):
    _create(app_client)
    restrictions._snapshot["ips"][BAD] = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
    assert app_client.get("/config/public", headers=_from(BAD)).status_code == 200


def test_blocked_address_cannot_open_the_live_socket(app_client, db):
    _create(app_client)
    with pytest.raises(WebSocketDisconnect) as refused:
        with app_client.websocket_connect("/ws/chat/7", headers=_from(BAD)) as ws:
            ws.receive_text()
    assert refused.value.code == 4003


def test_rows_that_could_restrict_forever_are_ignored(db):
    db.rows("security_restrictions").extend([
        {"kind": "ip", "target": BAD, "expires_at": None, "revoked_at": None},
        {"kind": "ip", "target": GOOD, "expires_at": "soon", "revoked_at": None},
        {"kind": "ip", "target": "nonsense", "expires_at": "2999-01-01T00:00:00+00:00", "revoked_at": None}])
    assert restrictions.refresh() and restrictions._snapshot["ips"] == {}


def test_unreadable_list_keeps_the_last_copy_then_fails_open(app_client, db, monkeypatch):
    _create(app_client)
    clock = [1000.0]
    monkeypatch.setattr(restrictions.time, "monotonic", lambda: clock[0])
    db.fail.add("security_restrictions")
    assert restrictions.refresh() is False
    assert app_client.get("/config/public", headers=_from(BAD)).status_code == 403   # last good copy
    clock[0] += restrictions.STALE_SECONDS + 1
    assert restrictions.refresh() is False
    assert app_client.get("/config/public", headers=_from(BAD)).status_code == 200   # open, and says so
    assert _queued("restriction_check_failed")
    db.fail.clear()
    assert restrictions.refresh()
    assert app_client.get("/config/public", headers=_from(BAD)).status_code == 403


def test_nothing_is_restricted_before_the_database_change(app_client, db):
    db.fail, db.code = {"security_restrictions"}, "42P01"
    assert restrictions.refresh() is True
    assert app_client.get("/config/public", headers=_from(BAD)).status_code == 200
    assert not _queued("restriction_check_failed")
    listing = app_client.get(f"{BASE}/restrictions", headers=_admin()).json()
    assert listing["available"] is False and "database change" in listing["problem"]


def test_a_broken_check_never_blocks(app_client, db, monkeypatch):
    _create(app_client)
    monkeypatch.setattr(restrictions, "_current", lambda: 1 / 0)
    assert app_client.get("/config/public", headers=_from(BAD)).status_code == 200
    assert restrictions.feature_blocked(7, "uploads") is False


def test_checks_do_not_read_the_database_on_the_request(monkeypatch):
    started = []
    monkeypatch.setattr(restrictions, "_refresh_in_background", lambda: started.append(1))
    monkeypatch.setattr(restrictions, "refresh", lambda: pytest.fail("read during a request"))
    for _ in range(5):
        restrictions.feature_blocked(7, "uploads")
    assert started == [1]                       # once per CACHE_SECONDS


# ── Feature restrictions ─────────────────────────────────────────────────────

def test_campaign_sending_is_refused_for_that_business_only(app_client, db, monkeypatch):
    import crud
    from services.campaign_service import CampaignService
    _restrict(app_client, "campaigns")
    asked = []
    monkeypatch.setattr(crud, "get_business_by_id", lambda b: asked.append(b))
    refused = CampaignService.run(7, "all", "hello there")
    assert refused["ok"] is False and "restricted" in refused["error"] and refused["sent"] == 0 and not asked
    CampaignService.run(7, "all", "hello there", dry_run=True)       # previews still work
    CampaignService.run(8, "all", "hello there")
    assert asked == [7, 8]
    assert _queued("restricted_feature_blocked")[0]["rule"] == "campaigns"


def test_broadcast_is_refused(app_client, db, monkeypatch):
    import core.plan_guard as plan_guard
    from routes import business_routes
    _restrict(app_client, "campaigns")
    route = next(r for r in business_routes.router.routes if r.path == "/broadcast")
    for dependency in route.dependant.dependencies:
        if dependency.name != "user":
            app_client.app.dependency_overrides[dependency.call] = lambda: None
    try:
        r = app_client.post("/broadcast", json={"message": "hello"}, headers=_bearer("business", business_id=7))
    finally:
        app_client.app.dependency_overrides.clear()
    assert r.status_code == 403 and "restricted" in r.json()["detail"], r.text


def test_uploads_are_refused(app_client, db):
    from services.content_moderation import screen_upload
    _restrict(app_client, "uploads")
    common = dict(data=b"x", content_type="image/png", filename="a.png", username="u", ip=None, endpoint="/x")
    ok, message = screen_upload(business_id=7, **common)
    assert ok is False and "restricted" in message
    assert "restricted" not in screen_upload(business_id=8, **common)[1]


def test_ai_replies_stop_for_that_business(app_client, db, monkeypatch):
    import core.platform_controls as controls
    import services._ai_state as state
    from services.ai import generate_reply
    _restrict(app_client, "ai_replies")
    monkeypatch.setattr(controls, "is_paused", lambda _f: False)
    monkeypatch.setattr(state, "is_in_human_handoff", lambda *_: False)
    reply = generate_reply("hi", "+441234", 7, "Shop", [])
    assert "temporarily paused" in reply
    assert generate_reply("hi", "+441234", 7, "Shop", [], message_is_from_agent=True) == ""


# ── Signing out and restoring ────────────────────────────────────────────────

def test_sign_out_ends_existing_sessions_without_changing_the_password(app_client, db):
    owner = {"sub": "shop", "role": "business", "business_id": 7}
    access = {"Authorization": "Bearer " + auth.create_access_token(owner)}
    refresh = auth.create_refresh_token(owner)
    assert app_client.get("/chat/customers", headers=access).status_code == 200
    before = dict(db.rows("businesses")[0])
    url = f"{BASE}/businesses/7/sign-out"
    assert app_client.post(url, json={"reason": "stolen phone"}, headers=_admin()).status_code == 422
    r = app_client.post(url, json={"reason": "stolen phone", "confirm": True}, headers=_admin())
    assert r.status_code == 200 and r.json()["warnings"] == []
    assert app_client.get("/chat/customers", headers=access).status_code == 401       # at once
    assert app_client.post("/auth/refresh", json={"refresh_token": refresh}).status_code == 401
    after = db.rows("businesses")[0]
    assert {k for k in after if after[k] != before.get(k)} == {"password_changed_at"}
    assert db.rows("businesses")[1]["password_changed_at"] is None
    assert db.rows("admin_audit_logs")[-1]["action"] == "business.sessions_revoked"
    assert app_client.post(f"{BASE}/businesses/99/sign-out", json={"reason": "r", "confirm": True},
                           headers=_admin()).status_code == 404


def test_restoring_records_the_reason(app_client, db, monkeypatch):
    import crud
    import routes.admin_routes as admin_routes
    seen = []
    monkeypatch.setattr(crud, "update_business", lambda *a: None)
    monkeypatch.setattr(admin_routes, "log_admin_action", lambda **kw: seen.append(kw))
    r = app_client.post("/platform/businesses/7/activate?reason=cleared", headers=_admin())
    assert r.status_code == 200 and seen[0]["reason"] == "cleared"


# ── Dashboard ────────────────────────────────────────────────────────────────

def test_containment_screen_is_wired():
    html = (STATIC / "dashboard.html").read_text(encoding="utf-8")
    script = (STATIC / "security_containment.js").read_text(encoding="utf-8")
    assert html.index("/static/security_center.js") < html.index("/static/security_containment.js")
    wanted = set(re.findall(r"'(sc-[a-z]+(?:-[a-z]+)*)'", script))
    missing = sorted(i for i in wanted if f'id="{i}"' not in html)
    assert wanted and not missing, missing
    ids = re.findall(r'\bid="(sc-[^"]+)"', html)
    assert len(ids) == len(set(ids)), "duplicate ids"
    assert not re.search(r"\son[a-z]+\s*=\s*[\"']", script)
    offered = set(re.findall(r'<option value="([a-z_]+)">[^<]+</option>',
                             html.split('id="sc-c-feature">')[1].split("</select>")[0]))
    assert offered == set(restrictions.FEATURES)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_containment_script_behaviour_in_node():
    result = subprocess.run(["node", str(HERE / "security_containment_check.js"), str(STATIC)],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr or result.stdout
    assert "security containment OK" in result.stdout
