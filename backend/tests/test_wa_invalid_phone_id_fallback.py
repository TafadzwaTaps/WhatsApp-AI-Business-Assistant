"""Campaign failure from the live log: Graph API code 100 / subcode 33 (invalid phone-number ID)."""
import types
import pytest
import main
from services.campaign_service import _friendly_send_error

BAD = {"error": {"message": "Unsupported post request. Object with ID '1088821460973757' does not exist",
                 "code": 100, "type": "GraphMethodException", "error_subcode": 33}}
OK = {"messages": [{"id": "wamid.1"}]}


class _Resp:
    def __init__(self, status, body): self.status_code, self._b = status, body
    def json(self): return self._b


@pytest.fixture
def shared(monkeypatch):
    monkeypatch.setattr(main, "SHARED_PHONE_NUMBER_ID", "SHARED_ID")
    monkeypatch.setattr(main, "SHARED_WA_TOKEN", "SHARED_TOKEN")
    # platform pause check must not hit the db
    import core.platform_controls as pc
    monkeypatch.setattr(pc, "is_paused", lambda *_: False, raising=False)


def test_invalid_phone_id_retries_once_via_shared_number(monkeypatch, shared):
    calls = []
    def post(url, headers=None, json=None, timeout=None):
        calls.append(url)
        return _Resp(400, BAD) if "1088821460973757" in url else _Resp(200, OK)
    monkeypatch.setattr(main.http_requests, "post", post)
    r = main.send_whatsapp("1088821460973757", "own-token", "4851", "hi")
    assert r == OK
    assert len(calls) == 2 and "SHARED_ID" in calls[1]


def test_no_retry_loop_when_shared_also_fails(monkeypatch, shared):
    calls = []
    monkeypatch.setattr(main.http_requests, "post", lambda url, **k: calls.append(url) or _Resp(400, BAD))
    r = main.send_whatsapp("1088821460973757", "own-token", "4851", "hi")
    assert "error" in r and len(calls) == 2          # own, then shared once — never more


def test_shared_creds_failing_does_not_retry(monkeypatch, shared):
    calls = []
    monkeypatch.setattr(main.http_requests, "post", lambda url, **k: calls.append(url) or _Resp(400, BAD))
    main.send_whatsapp("SHARED_ID", "SHARED_TOKEN", "4851", "hi")
    assert len(calls) == 1


def test_other_4xx_errors_do_not_fall_back(monkeypatch, shared):
    other = {"error": {"code": 131047, "message": "Re-engagement message"}}
    calls = []
    monkeypatch.setattr(main.http_requests, "post", lambda url, **k: calls.append(url) or _Resp(400, other))
    main.send_whatsapp("OWN", "own-token", "4851", "hi")
    assert len(calls) == 1


def test_no_fallback_when_shared_not_configured(monkeypatch):
    monkeypatch.setattr(main, "SHARED_PHONE_NUMBER_ID", "")
    monkeypatch.setattr(main, "SHARED_WA_TOKEN", "")
    import core.platform_controls as pc
    monkeypatch.setattr(pc, "is_paused", lambda *_: False, raising=False)
    calls = []
    monkeypatch.setattr(main.http_requests, "post", lambda url, **k: calls.append(url) or _Resp(400, BAD))
    assert "error" in main.send_whatsapp("OWN", "t", "4851", "hi") and len(calls) == 1


def test_friendly_errors():
    assert "Phone Number ID" in _friendly_send_error(RuntimeError(BAD["error"]))
    assert "24 hours" in _friendly_send_error(RuntimeError({"code": 131047, "message": "x"}))
    assert "expired" in _friendly_send_error(RuntimeError({"code": 190, "message": "x"}))
    assert _friendly_send_error(RuntimeError("boom")) == "boom"
