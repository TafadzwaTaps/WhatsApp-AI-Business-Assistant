"""Medium audit items: cron secret, rate limits, exact slug, twins, swallowed errors, shared limiter."""
import logging, os, re, sys, types
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services import security as sec


@pytest.fixture(autouse=True)
def _clean_limits(monkeypatch):
    sec._rate_store.clear()
    monkeypatch.delenv("RATE_LIMIT_BACKEND", raising=False)
    yield
    sec._rate_store.clear()


def _app_with_handler(*routers):
    app = FastAPI()
    @app.exception_handler(sec.RateLimitExceeded)
    async def _h(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": "rate limited"}, status_code=429)
    for r in routers:
        app.include_router(r)
    return app


# ── 1. cron secret ───────────────────────────────────────────────────────
def test_campaign_runner_needs_cron_secret(monkeypatch):
    from routes import growth_routes
    c = TestClient(_app_with_handler(growth_routes.router))
    monkeypatch.delenv("CRON_SECRET", raising=False)
    assert c.post("/campaigns/scheduled/run").status_code == 503
    monkeypatch.setenv("CRON_SECRET", "s3")
    assert c.post("/campaigns/scheduled/run").status_code == 403
    assert c.post("/campaigns/scheduled/run", headers={"x-cron-secret": "bad"}).status_code == 403


# ── 2. rate limits on public endpoints ───────────────────────────────────
def test_support_ask_is_rate_limited():
    from routes import ux_routes
    c = TestClient(_app_with_handler(ux_routes.router))
    codes = [c.post("/support/ask", json={"question": "how do I add a product", "context": ""}).status_code
             for _ in range(sec.LIMITS["public_ask"]["max_calls"] + 2)]
    assert codes[-1] == 429 and codes[0] != 429


def test_reset_token_validation_is_rate_limited():
    from routes import password_reset_routes as pr
    c = TestClient(_app_with_handler(pr.router))
    codes = [c.post("/auth/validate-reset-token", json={"token": "x" * 20}).status_code
             for _ in range(sec.LIMITS["reset"]["max_calls"] + 2)]
    assert codes[-1] == 429


# ── 3. exact slug lookup ─────────────────────────────────────────────────
from core.slug import pick_business, name_to_slug

ROWS = [{"id": 1, "name": "Digital Edge"}, {"id": 2, "name": "Digital Edge Solutions"},
        {"id": 3, "name": "Firelilyfarrismum"}, {"id": 4, "name": "Flavoury Foods (Pvt) Ltd"}]


def test_slug_exact_not_similar():
    assert pick_business("digital-edge", ROWS)["id"] == 1
    assert pick_business("digital-edge-solutions", ROWS)["id"] == 2


def test_slug_punctuation_and_compact_forms():
    assert pick_business("flavoury-foods-pvt-ltd", ROWS)["id"] == 4
    assert pick_business("firelilyfarrismum", ROWS)["id"] == 3


def test_slug_ambiguous_loose_match_is_refused():
    assert pick_business("digital", ROWS) is None            # two candidates: never guess
    assert pick_business("flavoury", ROWS)["id"] == 4        # single candidate: allowed
    assert pick_business("nothing-here", ROWS) is None


def test_slug_duplicate_names_pick_oldest():
    rows = [{"id": 9, "name": "Same Shop"}, {"id": 5, "name": "same  shop"}]
    assert pick_business("same-shop", rows)["id"] == 5


# ── 4. twin modules ──────────────────────────────────────────────────────
BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    return open(os.path.join(BACKEND, rel), encoding="utf-8").read()


def test_live_twins_are_identical():
    assert _read("services/inventory.py") == _read("inventory.py")
    assert _read("services/tenant_router.py") == _read("routes/tenant_router.py")


def test_no_code_imports_the_stale_twins():
    dead = [r"routes_saas\.(marketplace|onboarding|saas_admin)_routes", r"crud\.crud\b",
            r"(^|\s)from inventory import", r"(^|\s)import inventory\b", r"routes\.tenant_router"]
    offenders = []
    for root, _, files in os.walk(BACKEND):
        if "tests" in root.split(os.sep):
            continue
        for f in files:
            if f.endswith(".py"):
                src = open(os.path.join(root, f), encoding="utf-8", errors="ignore").read()
                for line in src.splitlines():
                    if re.match(r"\s*(from|import)\s", line) and any(re.search(p, line) for p in dead):
                        offenders.append((f, line.strip()))
    assert offenders == []


def test_stale_twins_are_marked():
    for rel in ("routes_saas/marketplace_routes.py", "routes_saas/onboarding_routes.py",
                "routes_saas/saas_admin_routes.py", "crud/crud.py"):
        assert "[NOT LIVE]" in _read(rel)[:400]


# ── 5. swallowed errors are logged ───────────────────────────────────────
def test_swallow_logs_warning(caplog):
    from core.safelog import swallow
    with caplog.at_level(logging.WARNING, logger="wazibot.swallowed"):
        swallow("mod.x", ValueError("boom"))
    assert "mod.x" in caplog.text and "boom" in caplog.text


def test_no_silent_except_pass_left():
    pat = re.compile(r"except Exception:\s*(#[^\n]*)?\n\s+pass\b|except Exception:\s*pass\b")
    left = []
    for root, _, files in os.walk(BACKEND):
        if "tests" in root.split(os.sep):
            continue
        for f in files:
            if f.endswith(".py") and f != "safelog.py":
                if pat.search(open(os.path.join(root, f), encoding="utf-8", errors="ignore").read()):
                    left.append(f)
    assert left == []


# ── 6. shared rate-limit storage ─────────────────────────────────────────
class _FakeRpc:
    def __init__(self, store): self.store, self.name, self.params = store, None, None
    def rpc(self, name, params): self.name, self.params = name, params; return self
    def execute(self):
        k = self.params["p_key"]
        if self.name == "rl_hit":
            self.store[k] = self.store.get(k, 0) + 1; v = self.store[k]
        elif self.name == "rl_peek": v = self.store.get(k, 0)
        else: self.store.pop(k, None); v = None
        return types.SimpleNamespace(data=v)


def test_shared_backend_counts_across_restarts(monkeypatch):
    store = {}
    monkeypatch.setenv("RATE_LIMIT_BACKEND", "supabase")
    monkeypatch.setattr(sys.modules["core.db"], "supabase", _FakeRpc(store), raising=False)
    sec._shared_down_until = 0.0
    req = types.SimpleNamespace(headers={}, client=types.SimpleNamespace(host="9.9.9.9"))
    for _ in range(3):
        sec.rate_limit("t", req, 3, 60)
    sec._rate_store.clear()                      # simulate a process restart: memory is gone
    with pytest.raises(sec.RateLimitExceeded):
        sec.rate_limit("t", req, 3, 60)          # shared counter remembers


def test_shared_backend_failure_falls_back_to_memory(monkeypatch):
    class Boom:
        def rpc(self, *a, **k): raise RuntimeError("table missing")
    monkeypatch.setenv("RATE_LIMIT_BACKEND", "supabase")
    monkeypatch.setattr(sys.modules["core.db"], "supabase", Boom(), raising=False)
    sec._shared_down_until = 0.0
    req = types.SimpleNamespace(headers={}, client=types.SimpleNamespace(host="8.8.8.8"))
    sec.rate_limit("t2", req, 2, 60); sec.rate_limit("t2", req, 2, 60)
    with pytest.raises(sec.RateLimitExceeded):
        sec.rate_limit("t2", req, 2, 60)         # still limited, via memory
    sec._shared_down_until = 0.0


def test_account_lockout_uses_shared_counter(monkeypatch):
    store = {}
    monkeypatch.setenv("RATE_LIMIT_BACKEND", "supabase")
    monkeypatch.setattr(sys.modules["core.db"], "supabase", _FakeRpc(store), raising=False)
    sec._shared_down_until = 0.0
    for _ in range(sec.LOGIN_MAX_ATTEMPTS_PER_ACCOUNT):
        sec.record_failed_login_account("Victim")
    sec._account_login_fails.clear()             # restart
    with pytest.raises(sec.RateLimitExceeded):
        sec.check_account_login_lockout("victim")
    sec.clear_account_login_lockout("victim")
    sec.check_account_login_lockout("victim")    # cleared on successful login
