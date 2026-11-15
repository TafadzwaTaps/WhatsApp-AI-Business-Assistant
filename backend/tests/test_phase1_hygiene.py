"""Phase 1 tests: change-password endpoint, cron secret compare, log hygiene."""
import ast
import pathlib

BACKEND = pathlib.Path(__file__).resolve().parent.parent


def _src(rel):
    return (BACKEND / rel).read_text(encoding="utf-8")


def test_change_password_route_defined_and_authenticated():
    tree = ast.parse(_src("routes/auth_routes.py"))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "change_password")
    src = ast.unparse(fn)
    assert "get_current_user" in src
    assert "verify_password" in src and "hash_password" in src
    assert "password_changed_at" in src
    assert "business_id" in src and "impersonated_by" in src


def test_no_secret_fragments_logged():
    assert "prefix=" not in _src("core/crypto.py")
    assert "header_preview" not in _src("core/auth.py")
    admin = _src("routes/admin_routes.py")
    assert "token[-6:]" not in admin and "fernet_key[:" not in admin and "supa_key[:" not in admin


def test_cron_compare_constant_time():
    from routes_saas import billing_routes as b
    assert b._cron_secret_ok("abc", "abc")
    assert not b._cron_secret_ok("abd", "abc")
    assert not b._cron_secret_ok("", "abc")
    assert not b._cron_secret_ok("é", "abc")
    assert 'headers.get("x-cron-secret") !=' not in _src("routes_saas/billing_routes.py")


def test_no_raw_exception_in_500s():
    for rel in ("routes/chat_routes.py", "routes/growth_routes.py", "routes/admin_routes.py"):
        assert "HTTPException(500, str(exc))" not in _src(rel)


def test_password_reset_uses_shared_ip_helper():
    assert "x-forwarded-for" not in _src("routes/password_reset_routes.py").lower()
