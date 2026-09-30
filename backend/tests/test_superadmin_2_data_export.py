"""
tests/test_superadmin_2_data_export.py — SuperAdmin 2.0 Phase 12:
controlled data export.

Covers:
  - the businesses dataset's field allowlist never includes
    owner_password or whatsapp_token — the actual safety property this
    phase exists for
  - to_csv() only ever emits allowlisted columns, even if a row dict
    happens to carry extra fields (defense in depth on top of the SELECT
    itself only asking for allowlisted columns in production)
  - fetch_dataset() caps the row limit at EXPORT_ROW_LIMIT and raises
    KeyError for an unknown dataset name
  - GET /admin/saas/export/datasets lists every dataset with its fields
  - GET /admin/saas/export/{dataset}: unknown dataset -> 400, bad format
    -> 400, successful csv/json export, audit logging, authorization
"""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    import main
    return TestClient(main.app)


def _override_superadmin(client, username="root-admin"):
    import core.auth as auth
    client.app.dependency_overrides[auth.get_current_user] = lambda: {
        "username": username, "role": "superadmin", "business_id": None,
    }


def _override_business_role(client, business_id=1):
    import core.auth as auth
    client.app.dependency_overrides[auth.get_current_user] = lambda: {
        "username": "shopowner", "role": "business", "business_id": business_id,
    }


@pytest.fixture(autouse=True)
def _clear_overrides(client):
    yield
    client.app.dependency_overrides.clear()


# ── The actual safety property this phase exists for ────────────────────────

def test_businesses_allowlist_never_includes_secrets():
    from services.data_export import EXPORT_DATASETS
    fields = EXPORT_DATASETS["businesses"]["fields"]
    assert "owner_password" not in fields
    assert "whatsapp_token" not in fields


def test_to_csv_only_emits_allowlisted_columns_even_if_row_has_extra_fields():
    from services.data_export import to_csv
    rows = [{"id": 1, "name": "Biz", "owner_password": "hash123", "whatsapp_token": "enc-secret"}]
    csv_text = to_csv(rows, fields=["id", "name"])
    assert "hash123" not in csv_text
    assert "enc-secret" not in csv_text
    assert "id,name" in csv_text.splitlines()[0]


def test_to_csv_serializes_dict_values_as_json_not_python_repr():
    from services.data_export import to_csv
    rows = [{"id": 1, "metadata": {"a": 1}}]
    csv_text = to_csv(rows, fields=["id", "metadata"])
    assert '"a": 1' in csv_text or '""a"": 1' in csv_text  # csv-quoted JSON, not {'a': 1}
    assert "'a': 1" not in csv_text  # never Python single-quote repr


# ── fetch_dataset() ───────────────────────────────────────────────────────────

def test_fetch_dataset_raises_keyerror_for_unknown_dataset():
    from services.data_export import fetch_dataset
    with pytest.raises(KeyError):
        fetch_dataset("not_a_real_dataset")


def test_fetch_dataset_caps_limit_at_export_row_limit(fake_supabase, monkeypatch):
    from services import data_export

    captured = {}
    def _fake_fetch(limit):
        captured["limit"] = limit
        return []
    monkeypatch.setitem(data_export.EXPORT_DATASETS, "businesses", {
        **data_export.EXPORT_DATASETS["businesses"], "fetch": _fake_fetch,
    })

    data_export.fetch_dataset("businesses", limit=999999)
    assert captured["limit"] == data_export.EXPORT_ROW_LIMIT


# ── Authorization ─────────────────────────────────────────────────────────────

def test_business_role_rejected_on_export_endpoints(client):
    _override_business_role(client)
    assert client.get("/admin/saas/export/datasets").status_code == 403
    assert client.get("/admin/saas/export/businesses").status_code == 403


# ── GET /admin/saas/export/datasets ──────────────────────────────────────────

def test_export_datasets_lists_all_datasets_with_fields(client, fake_supabase):
    _override_superadmin(client)
    resp = client.get("/admin/saas/export/datasets")
    assert resp.status_code == 200
    body = resp.json()
    names = {d["name"] for d in body["datasets"]}
    assert "businesses" in names
    assert "security_events" in names
    biz = next(d for d in body["datasets"] if d["name"] == "businesses")
    assert "owner_password" not in biz["fields"]


# ── GET /admin/saas/export/{dataset} ─────────────────────────────────────────

def test_export_unknown_dataset_rejected(client, fake_supabase):
    _override_superadmin(client)
    resp = client.get("/admin/saas/export/not_a_real_dataset")
    assert resp.status_code == 400


def test_export_invalid_format_rejected(client, fake_supabase):
    _override_superadmin(client)
    resp = client.get("/admin/saas/export/businesses?format=xml")
    assert resp.status_code == 400


def test_export_csv_returns_csv_content_and_is_audit_logged(client, fake_supabase, monkeypatch):
    _override_superadmin(client, username="root-admin")
    fake_supabase.results["businesses"] = [
        {"id": 1, "name": "Biz One", "owner_username": "biz1", "owner_email": "a@b.com",
         "contact_phone": "123", "category": "retail", "currency": "USD",
         "subscription_tier": "starter", "billing_status": "active",
         "is_active": True, "created_at": "2026-01-01T00:00:00+00:00"},
    ]
    recorded = {}
    import routes.saas_admin_routes as saas_routes
    monkeypatch.setattr(saas_routes, "log_admin_action", lambda **kw: recorded.update(kw))

    resp = client.get("/admin/saas/export/businesses?format=csv")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "Biz One" in resp.text
    assert recorded["action"] == "data_export"
    assert recorded["metadata"]["dataset"] == "businesses"
    assert recorded["metadata"]["row_count"] == 1


def test_export_json_returns_row_count_and_rows(client, fake_supabase):
    _override_superadmin(client)
    fake_supabase.results["security_events"] = [
        {"id": 1, "event_type": "account_lockout", "created_at": "2026-09-10T00:00:00+00:00"},
    ]
    resp = client.get("/admin/saas/export/security_events?format=json")
    assert resp.status_code == 200
    body = resp.json()
    assert body["dataset"] == "security_events"
    assert body["row_count"] == 1
    assert body["rows"][0]["event_type"] == "account_lockout"
