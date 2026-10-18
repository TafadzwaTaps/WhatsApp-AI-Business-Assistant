"""Friendly outage handling: /healthz, maintenance page, unhandled-exception handler."""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    import main
    return TestClient(main.app, raise_server_exceptions=False)


def test_healthz_ok_without_auth_or_db(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_maintenance_page_is_branded_and_self_contained():
    import main, re
    r = main.maintenance_response(503)
    body = r.body.decode()
    assert r.status_code == 503 and r.headers["retry-after"] == "30"
    assert "are working on it" in body
    assert not re.search(r'(src|href)="https?://', body)   # no external deps


def test_unhandled_error_gives_html_page_to_browsers_and_json_to_api(client):
    import main

    @main.app.get("/__boom_test", include_in_schema=False)
    def boom():
        raise RuntimeError("db exploded")

    try:
        page = client.get("/__boom_test", headers={"accept": "text/html,application/xhtml+xml"})
        assert page.status_code == 503 and "are working on it" in page.text
        assert "db exploded" not in page.text            # never leak internals

        api = client.get("/__boom_test", headers={"accept": "application/json"})
        assert api.status_code == 500 and "detail" in api.json()
        assert "db exploded" not in api.text
    finally:
        main.app.router.routes[:] = [r for r in main.app.router.routes if getattr(r, "path", "") != "/__boom_test"]


def test_public_site_error_shows_maintenance_not_redirect(client, monkeypatch):
    import services.site_generator as sg
    monkeypatch.setattr(sg, "generate_site_html", lambda slug: (_ for _ in ()).throw(RuntimeError("x")))
    r = client.get("/site/anything", follow_redirects=False)
    assert r.status_code == 503 and "are working on it" in r.text
