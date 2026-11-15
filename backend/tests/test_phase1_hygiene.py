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


# ── Security-review hygiene (change-password hardening, raw-exception leaks) ──

def _change_password_src():
    tree = ast.parse(_src("routes/auth_routes.py"))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "change_password")
    return ast.unparse(fn)


def test_change_password_rejects_empty_current_or_stored_hash_before_verify():
    src = _change_password_src()
    assert "not data.current_password" in src and "not stored_hash" in src
    # the guards must short-circuit before verify_password is evaluated
    assert src.index("not stored_hash") < src.index("verify_password(")
    assert src.index("not data.current_password") < src.index("verify_password(")


def test_change_password_rate_limit_keyed_by_business():
    src = _change_password_src()
    assert "key_suffix=str(business_id)" in src
    assert "'change_password', request, 5, 60" in src  # still 5/min


def test_change_password_length_limits_and_bcrypt_72_bytes():
    from pydantic import ValidationError
    from routes.auth_routes import ChangePasswordRequest
    ChangePasswordRequest(current_password="a" * 128, new_password="b" * 128)
    for kw in ({"current_password": "a" * 129, "new_password": "ok-pass-1"},
               {"current_password": "x", "new_password": "b" * 129}):
        try:
            ChangePasswordRequest(**kw)
            raise AssertionError("expected ValidationError")
        except ValidationError:
            pass
    src = _change_password_src()
    assert ".encode('utf-8')) > 72" in src
    assert "HTTPException(400" in src


# Files NOT owned by this hardening pass that still return str(exc) in a 500.
# Allow-list is a ceiling per file: it may shrink, never grow. TODO: clean up.
#   - routes_saas/billing_routes.py: Stripe portal (payment code, deliberately untouched)
#   - the rest: onboarding/saas-admin/ux/expansion routes, out of scope of this pass
_STR_EXC_500_ALLOWLIST = {
    "routes/expansion_routes.py": 2,
    "routes/onboarding_routes.py": 8,
    "routes/saas_admin_routes.py": 19,
    "routes/ux_routes.py": 2,
    "routes_saas/billing_routes.py": 1,
    "routes_saas/onboarding_routes.py": 8,
    "routes_saas/saas_admin_routes.py": 7,
}


def test_no_str_exc_in_http_500_outside_allowlist():
    import re
    pat = re.compile(r"HTTPException\(\s*500\s*,\s*(?:detail\s*=\s*)?str\(\s*(?:exc|e|err|ex)\s*\)")
    pat_resp = re.compile(r"status_code\s*=\s*500[^\n]*str\(\s*(?:exc|e)\s*\)")
    offenders = {}
    for folder in ("routes", "routes_saas"):
        for p in sorted((BACKEND / folder).glob("*.py")):
            rel = f"{folder}/{p.name}"
            text = p.read_text(encoding="utf-8")
            n = len(pat.findall(text)) + len(pat_resp.findall(text))
            if n > _STR_EXC_500_ALLOWLIST.get(rel, 0):
                offenders[rel] = n
    assert not offenders, f"raw exception text in 500 responses: {offenders}"


def test_debug_endpoints_do_not_echo_exceptions():
    admin = _src("routes/admin_routes.py")
    for frag in ('"error": str(exc)', 'steps["token_error"]    = str(exc)',
                 'HTTPException(422, str(exc))', '"error": str(e)'):
        assert frag not in admin


def test_debug_security_text_reflects_fail_closed():
    admin = _src("routes/admin_routes.py")
    assert "ALLOW_UNSIGNED_WEBHOOKS=1" in admin and "fail closed" in admin
