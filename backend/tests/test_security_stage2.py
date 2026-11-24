"""Security audit Stage 2: F-01 (shared-number cross-tenant send/close) and F-02 (webhook body cap)."""
from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from routes import chat_routes as cr
from services.security import read_body_capped


class _Req:
    def __init__(self, chunks, content_length=None):
        self._chunks = chunks
        self.headers = {} if content_length is None else {"content-length": str(content_length)}

    async def stream(self):
        for c in self._chunks:
            yield c


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_body_cap_accepts_small_body():
    assert _run(read_body_capped(_Req([b"ab", b"cd"], 4), 10)) == b"abcd"


def test_body_cap_keeps_request_json_usable():
    app = FastAPI()

    @app.post("/t")
    async def t(request: Request):
        raw = await read_body_capped(request)
        return {"n": len(raw), "json": await request.json()}

    c = TestClient(app)
    assert c.post("/t", json={"a": 1}).json() == {"n": 7, "json": {"a": 1}}
    assert c.post("/t", content=b"x" * 1_000_001).status_code == 413


def test_body_cap_rejects_declared_oversize():
    with pytest.raises(HTTPException) as e:
        _run(read_body_capped(_Req([b"x"], 99), 10))
    assert e.value.status_code == 413


def test_body_cap_rejects_streamed_oversize_without_content_length():
    with pytest.raises(HTTPException) as e:
        _run(read_body_capped(_Req([b"x" * 6, b"x" * 6]), 10))
    assert e.value.status_code == 413


def test_body_cap_rejects_bad_content_length():
    with pytest.raises(HTTPException) as e:
        _run(read_body_capped(_Req([b"x"], "abc"), 10))
    assert e.value.status_code == 400


def test_cross_tenant_reply_off_by_default(monkeypatch):
    monkeypatch.delenv("ALLOW_SHARED_NUMBER_CROSS_TENANT_REPLY", raising=False)
    assert cr._cross_tenant_reply_enabled() is False


def test_cross_tenant_reply_opt_in(monkeypatch):
    monkeypatch.setenv("ALLOW_SHARED_NUMBER_CROSS_TENANT_REPLY", "1")
    assert cr._cross_tenant_reply_enabled() is True


def test_chat_send_does_not_use_fallback_by_default(monkeypatch):
    monkeypatch.delenv("ALLOW_SHARED_NUMBER_CROSS_TENANT_REPLY", raising=False)
    monkeypatch.setattr(cr.crud, "get_customer_by_id", lambda cid, bid: None)
    called = []
    monkeypatch.setattr(cr, "_shared_number_customer_lookup", lambda *a: called.append(a) or {"id": 1})
    with pytest.raises(HTTPException) as e:
        _run(cr.chat_send(cr.ChatSendRequest(customer_id=7, text="hi"), user={"business_id": 1}))
    assert e.value.status_code == 404 and not called


def test_close_conversation_does_not_use_fallback_by_default(monkeypatch):
    monkeypatch.delenv("ALLOW_SHARED_NUMBER_CROSS_TENANT_REPLY", raising=False)
    monkeypatch.setattr(cr.crud, "get_customer_by_id", lambda cid, bid: None)
    called = []
    monkeypatch.setattr(cr, "_shared_number_customer_lookup", lambda *a: called.append(a) or {"id": 1})
    with pytest.raises(HTTPException) as e:
        _run(cr.close_conversation(7, cr.ConversationCloseRequest(), user={"business_id": 1}))
    assert e.value.status_code == 404 and not called
