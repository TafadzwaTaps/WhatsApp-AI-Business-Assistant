"""Stage 2: one Graph API version setting (WA_GRAPH_VERSION), default unchanged (v18.0), and every call site honours it.
All HTTP is mocked; nothing reaches Meta."""
import asyncio
import os
import re

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _versioned(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("WA_GRAPH_VERSION", raising=False)
    else:
        monkeypatch.setenv("WA_GRAPH_VERSION", value)
    return value or "v18.0"


CASES = [None, "v24.0", "v27.0"]


def test_no_graph_version_is_hard_coded_outside_the_central_module():
    offenders = []
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in ("tests", "__pycache__", ".venv", "node_modules", "migrations")]
        for f in files:
            if f.endswith(".py"):
                src = open(os.path.join(base, f), encoding="utf8", errors="ignore").read()
                if re.search(r"graph\.facebook\.com/v\d", src) and f != "wa_version.py":
                    offenders.append(os.path.join(base, f))
    assert offenders == []


def test_the_default_is_the_currently_deployed_version_and_reads_from_one_place(monkeypatch):
    from core import wa_version
    monkeypatch.delenv("WA_GRAPH_VERSION", raising=False)
    assert wa_version.DEFAULT_GRAPH_VERSION == "v18.0" and wa_version.graph_version() == "v18.0"


@pytest.mark.parametrize("bad", ["", "24.0", "v24", "latest", "v24.0/x", "../v24.0", "V24.0", "v2400.0", "v24.0 beta"])
def test_invalid_settings_fall_back_to_the_default(monkeypatch, bad):
    from core import wa_version
    monkeypatch.setenv("WA_GRAPH_VERSION", bad)
    assert wa_version.graph_version() == "v18.0"


class _Resp:
    status_code = 200
    text = "{}"
    content = b"data"
    headers = {"content-type": "audio/ogg"}

    def __init__(self, body=None): self._b = body if body is not None else {"messages": [{"id": "wamid.1"}]}
    def json(self): return self._b
    def raise_for_status(self): pass


@pytest.fixture
def http(monkeypatch):
    import requests
    calls = []

    def rec(method):
        def f(url, *a, **k):
            calls.append({"method": method, "url": url, "json": k.get("json"), "headers": k.get("headers")})
            return _Resp({"messages": [{"id": "wamid.1"}], "id": "MEDIA1"})
        return f
    monkeypatch.setattr(requests, "post", rec("POST"))
    monkeypatch.setattr(requests, "get", rec("GET"))
    return calls


@pytest.mark.parametrize("ver", CASES)
def test_text_messages_use_the_configured_version_and_the_same_payload(monkeypatch, http, ver):
    import main
    expected = _versioned(monkeypatch, ver)
    out = main.send_whatsapp("PID", "TOKEN", "263771000111", "hello")
    assert "error" not in out
    c = http[-1]
    assert c["url"] == f"https://graph.facebook.com/{expected}/PID/messages"
    assert c["json"] == {"messaging_product": "whatsapp", "to": "263771000111", "type": "text", "text": {"body": "hello"}}
    assert c["headers"]["Authorization"] == "Bearer TOKEN"


@pytest.mark.parametrize("ver", CASES)
def test_the_legacy_integration_sender(monkeypatch, http, ver):
    from integrations import whatsapp as wa
    expected = _versioned(monkeypatch, ver)
    wa.send_whatsapp_message("PID", "TOKEN", "whatsapp:263771000111", "hello")
    assert http[-1]["url"] == f"https://graph.facebook.com/{expected}/PID/messages"
    assert http[-1]["json"]["to"] == "263771000111" and http[-1]["json"]["type"] == "text"


@pytest.mark.parametrize("ver", CASES)
def test_document_upload_and_send(monkeypatch, http, tmp_path, ver):
    from integrations import whatsapp as wa
    expected = _versioned(monkeypatch, ver)
    f = tmp_path / "invoice.pdf"
    f.write_bytes(b"%PDF-1.4 test")
    wa.send_whatsapp_document("263771000111", str(f), "TOKEN", "PID", "Invoice")
    urls = [c["url"] for c in http]
    assert f"https://graph.facebook.com/{expected}/PID/media" in urls and f"https://graph.facebook.com/{expected}/PID/messages" in urls


