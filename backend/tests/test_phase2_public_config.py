"""/config/public is unauthenticated: it must never publish a privileged Supabase key."""
import base64
import json

import pytest
from fastapi.testclient import TestClient


def _jwt(role):
    b = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{b({'alg': 'HS256'})}.{b({'role': role, 'ref': 'x'})}.sig"


@pytest.fixture
def client():
    import main
    return TestClient(main.app)


def test_service_role_key_is_never_published_even_when_anon_key_is_unset(client, monkeypatch):
    monkeypatch.delenv("SUPABASE_ANON_KEY", raising=False)
    monkeypatch.setenv("SUPABASE_KEY", _jwt("service_role"))
    r = client.get("/config/public")
    assert r.status_code == 200
    assert r.json()["supabase_anon_key"] == ""
    assert "service_role" not in r.text and _jwt("service_role") not in r.text


def test_a_service_role_key_misplaced_in_the_anon_variable_is_withheld(client, monkeypatch):
    monkeypatch.setenv("SUPABASE_ANON_KEY", _jwt("service_role"))
    assert client.get("/config/public").json()["supabase_anon_key"] == ""


def test_real_anon_key_is_still_published(client, monkeypatch):
    monkeypatch.setenv("SUPABASE_ANON_KEY", _jwt("anon"))
    monkeypatch.setenv("SUPABASE_URL", "https://abc.supabase.co")
    body = client.get("/config/public").json()
    assert body["supabase_anon_key"] == _jwt("anon") and body["supabase_url"] == "https://abc.supabase.co"


def test_garbage_key_is_withheld(client, monkeypatch):
    monkeypatch.setenv("SUPABASE_ANON_KEY", "not-a-jwt")
    assert client.get("/config/public").json()["supabase_anon_key"] == ""


def test_value_identical_to_the_backend_key_is_withheld(client, monkeypatch):
    k = _jwt("anon")
    monkeypatch.setenv("SUPABASE_ANON_KEY", k)
    monkeypatch.setenv("SUPABASE_KEY", k)          # misconfiguration: both variables hold the same key
    assert client.get("/config/public").json()["supabase_anon_key"] == ""
