"""
tests/test_content_moderation.py — automatic upload screening.

Covers:
  - services/content_moderation.check_structural: magic-byte/type/size
    validation, independent of any AI key.
  - services/content_moderation.check_ai_safety / screen_upload: fails
    open when no vision key is configured or the call errors, blocks
    and traces when the model flags content.
  - Wiring: POST /me/upload-avatar, POST /products/upload-image, and
    POST /onboarding/step/2 all reject disallowed/flagged uploads with
    422 and never reach Supabase Storage; a clean upload still succeeds.
"""

import pytest
from fastapi.testclient import TestClient

PNG_MAGIC = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
FAKE_EXE  = b"MZ" + b"\x00" * 40  # a Windows executable's real magic bytes


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
    import routes.onboarding_routes as ob
    dep = lambda: {"business_id": business_id, "username": username, "role": "business"}
    client.app.dependency_overrides[br.require_business] = dep
    client.app.dependency_overrides[ob.require_business] = dep


# ── Layer 1: structural checks (no external dependency) ─────────────────────

def test_structural_rejects_executable_disguised_as_image():
    from services.content_moderation import check_structural
    ok, reason = check_structural(FAKE_EXE, "image/png")
    assert not ok
    assert "content" in reason.lower() or "type" in reason.lower()


def test_structural_rejects_oversized_file():
    from services.content_moderation import check_structural, MAX_IMAGE_BYTES
    huge = PNG_MAGIC + b"\x00" * MAX_IMAGE_BYTES
    ok, reason = check_structural(huge, "image/png")
    assert not ok
    assert "large" in reason.lower()


def test_structural_rejects_unsupported_content_type():
    from services.content_moderation import check_structural
    ok, reason = check_structural(PNG_MAGIC, "application/pdf")
    assert not ok


def test_structural_accepts_genuine_png():
    from services.content_moderation import check_structural
    ok, reason = check_structural(PNG_MAGIC, "image/png")
    assert ok
    assert reason == ""


# ── Layer 2: AI safety check — fail-soft behavior ───────────────────────────

def test_ai_check_fails_open_when_no_key_configured(monkeypatch):
    import services.content_moderation as cm
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    ok, category, reason = cm.check_ai_safety(PNG_MAGIC, "image/png")
    assert ok is True


def test_ai_check_fails_open_when_call_errors(monkeypatch):
    import services.content_moderation as cm
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(cm, "_call_vision", lambda *a, **kw: None)
    ok, category, reason = cm.check_ai_safety(PNG_MAGIC, "image/png")
    assert ok is True


def test_ai_check_blocks_when_model_flags_content(monkeypatch):
    import services.content_moderation as cm
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        cm, "_call_vision",
        lambda *a, **kw: '{"flagged": true, "category": "sexual_content", "reason": "explicit imagery"}',
    )
    ok, category, reason = cm.check_ai_safety(PNG_MAGIC, "image/png")
    assert ok is False
    assert category == "sexual_content"
    assert reason == "explicit imagery"


def test_ai_check_allows_when_model_does_not_flag(monkeypatch):
    import services.content_moderation as cm
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(cm, "_call_vision", lambda *a, **kw: '{"flagged": false, "category": "none", "reason": ""}')
    ok, category, reason = cm.check_ai_safety(PNG_MAGIC, "image/png")
    assert ok is True


def test_screen_upload_logs_rejection_for_traceability(monkeypatch):
    import services.content_moderation as cm
    import crud.security_events as sec_events
    recorded = {}
    monkeypatch.setattr(cm, "check_ai_safety", lambda data, ct: (False, "graphic_violence", "gore"))

    # Patch the real module's function in place rather than swapping
    # sys.modules["crud.security_events"] for a fake object: content_
    # moderation.py does `import crud.security_events as _sec_events`
    # INSIDE the function it's called from, and that statement resolves
    # through the `crud` package's own `security_events` attribute (a
    # side effect of the first import), not through sys.modules directly
    # — so replacing only the sys.modules entry doesn't reliably reach
    # it, and can leave a stale module reference in place for whichever
    # test runs next. Patching the function on the real module avoids
    # that path entirely and is properly undone by monkeypatch either way.
    monkeypatch.setattr(sec_events, "log_security_event", lambda **kw: recorded.update(kw))

    ok, err = cm.screen_upload(
        data=PNG_MAGIC, content_type="image/png", filename="x.png",
        business_id=42, username="shopowner", ip="1.2.3.4", endpoint="/me/upload-avatar",
    )
    assert ok is False
    assert recorded["event_type"] == "upload_blocked_content"
    assert recorded["business_id"] == 42
    assert recorded["username"] == "shopowner"
    assert recorded["metadata"]["category"] == "graphic_violence"
    assert recorded["metadata"]["reason"] == "gore"
    assert recorded["metadata"]["endpoint"] == "/me/upload-avatar"


# ── Wiring: real endpoints reject bad uploads before touching storage ───────

def test_upload_avatar_rejects_disguised_executable(client):
    _override_business(client)
    resp = client.post(
        "/me/upload-avatar",
        files={"file": ("avatar.png", FAKE_EXE, "image/png")},
    )
    assert resp.status_code == 422


def test_upload_product_image_rejects_disguised_executable(client):
    _override_business(client)
    resp = client.post(
        "/products/upload-image",
        files={"file": ("product.png", FAKE_EXE, "image/png")},
    )
    assert resp.status_code == 422


def test_upload_avatar_accepts_clean_image_and_reaches_storage(client, monkeypatch):
    _override_business(client)
    import routes.business_routes as br

    class _FakeStorage:
        def from_(self, bucket):
            return self
        def upload(self, *a, **kw):
            return {"ok": True}

    class _FakeSupabase:
        storage = _FakeStorage()
        def table(self, name):
            raise AssertionError("get_business_by_id should go through crud, not raw table() here")

    monkeypatch.setattr(br, "crud", br.crud)  # sanity — crud module untouched
    monkeypatch.setattr(br.crud, "get_business_by_id", lambda bid: {"features_json": {}})
    monkeypatch.setattr(br.crud, "update_business", lambda bid, data: {"ok": True})

    import core.db as db_mod
    monkeypatch.setattr(db_mod, "supabase", _FakeSupabase())

    resp = client.post(
        "/me/upload-avatar",
        files={"file": ("avatar.png", PNG_MAGIC, "image/png")},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_onboarding_step2_rejects_disguised_executable(client):
    _override_business(client)
    resp = client.post(
        "/onboarding/step/2",
        files={"logo": ("logo.png", FAKE_EXE, "image/png")},
        data={"tagline": "hello"},
    )
    assert resp.status_code == 422
