"""Security audit F-10 (voice-note size cap), F-22 (dropped records, redaction), F-17 (debug use)."""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

import core.auth as auth
import core.safelog as safelog
import crud.admin_audit as admin_audit
import crud.security_events as sec_events
import services.whatsapp_service as wa

MEDIA_URL = "https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=1"


# ── F-10 ─────────────────────────────────────────────────────────────────────

def _fake_httpx(monkeypatch, meta, audio):
    calls = []

    class Resp:
        def __init__(self, data=None, content=b""):
            self._data, self.content = data, content

        def raise_for_status(self):
            pass

        def json(self):
            return self._data

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **_):
            calls.append(url)
            return Resp(content=audio) if url == MEDIA_URL else Resp(meta)

        async def post(self, url, **_):
            calls.append(url)
            return Resp({"text": "hello"})

    import httpx
    import socket
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: Client())
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("157.240.1.1", 443))])
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setattr(wa, "_MAX_AUDIO_BYTES", 10)
    return calls


def _transcribe():
    return asyncio.run(wa.transcribe_whatsapp_voice_note("m1", "tok"))


def test_voice_note_within_the_cap_is_transcribed(monkeypatch):
    _fake_httpx(monkeypatch, {"url": MEDIA_URL, "file_size": 10}, b"x" * 10)
    assert _transcribe() == "hello"


def test_voice_note_declared_too_big_is_not_downloaded(monkeypatch):
    calls = _fake_httpx(monkeypatch, {"url": MEDIA_URL, "file_size": 11}, b"x")
    assert _transcribe() is None and len(calls) == 1


def test_voice_note_actually_too_big_is_not_sent_for_transcription(monkeypatch):
    calls = _fake_httpx(monkeypatch, {"url": MEDIA_URL}, b"x" * 11)
    assert _transcribe() is None and len(calls) == 2


# ── F-22 ─────────────────────────────────────────────────────────────────────

class _Store:
    def __init__(self, error=None):
        self.rows, self.error = [], error

    def table(self, _):
        return self

    def insert(self, row):
        self.rows.append(row)
        return self

    def execute(self):
        if self.error:
            raise self.error


def test_redaction_hides_secrets_but_keeps_counters():
    clean = safelog.redact({"password": "p", "nested": [{"Access_Token": "t", "path": "/x"}],
                            "tokens": 42, "token_set": True, "api-key": {"v": "k"}})
    assert clean == {"password": "[redacted]", "nested": [{"Access_Token": "[redacted]", "path": "/x"}],
                     "tokens": 42, "token_set": True, "api-key": "[redacted]"}


@pytest.mark.parametrize("module,write", [
    (admin_audit, lambda: admin_audit.log_admin_action(actor_username="a", action="x",
                                                       metadata={"secret": "s3"})),
    (sec_events, lambda: sec_events.log_security_event(event_type="x", metadata={"secret": "s3"})),
])
def test_stored_metadata_is_redacted(monkeypatch, module, write):
    store = _Store()
    monkeypatch.setattr(module, "supabase", store)
    write()
    assert store.rows[0]["metadata"] == {"secret": "[redacted]"}


def test_dropped_records_warn_once_without_row_contents(monkeypatch, caplog):
    monkeypatch.setattr(safelog, "_last_drop_warning", {})
    monkeypatch.setattr(sec_events, "supabase", _Store(RuntimeError("failing row: victim@example.com")))
    with caplog.at_level("WARNING"):
        for _ in range(3):
            sec_events.log_security_event(event_type="account_lockout", username="victim@example.com")
    assert caplog.text.count("SECURITY RECORD NOT SAVED") == 1
    assert "victim@example.com" not in caplog.text and "RuntimeError" in caplog.text


# ── F-17 ─────────────────────────────────────────────────────────────────────

def test_debug_route_use_is_audited_only_for_super_admin(monkeypatch):
    from main import app
    import routes.admin_routes as admin_routes
    seen = []
    monkeypatch.setattr(admin_routes, "log_admin_action", lambda **kw: seen.append(kw))
    monkeypatch.setattr(auth, "is_token_revoked", lambda jti: False)
    monkeypatch.setattr(auth, "_lookup_auth_state", lambda bid: (None, True))
    client = TestClient(app)

    def get(role, **extra):
        token = auth.create_access_token({"sub": "u", "role": role, **extra})
        return client.get("/debug/env", headers={"Authorization": f"Bearer {token}"}).status_code

    assert get("business", business_id=1) == 403 and not seen
    assert get("superadmin") == 200
    assert [(s["action"], s["metadata"]["path"]) for s in seen] == [("debug.access", "/debug/env")]
