"""
tests/test_account_deletion.py — self-service account deletion.

Covers:
  - services/account_deletion.build_backup_zip / upload_backup /
    request_deletion: backup-before-deactivate ordering, and that a
    failure in the backup step raises WITHOUT deactivating the account.
  - services/account_deletion.purge_due_accounts: purges what's due,
    keeps going after one failure.
  - POST /me/delete-account: wrong password / wrong confirm text
    rejected; correct flow deactivates + returns purge_after; a backup
    failure returns 500 and leaves the account untouched.
  - POST /billing/purge-deleted-accounts: CRON_SECRET enforcement,
    matching the existing trial-warnings/cart-cleanup endpoints.
  - Reactivating a business clears its pending deletion record.
"""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    import main
    return TestClient(main.app)


@pytest.fixture(autouse=True)
def _clear_overrides(client):
    yield
    client.app.dependency_overrides.clear()


def _override_business(client, business_id=7, username="shopowner"):
    import routes.business_routes as br
    dep = lambda: {"business_id": business_id, "username": username, "role": "business"}
    client.app.dependency_overrides[br.require_business] = dep


def _override_superadmin(client, username="root-admin"):
    import routes.admin_routes as ar
    client.app.dependency_overrides[ar.require_superadmin] = lambda: {"username": username, "role": "superadmin"}


# ── services/account_deletion.py ────────────────────────────────────────────

def test_request_deletion_raises_and_does_not_deactivate_when_backup_fails(monkeypatch):
    import services.account_deletion as ad

    monkeypatch.setattr(ad, "build_backup_zip", lambda bid: (_ for _ in ()).throw(RuntimeError("db down")))
    deactivate_called = {}
    monkeypatch.setattr(ad, "deactivate_and_invalidate_sessions", lambda bid: deactivate_called.setdefault("called", True))

    with pytest.raises(RuntimeError):
        ad.request_deletion(business_id=1, business_row={"name": "Biz"}, reason="")

    assert "called" not in deactivate_called


def test_request_deletion_raises_when_upload_fails(monkeypatch):
    import services.account_deletion as ad

    monkeypatch.setattr(ad, "build_backup_zip", lambda bid: b"fake-zip-bytes")
    monkeypatch.setattr(ad, "upload_backup", lambda bid, data: (_ for _ in ()).throw(RuntimeError("storage down")))
    deactivate_called = {}
    monkeypatch.setattr(ad, "deactivate_and_invalidate_sessions", lambda bid: deactivate_called.setdefault("called", True))

    with pytest.raises(RuntimeError):
        ad.request_deletion(business_id=1, business_row={"name": "Biz"}, reason="")

    assert "called" not in deactivate_called


def test_request_deletion_success_path_deactivates_after_backup_and_record(monkeypatch):
    import services.account_deletion as ad
    import crud.account_deletions as deletions_crud

    monkeypatch.setattr(ad, "build_backup_zip", lambda bid: b"fake-zip-bytes")
    monkeypatch.setattr(ad, "upload_backup", lambda bid, data: "7/12345.zip")
    recorded = {}
    monkeypatch.setattr(deletions_crud, "insert_deletion_record", lambda **kw: recorded.update(kw) or {"id": 1})
    deactivated = {}
    monkeypatch.setattr(ad, "deactivate_and_invalidate_sessions", lambda bid: deactivated.setdefault("bid", bid))

    result = ad.request_deletion(business_id=7, business_row={"name": "Biz", "owner_username": "biz1"}, reason="too expensive")

    assert deactivated["bid"] == 7
    assert recorded["business_id"] == 7
    assert recorded["reason"] == "too expensive"
    assert recorded["backup_path"] == "7/12345.zip"
    assert "purge_after" in result


def test_build_backup_zip_raises_when_nothing_could_be_fetched(monkeypatch):
    import services.account_deletion as ad
    monkeypatch.setattr(ad, "_fetch_dataset", lambda name, cfg, bid: None)
    with pytest.raises(RuntimeError):
        ad.build_backup_zip(1)


