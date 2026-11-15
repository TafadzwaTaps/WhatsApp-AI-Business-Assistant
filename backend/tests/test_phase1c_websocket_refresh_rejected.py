"""Phase 1c: refresh tokens must never authenticate the dashboard websocket."""
import asyncio
import types

import routes.webhook_routes as wr


class _FakeWS:
    def __init__(self, token):
        self.query_params = {"token": token}
        self.closed = None

    async def close(self, code=1000):
        self.closed = code


class _Manager:
    def __init__(self):
        self.connected = False

    async def connect(self, ws, business_id):
        self.connected = True
        raise RuntimeError("stop")  # abort the receive loop if we ever get here

    def disconnect(self, ws, business_id):
        pass


def _run(monkeypatch, payload):
    mgr = _Manager()
    monkeypatch.setattr(wr, "manager", mgr)
    monkeypatch.setattr(wr, "decode_token", lambda t: payload)
    ws = _FakeWS("tok")
    try:
        asyncio.run(wr.websocket_chat(ws, 7))
    except RuntimeError:
        pass
    return ws, mgr


def test_refresh_token_is_rejected(monkeypatch):
    ws, mgr = _run(monkeypatch, {"type": "refresh", "business_id": 7, "role": "business"})
    assert ws.closed == 4001 and not mgr.connected


def test_refresh_token_rejected_even_for_superadmin(monkeypatch):
    ws, mgr = _run(monkeypatch, {"type": "refresh", "role": "superadmin"})
    assert ws.closed == 4001 and not mgr.connected


def test_access_token_for_same_business_is_accepted(monkeypatch):
    ws, mgr = _run(monkeypatch, {"type": "access", "business_id": 7})
    assert mgr.connected and ws.closed is None


def test_access_token_for_other_business_is_rejected(monkeypatch):
    ws, mgr = _run(monkeypatch, {"type": "access", "business_id": 8})
    assert ws.closed == 4001 and not mgr.connected