@pytest.mark.parametrize("ver", CASES)
def test_catalogue_senders(monkeypatch, http, ver):
    from services import whatsapp_catalog as cat
    expected = _versioned(monkeypatch, ver)
    cat.send_text_message("PID", "TOKEN", "263771000111", "menu")
    cat.send_product_image("PID", "TOKEN", "263771000111", {"name": "Burger", "price": 5, "image_url": "https://cdn.example/b.png"})
    assert http and all(c["url"] == f"https://graph.facebook.com/{expected}/PID/messages" for c in http)
    assert http[0]["json"]["type"] == "text" and http[1]["json"]["type"] == "image"


@pytest.mark.parametrize("ver", CASES)
def test_marketing_paths_that_post_directly(monkeypatch, http, ver):
    import growth.reengagement as rg
    import growth.cart_recovery as cr
    import crud
    expected = _versioned(monkeypatch, ver)
    monkeypatch.setattr(crud, "get_decrypted_token", lambda b: "TOKEN")
    assert rg._send_message({"whatsapp_phone_id": "PID"}, "263771000111", "hi") is True
    import routes.webhook_routes as wh
    monkeypatch.setattr(wh, "send_whatsapp", None)                     # force the cart-recovery HTTP fallback
    assert cr._send_recovery_message(1, {"whatsapp_phone_id": "PID"}, "263771000111", "hi") is True
    assert all(c["url"] == f"https://graph.facebook.com/{expected}/PID/messages" for c in http) and len(http) == 2


class _FakeAsyncClient:
    calls = []

    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

    async def get(self, url, **k):
        _FakeAsyncClient.calls.append(url)
        if "graph.facebook.com" in url:
            return _Resp({"url": "https://lookaside.fbsbx.com/x", "mime_type": "image/png", "file_size": 10})
        return _Resp()


@pytest.mark.parametrize("ver", CASES)
def test_media_lookup_uses_the_configured_version(monkeypatch, ver):
    import httpx
    from services import whatsapp_service as ws
    expected = _versioned(monkeypatch, ver)
    _FakeAsyncClient.calls = []
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)

    async def ok(url): return False                                   # stop after the lookup: only the Graph URL matters here
    monkeypatch.setattr(ws, "_media_url_ok", ok)
    asyncio.run(ws.download_whatsapp_media("MEDIA9", "TOKEN"))
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    asyncio.run(ws.transcribe_whatsapp_voice_note("MEDIA9", "TOKEN"))
    graph = [u for u in _FakeAsyncClient.calls if "graph.facebook.com" in u]
    assert graph and all(u == f"https://graph.facebook.com/{expected}/MEDIA9" for u in graph)


@pytest.mark.parametrize("ver", CASES)
def test_template_sends_use_the_configured_version(monkeypatch, http, ver):
    import services.wa_templates as wt
    expected = _versioned(monkeypatch, ver)
    monkeypatch.setattr(wt, "resolve_credentials", lambda b: ("PID", "TOKEN"))
    out = wt._default_send(None, wt.build_payload("263771000111", {"name": "n_1", "language": "en", "param_spec": []}, {}))
    assert out["ok"] is True and http[-1]["url"] == f"https://graph.facebook.com/{expected}/PID/messages"
    assert http[-1]["json"]["type"] == "template"


@pytest.mark.parametrize("ver", CASES)
def test_the_diagnostic_routes_use_the_configured_version(monkeypatch, http, ver):
    import inspect
    import routes.admin_routes as adm
    import routes.business_routes as biz
    _versioned(monkeypatch, ver)
    for mod in (adm, biz):
        assert "graph_url(" in inspect.getsource(mod) and "graph.facebook.com/v" not in inspect.getsource(mod)


def test_surrounding_whitespace_is_tolerated(monkeypatch):
    from core import wa_version
    monkeypatch.setenv("WA_GRAPH_VERSION", " v24.0 ")
    assert wa_version.graph_version() == "v24.0"


def test_the_version_is_read_on_every_call_so_a_rollback_needs_no_deploy(monkeypatch):
    from core.wa_version import graph_url
    monkeypatch.setenv("WA_GRAPH_VERSION", "v24.0")
    a = graph_url("1/messages")
    monkeypatch.delenv("WA_GRAPH_VERSION")
    b = graph_url("1/messages")
    assert "v24.0" in a and "v18.0" in b