def test_purge_due_accounts_continues_after_one_failure(monkeypatch):
    import services.account_deletion as ad
    import crud.account_deletions as deletions_crud
    import crud as crud_mod

    monkeypatch.setattr(deletions_crud, "list_due_for_purge", lambda now_iso, limit=200: [
        {"id": 1, "business_id": 10, "backup_path": "10/a.zip"},
        {"id": 2, "business_id": 11, "backup_path": "11/b.zip"},
    ])

    def _fake_delete_business(bid):
        if bid == 10:
            raise RuntimeError("boom")
        return None

    monkeypatch.setattr(crud_mod, "delete_business", _fake_delete_business)
    marked = []
    monkeypatch.setattr(deletions_crud, "mark_purged", lambda rid: marked.append(rid))

    class _FakeStorage:
        def from_(self, bucket): return self
        def remove(self, paths): return None

    monkeypatch.setattr(ad.supabase, "storage", _FakeStorage(), raising=False)

    result = ad.purge_due_accounts()
    assert result["checked"] == 2
    assert result["purged"] == 1
    assert result["errors"] == 1
    assert marked == [2]


# ── POST /me/delete-account ──────────────────────────────────────────────────

def test_delete_account_rejects_wrong_confirm_text(client):
    _override_business(client)
    resp = client.post("/me/delete-account", json={"password": "whatever", "confirm": "yes please"})
    assert resp.status_code == 400


def test_delete_account_rejects_wrong_password(client, monkeypatch):
    _override_business(client)
    import routes.business_routes as br
    monkeypatch.setattr(br.crud, "get_business_by_id", lambda bid: {"owner_password": "$2b$hash", "name": "Biz"})
    monkeypatch.setattr(br, "verify_password", lambda plain, stored: False)

    resp = client.post("/me/delete-account", json={"password": "wrong", "confirm": "DELETE"})
    assert resp.status_code == 401


def test_delete_account_success(client, monkeypatch):
    _override_business(client, business_id=7, username="shopowner")
    import routes.business_routes as br
    monkeypatch.setattr(br.crud, "get_business_by_id", lambda bid: {
        "owner_password": "$2b$hash", "name": "Biz", "owner_username": "shopowner",
    })
    monkeypatch.setattr(br, "verify_password", lambda plain, stored: True)
    monkeypatch.setattr(
        "services.account_deletion.request_deletion",
        lambda **kw: {"purge_after": "2027-01-01T00:00:00+00:00", "backup_format": "csv"},
    )

    resp = client.post("/me/delete-account", json={"password": "right", "confirm": "delete", "reason": "moving on"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["purge_after"] == "2027-01-01T00:00:00+00:00"
    assert "90 days" in body["message"] or "purge_after" in body


def test_delete_account_backup_failure_returns_500_and_is_not_silently_ok(client, monkeypatch):
    _override_business(client)
    import routes.business_routes as br
    monkeypatch.setattr(br.crud, "get_business_by_id", lambda bid: {
        "owner_password": "$2b$hash", "name": "Biz", "owner_username": "shopowner",
    })
    monkeypatch.setattr(br, "verify_password", lambda plain, stored: True)
    monkeypatch.setattr(
        "services.account_deletion.request_deletion",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("backup failed")),
    )

    resp = client.post("/me/delete-account", json={"password": "right", "confirm": "DELETE"})
    assert resp.status_code == 500


# ── POST /billing/purge-deleted-accounts ────────────────────────────────────

def test_purge_endpoint_requires_cron_secret(client, monkeypatch):
    monkeypatch.delenv("CRON_SECRET", raising=False)
    resp = client.post("/billing/purge-deleted-accounts")
    assert resp.status_code == 503


def test_purge_endpoint_rejects_wrong_secret(client, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "right-secret")
    resp = client.post("/billing/purge-deleted-accounts", headers={"x-cron-secret": "wrong"})
    assert resp.status_code == 403


def test_purge_endpoint_runs_with_correct_secret(client, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "right-secret")
    monkeypatch.setattr(
        "services.account_deletion.purge_due_accounts",
        lambda limit=200: {"purged": 2, "errors": 0, "checked": 2},
    )
    resp = client.post("/billing/purge-deleted-accounts", headers={"x-cron-secret": "right-secret"})
    assert resp.status_code == 200
    assert resp.json()["purged"] == 2


# ── Reactivation clears pending deletion ────────────────────────────────────

def test_activate_business_clears_pending_deletion(client, monkeypatch):
    _override_superadmin(client)
    import routes.admin_routes as ar
    monkeypatch.setattr(ar.crud, "get_business_by_id", lambda bid: {"id": 5, "is_active": False})
    monkeypatch.setattr(ar.crud, "update_business", lambda bid, data: {"ok": True})
    monkeypatch.setattr(ar, "log_admin_action", lambda **kw: None)

    import crud.account_deletions as deletions_crud
    called = {}
    monkeypatch.setattr(deletions_crud, "mark_reactivated", lambda bid: called.setdefault("bid", bid))

    resp = client.post("/platform/businesses/5/activate")
    assert resp.status_code == 200
    assert called["bid"] == 5
