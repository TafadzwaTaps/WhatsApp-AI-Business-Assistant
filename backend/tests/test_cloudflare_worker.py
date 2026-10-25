"""Cloudflare integration: Worker behaviour, Worker/page sync, edge-secret IP trust, X-WZ-App header."""
import json, os, shutil, subprocess

import pytest
from fastapi.testclient import TestClient

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_worker_node_suite_passes():
    r = subprocess.run(["node", os.path.join(ROOT, "cloudflare", "test_worker.mjs")],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr


def test_worker_embeds_current_maintenance_page():
    """If static/maintenance.html changes, run: python cloudflare/build_worker.py"""
    html = open(os.path.join(ROOT, "static", "maintenance.html"), encoding="utf-8").read()
    worker = open(os.path.join(ROOT, "cloudflare", "maintenance-worker.js"), encoding="utf-8").read()
    assert json.dumps(html) in worker


class _Req:
    def __init__(self, headers, host="10.0.0.1"):
        self.headers = {k.lower(): v for k, v in headers.items()}
        class C: pass
        self.client = C(); self.client.host = host


def test_ip_unchanged_without_edge_secret(monkeypatch):
    from services.security import get_client_ip
    monkeypatch.delenv("EDGE_SHARED_SECRET", raising=False)
    monkeypatch.delenv("TRUSTED_PROXY_DEPTH", raising=False)
    r = _Req({"x-forwarded-for": "6.6.6.6, 1.2.3.4", "cf-connecting-ip": "9.9.9.9", "x-wz-edge": "anything"})
    assert get_client_ip(r) == "1.2.3.4"          # last value, same as before


def test_ip_uses_cf_header_only_with_valid_secret(monkeypatch):
    from services.security import get_client_ip
    monkeypatch.setenv("EDGE_SHARED_SECRET", "s3cret")
    ok = _Req({"x-forwarded-for": "1.2.3.4, 172.70.0.1", "cf-connecting-ip": "1.2.3.4", "x-wz-edge": "s3cret"})
    assert get_client_ip(ok) == "1.2.3.4"
    # direct hit on onrender.com trying to forge the header: wrong/missing secret -> ignored
    forged = _Req({"x-forwarded-for": "5.5.5.5", "cf-connecting-ip": "8.8.8.8", "x-wz-edge": "guess"})
    assert get_client_ip(forged) == "5.5.5.5"
    none = _Req({"x-forwarded-for": "5.5.5.5", "cf-connecting-ip": "8.8.8.8"})
    assert get_client_ip(none) == "5.5.5.5"


def test_every_response_carries_x_wz_app_header():
    import main
    c = TestClient(main.app, raise_server_exceptions=False)
    assert c.get("/healthz").headers.get("x-wz-app") == "1"
