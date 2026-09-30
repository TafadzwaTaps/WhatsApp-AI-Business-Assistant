"""
tests/test_superadmin_2_security.py — SuperAdmin 2.0 Phase 6: Security
Dashboard (/admin/saas/security, services.security.get_security_snapshot).

Covers:
  - locked accounts / flagged IPs only appear once they actually cross
    the same thresholds the real enforcement code uses (no separate,
    drifting copy of the limits)
  - suspicious signup IPs are read from the real rate-limit store, not
    a parallel tracker
  - webhook invalid-signature counter increments on a bad signature and
    never on a valid one
  - the snapshot never raises, even if one counter's internal state is
    corrupted
  - "active sessions" is deliberately NOT reported (stateless JWT — no
    server-side session store exists to report it from honestly)
  - authorization: business role rejected on the new endpoint
"""

import time

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _reset_security_state():
    """services/security.py's counters are module-level and in-process —
    reset the ones this test file touches so tests don't leak into each
    other (mirrors the intent of conftest's _reset_fake_db for the DB)."""
    import services.security as sec
    sec._account_login_fails.clear()
    sec._ip_login_fails.clear()
    sec._failed_logins.clear()
    sec._rate_store.clear()
    sec._webhook_sig_failures.clear()
    sec._msg_seen.clear()
    yield
    sec._account_login_fails.clear()
    sec._ip_login_fails.clear()
    sec._failed_logins.clear()
    sec._rate_store.clear()
    sec._webhook_sig_failures.clear()
    sec._msg_seen.clear()


@pytest.fixture
def client():
    import main
    return TestClient(main.app)


def _override_superadmin(client):
    import core.auth as auth
    client.app.dependency_overrides[auth.get_current_user] = lambda: {
        "username": "root-admin", "role": "superadmin", "business_id": None,
    }


def _override_business_role(client):
    import core.auth as auth
    client.app.dependency_overrides[auth.get_current_user] = lambda: {
        "username": "shopowner", "role": "business", "business_id": 1,
    }


@pytest.fixture(autouse=True)
def _clear_overrides(client):
    yield
    client.app.dependency_overrides.clear()


def test_business_role_is_rejected_on_security_dashboard(client):
    _override_business_role(client)
    resp = client.get("/admin/saas/security")
    assert resp.status_code == 403


def test_locked_account_appears_only_after_crossing_the_real_threshold():
    import services.security as sec
    # One under the threshold — should NOT show up as locked yet.
    for _ in range(sec.LOGIN_MAX_ATTEMPTS_PER_ACCOUNT - 1):
        sec.record_failed_login_account("shopowner")
    snap = sec.get_security_snapshot()
    assert not any(a["username"] == "shopowner" for a in snap["locked_accounts"])

    # One more crosses it.
    sec.record_failed_login_account("shopowner")
    snap = sec.get_security_snapshot()
    assert any(a["username"] == "shopowner" for a in snap["locked_accounts"])


def test_flagged_ip_appears_only_after_crossing_the_real_threshold():
    import services.security as sec
    for _ in range(sec.LOGIN_MAX_ATTEMPTS_PER_IP):
        sec.record_failed_login_ip("203.0.113.5")
    snap = sec.get_security_snapshot()
    assert any(f["ip"] == "203.0.113.5" for f in snap["flagged_login_ips"])


def test_suspicious_signup_ips_read_from_real_rate_limit_store():
    import services.security as sec
    from starlette.requests import Request

    class _FakeClient:
        host = "198.51.100.9"

    scope = {"type": "http", "headers": [], "client": ("198.51.100.9", 1234)}
    req = Request(scope)

    try:
        for _ in range(sec.SIGNUP_MAX_ATTEMPTS_PER_IP_HOUR):
            sec.check_signup_abuse(req)
    except sec.RateLimitExceeded:
        pass

    snap = sec.get_security_snapshot()
    assert any(s["ip"] == "198.51.100.9" for s in snap["suspicious_signup_ips"])


def test_webhook_invalid_signature_increments_counter_valid_does_not():
    import services.security as sec
    payload = b'{"test": true}'
    secret = "shh"

    valid_sig = "sha256=" + sec.hmac.new(secret.encode(), payload, sec.hashlib.sha256).hexdigest()
    assert sec.verify_meta_signature(payload, valid_sig, secret) is True
    snap_after_valid = sec.get_security_snapshot()
    assert snap_after_valid["webhook_invalid_signatures_24h"] == 0

    assert sec.verify_meta_signature(payload, "sha256=deadbeef", secret) is False
    snap_after_invalid = sec.get_security_snapshot()
    assert snap_after_invalid["webhook_invalid_signatures_24h"] == 1


def test_snapshot_never_raises_even_with_corrupted_internal_state(monkeypatch):
    import services.security as sec
    # Simulate a corrupted counter — must degrade gracefully, not 500.
    monkeypatch.setattr(sec, "_account_login_fails", None)
    snap = sec.get_security_snapshot()
    assert snap["locked_accounts"] == []  # failed gracefully, not raised


def test_snapshot_does_not_report_active_sessions():
    """
    Auth here is stateless JWT with no server-side session store — there
    is no honest way to report "active sessions" from this codebase.
    The spec explicitly forbids fabricating data the system doesn't
    actually track, so this key must simply not exist rather than be
    faked as 0 or some placeholder.
    """
    import services.security as sec
    snap = sec.get_security_snapshot()
    assert "active_sessions" not in snap


def test_security_dashboard_endpoint_returns_snapshot_plus_open_flags(client, fake_supabase):
    _override_superadmin(client)
    fake_supabase.results["business_risk_flags"] = [
        {"id": 1, "business_id": 5, "status": "open", "risk_level": "low", "reason": "test"},
    ]
    resp = client.get("/admin/saas/security")
    assert resp.status_code == 200
    body = resp.json()
    assert "note" in body
    assert body["open_risk_flags_count"] == 1
